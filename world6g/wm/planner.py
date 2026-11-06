#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规划器 (Planner): 基于世界模型的 MPC + 基线策略
================================================
目标: 给定历史状态窗, 选一个动作 (type, value) 最大化世界模型预测的 KPI 轨迹奖励。

设计:
  1. CEM-MPC        — Cross-Entropy Method 在动作参数空间中迭代优化,
                      奖励 = 世界模型 (GRU-WM-MS) K 步 rollout 的加权 KPI。
  2. RandomSearch   — 随机采样动作基线 (消融: 优化是否必要)。
  3. Greedy-OneStep — 单步贪心 (短视基线)。
  4. NoAction       — baseline 动作 (不干预)。

动作空间 (与 wm/data.py ACTION_TYPES 对齐):
  type_idx ∈ {1: pmax_scale, 2: prb_scale, 3: az_offset, 4: ho_offset}
  取值域: pmax_scale ∈ [0.5, 2] (功率倍数), prb_scale ∈ [0.25, 1.0],
          az_offset ∈ [-30, 30]°, ho_offset ∈ [1, 6] dB
  (与 scripts/generate_actions.py 的采样域一致 → 在世界模型训练分布内)

奖励 (可配置权重, 默认面向覆盖-容量均衡):
  r = w_dl·norm(下行速率) + w_sinr·norm(SINR) + w_rsrp·norm(RSRP) + w_cqi·norm(CQI)
  norm = z-score (用 meta 的 mu/sd), 对 K 步预测取均值。
  支持 penalty: 动作幅值正则 (惩罚大幅干预)。

与 twm_serve.py 的关系: twm_serve 复制了推理代码做服务; 这里直接加载
models/gru_wm_ms.pt + meta.json, 独立实现批量 rollout (CEM 需要批量评估)。

用法 (库):
    from wm.planner import CEMPlanner, make_action
    planner = CEMPlanner()                    # 加载 models/gru_wm_ms.pt
    act = planner.plan(hist_states)           # hist_states [H=8, S=4] 原始单位
    # act = {"type": "prb_scale", "value": 0.73, "score": 1.02}

用法 (CLI 自测):
    .venv/bin/python -m wm.planner --demo
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

from .models import GRUWM, N_STATE
from .data import ACTION_TYPES, HIST

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
WM_PATH = MODELS_DIR / "gru_wm_ms.pt"
META_PATH = MODELS_DIR / "gru_wm_ms_meta.json"

# ---------------- 动作空间定义 (域 = 训练采样域) ----------------
# idx 与 ACTION_TYPES 对齐: 0 baseline, 1 pmax_scale, 2 prb_scale, 3 az_offset, 4 ho_offset
ACTION_SPACE = {
    "pmax_scale": dict(idx=1, lo=0.5, hi=2.0),
    "prb_scale":  dict(idx=2, lo=0.25, hi=1.0),
    "az_offset":  dict(idx=3, lo=-30.0, hi=30.0),
    "ho_offset":  dict(idx=4, lo=1.0, hi=6.0),
}

# 默认奖励权重: 速率 > SINR > RSRP > CQI (面向用户体验吞吐)
DEFAULT_W = np.array([0.10, 0.20, 0.60, 0.10], dtype=np.float32)  # [RSRP, SINR, DL, CQI]


