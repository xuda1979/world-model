#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐小区协同策略推理服务 (Coord Policy Serving)
==============================================
技术报告 §7.5 的在线推理端: 加载 models/coord_policy.pt (61.4 万参数双头
MLP, 值头 argmax 部署规则 — 与 train_coord_policy.py / LOSO 评测完全一致),
提供 coord_policy_action(ctx) → 逐小区功率偏移组合推荐。

部署规则溯源: sweep_coord_policy.py 10 训练 seed, 值头 10/10 全胜
(regret 0.1226±0.0157 vs score 头 0.2401±0.0433, 实现率 96.5% vs 93.1%)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CTX_COLS = [f"sinr_c{i}" for i in range(3)] + [f"n_ue_c{i}" for i in range(3)]
SCENES = ["campus", "城区", "郊区", "低空", "机器人工厂"]

_MODEL = None
_CKPT = None
_FAILED = False


class CoordPolicyNet(nn.Module):
    """与 train_coord_policy.py 完全同构的推理网络 (双头: score / 值)。"""

    def __init__(self, ctx_dim: int, n_combo: int = 125, hidden: int = 512):
        super().__init__()
        self.act_emb = nn.Parameter(torch.randn(15, 16) * 0.02)
        self.trunk = nn.Sequential(
            nn.Linear(ctx_dim + 31, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
        )
        self.head_score = nn.Linear(hidden, 1)
        self.head_val = nn.Sequential(
            nn.Linear(hidden, 128), nn.GELU(), nn.Linear(128, 1))

    def forward(self, ctx: torch.Tensor, act_onehot: torch.Tensor):
        emb = act_onehot @ self.act_emb
        h = self.trunk(torch.cat([ctx, act_onehot, emb], -1))
        return self.head_score(h).squeeze(-1), self.head_val(h).squeeze(-1)

    def all_values(self, ctx: torch.Tensor, act_oh_all: torch.Tensor):
        B, n = ctx.shape[0], act_oh_all.shape[0]
        ctx_r = ctx[:, None, :].expand(B, n, -1).reshape(B * n, -1)
        a_r = act_oh_all[None].expand(B, n, -1).reshape(B * n, -1)
        scores, vals = self.forward(ctx_r, a_r)
        return scores.view(B, n), vals.view(B, n)


def _act_onehot() -> torch.Tensor:
    """125 × 15 动作 one-hot (3 小区 × 5 档, 字典序与训练一致)。"""
    oh = np.zeros((125, 15), dtype=np.float32)
    for i, (a, b, c) in enumerate(
            [(x, y, z) for x in range(5) for y in range(5) for z in range(5)]):
        oh[i, a] = oh[i, 5 + b] = oh[i, 10 + c] = 1.0
    return torch.tensor(oh)


def load_coord_policy():
    """懒加载 coord policy; 失败置 _FAILED 防止反复重试。"""
    global _MODEL, _CKPT, _FAILED
    if _MODEL is not None or _FAILED:
        return _MODEL
    path = ROOT / "models" / "coord_policy.pt"
    if not path.exists():
        _FAILED = True
        return None
    try:
        ckpt = torch.load(path, map_location="cpu", weights_only=False)
        ctx_dim = len(ckpt["ctx_mu"])
        net = CoordPolicyNet(ctx_dim)
        net.load_state_dict(ckpt["state_dict"])
        net.eval()
        _MODEL, _CKPT = net, ckpt
        return net
    except Exception as e:
        print(f"[coord_serve] coord_policy.pt 加载失败: {e}")
        _FAILED = True
        return None


def coord_policy_action(sinr_c: list[float], n_ue_c: list[int],
                        scene: str) -> dict:
    """值头 argmax 一步推荐 (与 LOSO 评测部署规则逐位一致)。

    参数: sinr_c/n_ue_c 为 3 小区的基线 (0,0,0) 行特征 (决策前观测)。
    返回: 推荐组合 (p0,p1,p2 dB) + top-k 值序 + 元信息。
    """
    net = load_coord_policy()
    if net is None:
        raise RuntimeError("coord_policy.pt 不可用")
    if scene not in SCENES:
        raise ValueError(f"scene 必须为 {SCENES} 之一, 收到: {scene!r}")
    if len(sinr_c) != 3 or len(n_ue_c) != 3:
        raise ValueError("sinr_c / n_ue_c 必须各为 3 个小区的值")
    feats = list(sinr_c) + [np.log1p(int(n)) for n in n_ue_c]
    feats = feats + [1.0 if scene == s else 0.0 for s in SCENES]
    X = (np.asarray(feats, dtype=np.float32) - _CKPT["ctx_mu"]) / _CKPT["ctx_sd"]
    with torch.no_grad():
        oh = _act_onehot()
        _, vals = net.all_values(torch.tensor(X).unsqueeze(0), oh)
        vals = vals.squeeze(0)
        order = vals.argsort(descending=True)
    combos = _CKPT["combos"]
    top = order[:5].tolist()
    best = int(order[0])
    p = [float(x) for x in combos[best].split(",")]
    return {
        "action": {"p0": p[0], "p1": p[1], "p2": p[2],
                   "combo": combos[best]},
        "top_k": [{"combo": combos[i], "value": round(float(vals[i]), 4)}
                  for i in top],
        "deploy_rule": "value-head argmax (10-seed sweep 10/10 全胜)",
        "scene": scene,
        "ctx": {"sinr_c": [round(v, 3) for v in sinr_c],
                "n_ue_c": [int(n) for n in n_ue_c]},
    }


def coord_policy_info() -> dict:
    """模型元信息 (供 API 响应与守护测试)。"""
    if _CKPT is None and load_coord_policy() is None:
        return {"available": False}
    import json
    meta = json.loads((ROOT / "models" / "coord_policy_meta.json").read_text(
        encoding="utf-8"))
    return {
        "available": True, "model": "CoordPolicyNet (score/值双头)",
        "deploy_rule": "value-head argmax",
        "params": meta["params"],
        "action_space": meta["action_space"],
        "loso": {k: meta["loso_summary"][k] for k in (
            "regret_mean", "regret_exact_rate",
            "coord_gain_realized_ratio")},
    }
