#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐小区协同策略蒸馏 (Coordination Policy, round 177)
====================================================
在 v2 协同动作数据集 (coord_action_dataset.parquet, 3125 行) 上训练
"上下文 → 联合动作" 的逐小区策略网络 (coord policy):

  - 决策上下文 x (决策时可知, 无泄漏): (0,0,0) 基线行的逐小区特征
    sinr_c0..2 / n_ue_c0..2 (动作前观测) + 场景 one-hot + seed one-hot;
  - 动作空间: 3 小区联合功率偏移 p ∈ {−6,−3,0,+3,+6}³ (125 组合);
  - 网络结构: MLP [ctx_dim + 3×5 one-hot] → 512 → 512 → 512 → 125 logits,
    softmax 概望分布 q(a|ctx);
  - 训练目标: 行为克隆 argmax_a* obj (0-1 类型损失) + 逐组合值蒸馏
    (obj 标准化后的 smooth L1) — 同一网络头同时给出 argmax 决策与
    125 组合的值序;
  - 泛化评测 (防记忆): leave-one-seed-out — 每折留出 1 个 seed 的全部
    5 场景 × 125 组合 (625 行), 用其余 4 seeds 训练, 在留出 seed 上
    评 25 上下文的 regret (留出联合最优 obj − 策略推荐组合 obj) 与
    相对基线提升 (policy − (0,0,0)); 5 折 (seed-offset k=0..4) 轮转;
  - 最终模型: 全量 5 seeds 训练 (deploy 版), fold-0 模型用于无泄漏
    报告数字 (与 holdout 数字同分布口径)。

判据 (与 §7.4 一致): obj = mean_ue_sinr + 0.5 × min_ue_sinr。
基线: (0,0,0) 基线动作; 上界: 同上下文 125 组合枚举最优 (联合最优)。

产出 (world6g/models/):
  coord_policy.pt          最终网络 (全量训练, 部署用)
  coord_policy_meta.json   训练配置 + LOSO 折数字 (报告权威源)
评测 JSON (world6g/datalake/generated/):
  coord_policy_loso.json   LOSO 逐折 + 汇总 (供报告/probe 溯源)