# ============================================================
# 世界模型包装 (批量 rollout, 原始单位 ↔ 归一化)
# ============================================================
class WorldModel:
    """加载 gru_wm_ms.pt, 提供批量多步 rollout (原始单位接口)。

    batch rollout: 输入 [B, H, S] 历史 (原始单位) + [B] type_idx + [B] value,
    输出 [B, K, S] 预测 (原始单位)。K 步全部批量并行 → CEM 一次评估一代。
    """

    def __init__(self, wm_path: Path = WM_PATH, meta_path: Path = META_PATH, device: str = "cpu"):
        self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.mu = np.array(self.meta["mu"], dtype=np.float32)
        self.sd = np.array(self.meta["sd"], dtype=np.float32)
        self.a_mu = float(self.meta["a_mu"])
        self.a_sd = float(self.meta["a_sd"])
        self.action_types = self.meta["action_types"]
        self.hist = int(self.meta["hist"])

        self.model = GRUWM()
        self.model.load_state_dict(torch.load(wm_path, map_location=device))
        self.model.eval()
        self.device = device

    def rollout(self, hist: np.ndarray, type_idx: np.ndarray, value: np.ndarray,
                steps: int = 8) -> np.ndarray:
        """批量 K 步 rollout.

        hist: [B, H, S] 原始单位; type_idx: [B] int; value: [B] 原始动作值
        返回: [B, K, S] 原始单位预测
        """
        hist = np.asarray(hist, dtype=np.float32)
        if hist.ndim == 2:          # 单样本 [H,S] → [1,H,S]
            hist = hist[np.newaxis]
        B = hist.shape[0]
        h = (hist - self.mu) / self.sd                       # [B,H,S] 归一化
        v_n = (value.astype(np.float32) - self.a_mu) / self.a_sd
        xa = np.stack([type_idx.astype(np.float32), v_n], axis=1)   # [B,2]
        xa_t = torch.tensor(xa, dtype=torch.float32)
        h_t = torch.tensor(h, dtype=torch.float32)
        with torch.no_grad():
            a_emb = self.model.act_emb(xa_t)                 # [B,16]
            out = []
            cur = h_t
            for _ in range(steps):
                a_exp = a_emb.unsqueeze(1).expand(-1, cur.shape[1], -1)
                z, _ = self.model.gru(torch.cat([cur, a_exp], -1))
                y = self.model.head(z[:, -1])                # [B,S] 归一化预测
                out.append(y)
                cur = torch.cat([cur[:, 1:], (y.unsqueeze(1))], 1)  # 滑窗自回归
            y_k = torch.stack(out, dim=1)                    # [B,K,S]
        return (y_k.numpy() * self.sd + self.mu).astype(np.float32)


# ============================================================
# 奖励函数
# ============================================================
@dataclass
class RewardConfig:
    w: np.ndarray = field(default_factory=lambda: DEFAULT_W.copy())
    """[RSRP, SINR, DL, CQI] 权重 (z-score 空间)"""
    lam_action: float = 0.0
    """动作幅值惩罚系数 (归一化 value 距域中心), 0 = 不惩罚"""
    k: int = 8
    """rollout 步数 (×50ms)"""


def make_reward(cfg: RewardConfig):
    """奖励 = w·z(pred) 对 K 步取均值 − lam·|value 距域中心|/half_range"""
    w = cfg.w
    k = cfg.k

    def reward(wm: WorldModel, hist: np.ndarray, type_idx, value) -> np.ndarray:
        pred = wm.rollout(hist, type_idx, value, steps=k)    # [B,K,S]
        z = (pred - wm.mu) / wm.sd                           # z-score
        r = z.mean(axis=1) @ w                               # [B]
        if cfg.lam_action > 0:
            # 幅值正则: 每个动作的归一化偏离
            dev = np.zeros(len(value), dtype=np.float32)
            for i, ti in enumerate(type_idx):
                tname = ACTION_TYPES[int(ti)]
                spec = ACTION_SPACE.get(tname)
                if spec and spec["idx"] != 0:
                    mid = 0.5 * (spec["lo"] + spec["hi"])
                    half = 0.5 * (spec["hi"] - spec["lo"])
                    dev[i] = abs(value[i] - mid) / half
            r = r - cfg.lam_action * dev
        return r

    return reward


# ============================================================
# CEM-MPC 规划器
# ============================================================
def make_action(type_idx: int, value: float) -> dict:
    tname = ACTION_TYPES[type_idx]
    return {"type": tname, "value": float(value)}


@dataclass
class CEMConfig:
    n_iter: int = 4               # CEM 迭代数
    pop: int = 64                 # 每代样本数
    elite: int = 16               # 精英数
    init_sd: float = 0.4          # 初始分布 sd (域比例)
    seed: int = 7