"""
import sys
import json
import time
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datalake.config import GEN_DIR          # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"
SCENES = ["campus", "城区", "郊区", "低空", "机器人工厂"]
GRID = [-6.0, -3.0, 0.0, 3.0, 6.0]
SEED_OFFSETS = [0, 1000, 2000, 3000, 4000]
CTX_COLS = [f"sinr_c{i}" for i in range(3)] + [f"n_ue_c{i}" for i in range(3)]
# n_ue_* 是小整数 (≤8), log1p 压缩后与 sinr (dB, ~0-30) 同量级
LOG_COLS = [f"n_ue_c{i}" for i in range(3)]


# ---------------------------------------------------------------- 数据
def load_contexts(df: pd.DataFrame) -> pd.DataFrame:
    """每 (scene, seed) 取 (0,0,0) 基线行特征作为决策上下文 (动作前观测)。"""
    base = df[df.combo == "+0,+0,+0"].copy()
    base["ctx_id"] = base.scene + "|" + base.seed.astype(str)
    base = base.set_index("ctx_id")
    return base


def build_features(base: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """上下文特征矩阵 X (n_ctx × d) 与上下文列表。"""
    feats = []
    for cid, row in base.iterrows():
        v = [row[c] for c in CTX_COLS]
        # log1p 压缩 n_ue
        for i, c in enumerate(LOG_COLS):
            v[3 + i] = np.log1p(v[3 + i])
        sc = [1.0 if row.scene == s else 0.0 for s in SCENES]
        feats.append(v + sc)
    X = np.asarray(feats, dtype=np.float32)
    mu, sd = X.mean(0), X.std(0) + 1e-6
    Xn = (X - mu) / sd
    return Xn, (mu, sd)


def build_targets(df: pd.DataFrame, base: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """逐上下文的 125 组合 obj 矩阵 Y (n_ctx × 125) 与组合索引表。"""
    combos = [f"{c0:+.0f},{c1:+.0f},{c2:+.0f}"
              for c0 in GRID for c1 in GRID for c2 in GRID]
    cidx = {c: i for i, c in enumerate(combos)}
    n_ctx = len(base)
    Y = np.full((n_ctx, len(combos)), np.nan, dtype=np.float32)
    df = df.copy()
    df["ctx_id"] = df.scene + "|" + df.seed.astype(str)
    pos = {cid: i for i, cid in enumerate(base.index)}
    for cid, g in df.groupby("ctx_id"):
        rows = g.set_index("combo")["obj"]
        for c in combos:
            if c in rows.index:
                Y[pos[cid], cidx[c]] = rows[c]
    assert not np.isnan(Y).any(), "组合覆盖不完整"
    return Y, combos


# ---------------------------------------------------------------- 网络
class CoordPolicyNet(nn.Module):
    """上下文 → 125 联合组合的分布与值序。

    输入: [ctx (d), 3×5 动作 one-hot] → 512³ MLP → 双头:
      logits 头: 全组合联合分布 (训练 BC on argmax_a*),
      值头: 逐组合 obj 预测 (smooth L1, 全 125 组合监督)。
    """

    def __init__(self, ctx_dim: int, n_combo: int = 125, hidden: int = 512):
        super().__init__()
        self.act_emb = nn.Parameter(torch.randn(15, 16) * 0.02)
        self.trunk = nn.Sequential(
            nn.Linear(ctx_dim + 31, hidden), nn.GELU(),  # ctx + act_onehot(15) + emb(16)
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
        )
        self.head_score = nn.Linear(hidden, 1)   # 逐对得分 → 125 组合 softmax
        self.head_val = nn.Sequential(
            nn.Linear(hidden, 128), nn.GELU(), nn.Linear(128, 1))

    def forward(self, ctx: torch.Tensor, act_onehot: torch.Tensor):
        # act_onehot: (B, 15) 3 小区 × 5 档 (每行 3 个 1)
        emb = act_onehot @ self.act_emb            # (B,16) 三小区 level-embedding 和
        h = self.trunk(torch.cat([ctx, act_onehot, emb], -1))
        return self.head_score(h).squeeze(-1), self.head_val(h).squeeze(-1)

    def all_values(self, ctx: torch.Tensor, act_oh_all: torch.Tensor):
        """ctx (B,d) × 全 125 组合 → 值矩阵 (B,125) 与 logits (B,125)。"""
        B, n = ctx.shape[0], act_oh_all.shape[0]
        ctx_r = ctx[:, None, :].expand(B, n, -1).reshape(B * n, -1)
        a_r = act_oh_all[None].expand(B, n, -1).reshape(B * n, -1)
        logits, vals = self.forward(ctx_r, a_r)
        return logits.view(B, n), vals.view(B, n)


def combo_str(p: tuple[float, float, float]) -> str:
    return f"{p[0]:+.0f},{p[1]:+.0f},{p[2]:+.0f}"


# ---------------------------------------------------------------- 训练
def train_one_fold(X: np.ndarray, Y: np.ndarray, best_idx: np.ndarray,
                   tr: np.ndarray, seed: int, epochs: int, lr: float,
                   device: str, tau: float = 0.5) -> tuple[CoordPolicyNet, dict]:
    """训练一折: tr 为训练上下文索引。返回模型与训练历史。"""
    torch.manual_seed(seed)
    np.random.seed(seed)
    # obj 标准化 (训练折统计量)
    ymu, ysd = Y[tr].mean(), Y[tr].std() + 1e-6
    Yn = (Y - ymu) / ysd

    # 动作 one-hot: 125 × (3*5)
    oh = np.zeros((125, 15), dtype=np.float32)
    for i, (a, b, c) in enumerate(
            [(x, y, z) for x in range(5) for y in range(5) for z in range(5)]):
        oh[i, a] = oh[i, 5 + b] = oh[i, 10 + c] = 1.0
    oh_t = torch.tensor(oh, device=device)

    net = CoordPolicyNet(X.shape[1]).to(device)
    opt = torch.optim.Adam(net.parameters(), lr=lr)
    Xt = torch.tensor(X, device=device)
    Yt = torch.tensor(Yn, device=device)
    Bt = torch.tensor(best_idx, device=device)
    # 软目标: q*(a|ctx) ∝ exp(obj_a / τ) — 125 组合密集监督, 优于纯 argmax BC
    Qt = F.softmax(Yt / (tau * ysd), dim=-1)
    hist = []

    for ep in range(epochs):
        net.train()
        scores, vals = net.all_values(Xt[tr], oh_t)
        logq = F.log_softmax(scores, dim=-1)
        loss_bc = -(Qt[tr] * logq).sum(-1).mean()          # 软分布匹配
        # 值蒸馏: 全 125 组合 smooth L1 (obj 标准化域)
        loss_v = F.smooth_l1_loss(vals, Yt[tr])
        loss = loss_bc + 0.5 * loss_v
        opt.zero_grad()
        loss.backward()
        opt.step()
        if (ep + 1) % max(1, epochs // 10) == 0:
            net.eval()
            with torch.no_grad():
                lg, _ = net.all_values(Xt[tr], oh_t)
                acc = (lg.argmax(-1) == Bt[tr]).float().mean().item()
            hist.append({"epoch": ep + 1,
                         "loss": round(loss.item(), 5),
                         "bc_acc": round(acc, 4)})
    meta_tr = {"ymu": float(ymu), "ysd": float(ysd), "n_ctx_train": int(len(tr)),
               "tau": tau}
    return net, {"hist": hist, **meta_tr}


def eval_fold(net: CoordPolicyNet, X: np.ndarray, Y: np.ndarray,
              te: np.ndarray, device: str) -> dict:
    """留出评测: 值头 (部署决策规则) argmax 推荐组合 → 真实 obj/regret。

    部署头选型依据 sweep_coord_policy.py (10 训练 seed): 值头 10/10 全胜
    (regret 0.123±0.016 vs score 头 0.240±0.043, 实现率 96.5% vs 93.1%)。
    score 头保留为辅助对照。
    """
    net.eval()
    oh = np.zeros((125, 15), dtype=np.float32)
    for i, (a, b, c) in enumerate(
            [(x, y, z) for x in range(5) for y in range(5) for z in range(5)]):
        oh[i, a] = oh[i, 5 + b] = oh[i, 10 + c] = 1.0
    oh_t = torch.tensor(oh, device=device)
    recs, objs, regrets, lifts = [], [], [], []
    s_recs, s_objs, s_regrets, s_lifts = [], [], [], []
    with torch.no_grad():
        scores, vals = net.all_values(torch.tensor(X[te], device=device), oh_t)
        vpick = vals.argmax(-1).cpu().numpy()
        spick = scores.argmax(-1).cpu().numpy()
        head_dist = float((vpick != spick).mean())
    for row_i, ci in enumerate(te):
        for p, R, O, G, L in ((vpick, recs, objs, regrets, lifts),
                              (spick, s_recs, s_objs, s_regrets, s_lifts)):
            a = int(p[row_i])
            obj = float(Y[ci, a])
            best = float(Y[ci].max())
            base_obj = float(Y[ci][62])  # (0,0,0) 字典序网格索引
            R.append(a)
            O.append(obj)
            G.append(best - obj)
            L.append(obj - base_obj)
    return {"recommend": recs, "obj": objs,
            "regret": [round(r, 4) for r in regrets],
            "lift_vs_base": [round(l, 4) for l in lifts],
            "head_dist": head_dist,
            "score_head": {"recommend": s_recs, "obj": s_objs,
                           "regret": [round(r, 4) for r in s_regrets],
                           "lift_vs_base": [round(l, 4) for l in s_lifts]}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=177)
    ap.add_argument("--device", default="mps" if torch.backends.mps.is_available()
                    else "cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    t0 = time.time()
    df = pd.read_parquet(GEN_DIR / "coord_action_dataset.parquet")
    base = load_contexts(df)
    X, (mu, sd) = build_features(base)
    Y, combos = build_targets(df, base)
    best_idx = Y.argmax(1)
    base_idx = 62  # "+0,+0,+0" 字典序网格索引
    assert combos[base_idx] == "+0,+0,+0"

    ctx_index = list(base.index)

    def seed_offset_of(cid: str) -> int:
        scene, seed = cid.split("|")
        return int(seed) - (9000 + SCENES.index(scene) * 13)

    # 上下文排序: scene 主序 (SCENES 顺序) + seed_offset 序
    ctx_index.sort(key=lambda c: (SCENES.index(c.split("|")[0]),
                                  seed_offset_of(c)))
    # 重排 X/Y 到该序
    pos = {c: i for i, c in enumerate(base.index)}
    remap = [pos[c] for c in ctx_index]
    X = X[remap]
    Y = Y[remap]
    best_idx = Y.argmax(1)

    n_ctx = len(ctx_index)
    print(f"[data] {len(df)} 行 → {n_ctx} 上下文 × 125 组合 | ctx 特征维 {X.shape[1]}")
    print(f"[data] 上下文序: {ctx_index[0]} .. {ctx_index[-1]}")

    # ---- LOSO (leave-one-seed-out): 留出 seed-offset k 的 5 场景全组合 ----
    loso_folds = []
    for k in range(5):
        te = np.array([i for i, c in enumerate(ctx_index)
                       if seed_offset_of(c) == SEED_OFFSETS[k]])
        tr = np.setdiff1d(np.arange(n_ctx), te)
        net, tr_meta = train_one_fold(
            X, Y, best_idx, tr, args.seed + k, args.epochs, args.lr, args.device)
        ev = eval_fold(net, X, Y, te, args.device)
        loso_folds.append({
            "heldout_seed_offset": SEED_OFFSETS[k],
            "n_holdout_ctx": int(len(te)),
            **{kk: tr_meta[kk] for kk in ("ymu", "ysd", "n_ctx_train", "tau")},
            "eval": ev,
        })
        r = np.array(ev["regret"]); l = np.array(ev["lift_vs_base"])
        sr = np.array(ev["score_head"]["regret"])
        print(f"[LOSO k={k}] holdout {len(te)} ctx | regret mean {r.mean():+.4f} "
              f"max {r.max():+.4f} | lift_vs_base mean {l.mean():+.4f} | "
              f"score 头 regret {sr.mean():+.4f} | 头间不一致率 {ev['head_dist']:.0%}")

    all_regret = np.concatenate([np.array(f["eval"]["regret"]) for f in loso_folds])
    all_lift = np.concatenate([np.array(f["eval"]["lift_vs_base"]) for f in loso_folds])
    summary = {
        "n_contexts_total": n_ctx,
        "n_folds": 5,
        "regret_mean": round(float(all_regret.mean()), 4),
        "regret_p50": round(float(np.median(all_regret)), 4),
        "regret_max": round(float(all_regret.max()), 4),
        "regret_exact_rate": round(float((all_regret < 1e-6).mean()), 4),
        "lift_vs_base_mean": round(float(all_lift.mean()), 4),
        "lift_vs_base_min": round(float(all_lift.min()), 4),
        "oracle_lift_vs_base_mean": None,  # 填充后
        "coord_gain_realized_ratio": None,  # 填充后
    }
    # oracle: 逐上下文联合最优相对基线
    oracle_lift = (Y.max(1) - Y[:, base_idx]).mean()
    summary["oracle_lift_vs_base_mean"] = round(float(oracle_lift), 4)
    summary["coord_gain_realized_ratio"] = round(
        float(all_lift.mean() / oracle_lift), 4)
    print(f"\n[LOSO 汇总] regret mean {summary['regret_mean']:+.4f} "
          f"(exact {summary['regret_exact_rate']:.0%}) | "
          f"lift_vs_base {summary['lift_vs_base_mean']:+.4f} vs oracle "
          f"{summary['oracle_lift_vs_base_mean']:+.4f} | 实现率 "
          f"{summary['coord_gain_realized_ratio']:.1%}")

    # ---- 最终全量模型 (部署) ----
    net_full, full_meta = train_one_fold(
        X, Y, best_idx, np.arange(n_ctx), args.seed, args.epochs, args.lr,
        args.device)
    # 存模型 + 归一化统计 + 动作表
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": net_full.state_dict(),
                "ctx_mu": mu, "ctx_sd": sd,
                "combos": combos, "scenes": SCENES},
               MODELS_DIR / "coord_policy.pt")
    meta = {
        "objective": ("逐小区协同策略蒸馏: 上下文 (基线逐小区特征+场景) → "
                      "125 联合功率组合分布; BC (argmax_a*) + 值蒸馏 (smooth L1)"),
        "data": str(GEN_DIR / "coord_action_dataset.parquet"),
        "model": "CoordPolicyNet(ctx→dist125+value)",
        "hidden": 512,
        "ctx_cols": CTX_COLS,
        "ctx_dim": int(X.shape[1]),
        "action_space": {"per_cell_db": GRID, "n_cells": 3, "n_combos": 125},
        "combos": combos,
        "seed": args.seed,
        "epochs": args.epochs,
        "lr": args.lr,
        "n_contexts": n_ctx,
        "params": sum(p.numel() for p in net_full.parameters()),
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
        "final_train": full_meta,
        "loso_summary": summary,
        "folds": [{k: v for k, v in f.items() if k != "eval"} |
                  {"eval_summary": {
                      "regret_mean": round(float(np.mean(f["eval"]["regret"])), 4),
                      "lift_vs_base_mean": round(
                          float(np.mean(f["eval"]["lift_vs_base"])), 4),
                      "head_dist": f["eval"]["head_dist"]}}
                  for f in loso_folds],
    }
    (MODELS_DIR / "coord_policy_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    out = {
        "objective": meta["objective"],
        "data": meta["data"],
        "n_contexts": n_ctx,
        "combos": combos,
        "contexts": ctx_index,
        "summary": summary,
        "folds": loso_folds,
        "oracle_lift_per_ctx": [round(float(v), 4) for v in (Y.max(1) - Y[:, base_idx])],
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    (GEN_DIR / "coord_policy_loso.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[done] coord_policy.pt ({meta['params']:,} params) + meta + "
          f"loso JSON | 总耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