class CEMPlanner:
    """CEM over (type, value): 每种动作类型独立维护高斯分布, 联合竞优。"""

    def __init__(self, wm: WorldModel | None = None, reward=None,
                 cem: CEMConfig | None = None, rcfg: RewardConfig | None = None,
                 device: str = "cpu"):
        self.wm = wm or WorldModel(device=device)
        self.reward = reward or make_reward(rcfg or RewardConfig())
        self.cem = cem or CEMConfig()
        self.rng = np.random.default_rng(self.cem.seed)

    def plan(self, hist_states: np.ndarray, return_all: bool = False) -> dict:
        """hist_states: [H,S] 原始单位 (H=8, S=4), 或 [U,H,S] 场景级 (多 UE 联合规划)
        → 最优动作 dict (动作是场景级全网干预, 多 UE 时奖励取均值)"""
        hist_b = np.asarray(hist_states, dtype=np.float32)
        if hist_b.ndim == 2:
            hist_b = hist_b[np.newaxis]    # [1,H,S]
        U = hist_b.shape[0]
        assert hist_b.shape[-1] == N_STATE, f"state dim {hist_b.shape[-1]} != {N_STATE}"
        assert hist_b.shape[1] == self.wm.hist, f"hist len {hist_b.shape[1]} != {self.wm.hist}"

        def eval_pop(ti_arr: np.ndarray, v_arr: np.ndarray) -> np.ndarray:
            """评估一批动作候选: [P] type_idx + [P] value → [P] 联合奖励。
            每个 UE 独立 rollout 同一动作, 奖励 = 各 UE 均值 (公平联合目标)。"""
            P = len(v_arr)
            # 展开为 [P*U, H, S]: 每个候选 × 每个 UE
            h_rep = np.repeat(hist_b[np.newaxis], P, axis=0).reshape(P * U, *hist_b.shape[1:])
            ti_rep = np.repeat(ti_arr, U)
            v_rep = np.repeat(v_arr, U)
            r = self.reward(self.wm, h_rep, ti_rep, v_rep)   # [P*U]
            return r.reshape(P, U).mean(axis=1)              # [P]

        results = {}
        cem = self.cem
        for tname, spec in ACTION_SPACE.items():
            lo, hi = spec["lo"], spec["hi"]
            mid = 0.5 * (lo + hi)
            half = 0.5 * (hi - lo)
            mu_v, sd_v = mid, cem.init_sd * half
            best_v, best_r = mid, -np.inf
            for _ in range(cem.n_iter):
                vs = np.clip(self.rng.normal(mu_v, sd_v, size=cem.pop), lo, hi)
                ti = np.full(cem.pop, spec["idx"], dtype=np.float32)
                r = eval_pop(ti, vs.astype(np.float32))
                order = np.argsort(-r)
                elites = vs[order[:cem.elite]]
                mu_v = float(elites.mean())
                sd_v = max(float(elites.std()) + 1e-3, 0.05 * half)
                if r[order[0]] > best_r:
                    best_r = float(r[order[0]])
                    best_v = float(vs[order[0]])
            results[tname] = (best_v, best_r)

        # 每类最优 → 全局最优
        best_type = max(results, key=lambda t: results[t][1])
        best_val, best_score = results[best_type]

        # 与 baseline (不干预) 比较: 干预需严格更优才推荐
        r_base = float(eval_pop(np.zeros(1, dtype=np.float32),
                                np.zeros(1, dtype=np.float32))[0])

        out = make_action(ACTION_SPACE[best_type]["idx"], best_val)
        out["score"] = best_score
        out["baseline_score"] = r_base
        out["recommend"] = best_score > r_base
        if return_all:
            out["per_type"] = {t: {"value": v, "score": s} for t, (v, s) in results.items()}
        return out


# ============================================================
# 基线策略
# ============================================================
def plan_random(wm: WorldModel, reward, hist_states: np.ndarray, n: int = 64,
                seed: int = 3) -> dict:
    """随机搜索基线: 均匀采样动作空间取最优。"""
    rng = np.random.default_rng(seed)
    hist = np.asarray(hist_states, dtype=np.float32)
    hist_b = hist[np.newaxis] if hist.ndim == 2 else hist
    vs, tis = [], []
    for tname, spec in ACTION_SPACE.items():
        vs.append(rng.uniform(spec["lo"], spec["hi"], size=n // len(ACTION_SPACE) + 1))
        tis.append(np.full(n // len(ACTION_SPACE) + 1, spec["idx"], dtype=np.float32))
    v = np.concatenate(vs).astype(np.float32)
    ti = np.concatenate(tis)
    B = len(v)
    h_rep = np.repeat(hist_b, B, axis=0)
    r = reward(wm, h_rep, ti, v)
    i = int(np.argmax(r))
    out = make_action(int(ti[i]), float(v[i]))
    out["score"] = float(r[i])
    return out


def plan_greedy_1step(wm: WorldModel, reward, hist_states: np.ndarray,
                      n: int = 64, seed: int = 3) -> dict:
    """单步贪心: 同 plan_random 但 reward 用 k=1 (短视)。"""
    # 简化实现: 直接构造 k=1 的 reward (rollout 1 步)
    hist = np.asarray(hist_states, dtype=np.float32)
    hist_b = hist[np.newaxis] if hist.ndim == 2 else hist
    rng = np.random.default_rng(seed)
    vs, tis = [], []
    for tname, spec in ACTION_SPACE.items():
        vs.append(rng.uniform(spec["lo"], spec["hi"], size=n // len(ACTION_SPACE) + 1))
        tis.append(np.full(n // len(ACTION_SPACE) + 1, spec["idx"], dtype=np.float32))
    v = np.concatenate(vs).astype(np.float32)
    ti = np.concatenate(tis)
    B = len(v)
    h_rep = np.repeat(hist_b, B, axis=0)
    pred = wm.rollout(h_rep, ti, v, steps=1)
    z = (pred[:, 0] - wm.mu) / wm.sd
    r = z @ DEFAULT_W
    i = int(np.argmax(r))
    out = make_action(int(ti[i]), float(v[i]))
    out["score"] = float(r[i])
    return out


# ============================================================
# Demo / 自测
# ============================================================
def _demo():
    from .data import load_branches

    print("[demo] 加载世界模型 + 同根分支数据 ...")
    wm = WorldModel()
    planner = CEMPlanner(wm=wm)

    groups = load_branches()
    print(f"  {len(groups)} 组同根分支")

    # 取每组基准分支的 8s 处历史窗 (训练/测试边界附近)
    t_idx = int(8000 / 50) - HIST

    # --- 1. 单 UE 规划示例 (前 4 组) ---
    print("\n--- 单 UE 规划 (per-UE 自私视角) ---")
    for g in groups[:4]:
        hist = g.baseline.states[t_idx:t_idx + HIST]       # [8,4] 原始单位
        res = planner.plan(hist, return_all=True)
        rec = "✓干预" if res["recommend"] else "✗保持"
        print(f"  [{g.scene}/UE{g.ue}] {rec} {res['type']}={res['value']:.3f} "
              f"(score {res['score']:+.3f} vs base {res['baseline_score']:+.3f})")

    # --- 2. 场景级联合规划 (8 UE 均值奖励; 动作是全网干预) ---
    print("\n--- 场景级联合规划 (多 UE 公平) ---")
    by_scene: dict[str, list] = {}
    for g in groups:
        by_scene.setdefault(g.scene, []).append(g)
    for scene, gs in by_scene.items():
        hist_u = np.stack([g.baseline.states[t_idx:t_idx + HIST] for g in gs])  # [U,8,4]
        res = planner.plan(hist_u, return_all=True)
        rec = "✓干预" if res["recommend"] else "✗保持"
        per = " ".join(f"{t}={d['value']:.2f}" for t, d in res["per_type"].items())
        print(f"  [{scene} ×{len(gs)}UE] {rec} {res['type']}={res['value']:.3f} "
              f"(score {res['score']:+.3f} vs base {res['baseline_score']:+.3f})")
        print(f"      per-type: {per}")

    # --- 3. 基线策略对比 (同一历史窗) ---
    g = groups[0]
    hist = g.baseline.states[t_idx:t_idx + HIST]
    cem_res = planner.plan(hist)
    rnd_res = plan_random(wm, planner.reward, hist)
    gr_res = plan_greedy_1step(wm, planner.reward, hist)
    print("\n--- 策略对比 (同历史窗, WM 内奖励) ---")
    print(f"  CEM-MPC:    {cem_res['type']}={cem_res['value']:.3f} score={cem_res['score']:+.3f}")
    print(f"  Random:     {rnd_res['type']}={rnd_res['value']:.3f} score={rnd_res['score']:+.3f}")
    print(f"  Greedy-1:   {gr_res['type']}={gr_res['value']:.3f} score={gr_res['score']:+.3f}")
    print(f"  NoAction:   baseline score={cem_res['baseline_score']:+.3f}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    if args.demo:
        _demo()
    else:
        ap.print_help()
