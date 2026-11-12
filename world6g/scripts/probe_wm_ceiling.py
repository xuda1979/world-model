#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时序 WM v3 特征天花板探针 (任务 #6, 1111 报告余项 #3)
======================================================
改 NN 之前, 先用 HistGradientBoostingRegressor (强非线性, 免调参) 估计
不同特征集下 heldout reward-R² 的可达上限, 并把 reward 分解为
win_obj[t+1] (动作可影响) 与 ref_obj[t+1] (纯外生) 分别测可预测性。

协议与 train_coord_seq_policy.py v2 的 5 折 LOSO 完全一致:
  转移 = 每 episode 内 t → t+1 (9 条/ep, 共 8100), 折 = seed offset。
池化 R² = 5 折 heldout 预测拼接后统一 r2_score, 与 v2 meta 的
loso_heldout_reward.reward_r2 (0.5337) 同口径可比。

特征集 (行 = 转移 t→t+1):
  base(30)  : ctx[t](10) + act_oh[t](15) + scene_oh(5)        ← v2 WM 输入
  v3(47)    : base + win_obj[t](1) + dec[t]/9(1) + prev_act_oh(15)
  oracle    : ctx[t+1](10) + win_obj[t+1](1) + act_oh + scene ← 下窗全知上界
"""
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.gen_coord_seqs import GRID, SCENES, SEED_OFFSETS  # noqa: E402

FX = ["sinr_c0", "n_ue_c0", "sinr_c1", "n_ue_c1", "sinr_c2", "n_ue_c2",
      "l3_margin_c0", "l3_margin_c1", "l3_margin_c2", "mean_sinr"]
G5 = len(GRID)
COMBOS = list(itertools.product(GRID, repeat=3))
COMBO_IDX = {c: i for i, c in enumerate(COMBOS)}
ZERO_IDX = COMBO_IDX[(0.0, 0.0, 0.0)]


def act_oh(a_idx):
    a_idx = np.asarray(a_idx, dtype=np.int64)
    n = len(a_idx)
    oh = np.zeros((n, 15), dtype=np.float32)
    d0 = (a_idx // (G5 * G5)) % G5
    d1 = (a_idx // G5) % G5
    d2 = a_idx % G5
    oh[np.arange(n), d0] = 1.0
    oh[np.arange(n), G5 + d1] = 1.0
    oh[np.arange(n), 2 * G5 + d2] = 1.0
    return oh


def build():
    df = pd.read_parquet(ROOT / "datalake" / "generated"
                         / "coord_seq_dataset.parquet")
    s_map = {s: i for i, s in enumerate(SCENES)}
    rows = {k: [] for k in
            ("X", "XN", "A", "PA", "D", "S", "F", "R", "W", "WN", "REF")}
    for (sc, sd, _ei), g in df.groupby(["scene", "seed", "ep_i"], sort=True):
        g = g.sort_values("dec_idx")
        x = g[FX].to_numpy(np.float32)
        w = g["win_obj"].to_numpy(np.float64)
        ref = g["ref_obj"].to_numpy(np.float64)
        r = g["reward"].to_numpy(np.float64)
        ai = np.array([COMBO_IDX[tuple(v)] for v in
                       g[["p0", "p1", "p2"]].to_numpy(float)], dtype=np.int64)
        si = s_map[sc]
        fold = SEED_OFFSETS.index(int(sd) - (9000 + 13 * si))
        for t in range(len(g) - 1):
            rows["X"].append(x[t])
            rows["XN"].append(x[t + 1])
            rows["A"].append(ai[t])
            rows["PA"].append(ai[t - 1] if t >= 1 else ZERO_IDX)
            rows["D"].append(t / (len(g) - 1))
            rows["S"].append(si)
            rows["F"].append(fold)
            rows["R"].append(r[t + 1])
            rows["W"].append(w[t])
            rows["WN"].append(w[t + 1])
            rows["REF"].append(ref[t + 1])
    return {k: np.array(v) for k, v in rows.items()}


def gbm():
    return HistGradientBoostingRegressor(
        max_iter=400, learning_rate=0.08, early_stopping=True,
        validation_fraction=0.15, random_state=0)


def run_loso(name, feats, y, F, out):
    t0 = time.time()
    preds = np.empty(len(y), dtype=np.float64)
    per_fold = []
    for k in range(5):
        tr = F != k
        te = ~tr
        m = gbm()
        m.fit(feats[tr], y[tr])
        preds[te] = m.predict(feats[te])
        per_fold.append(float(r2_score(y[te], preds[te])))
    pooled = float(r2_score(y, preds))
    mae = float(mean_absolute_error(y, preds))
    print(f"  {name:32s} pooled R2={pooled:+.4f}  MAE={mae:7.3f}  "
          f"per-fold={[round(v, 3) for v in per_fold]}  "
          f"({time.time() - t0:.0f}s)", flush=True)
    out["sets"][name] = {
        "pooled_r2": pooled, "mae": mae, "per_fold": per_fold,
        "n_transitions": int(len(y))}
    return pooled


def main():
    d = build()
    X, XN, A, PA, D, S, F = (d["X"], d["XN"], d["A"], d["PA"],
                             d["D"], d["S"], d["F"])
    R, W, WN, REF = d["R"], d["W"], d["WN"], d["REF"]
    scene_oh = np.eye(5, dtype=np.float32)[S.astype(np.int64)]
    a_oh = act_oh(A)
    pa_oh = act_oh(PA)
    base = np.hstack([X, a_oh, scene_oh]).astype(np.float32)
    v3 = np.hstack([base, W[:, None], D[:, None], pa_oh]).astype(np.float32)
    oracle_ctx = np.hstack([XN, a_oh, scene_oh]).astype(np.float32)
    oracle = np.hstack([XN, WN[:, None], a_oh, scene_oh]).astype(np.float32)

    out = {
        "desc": "时序 WM v3 特征天花板探针 (GBM 5 折 LOSO, 折=seed offset, "
                "协议同 train_coord_seq_policy.py)",
        "n_transitions": int(len(R)),
        "metrics": {
            "reward_std": float(R.std()), "win_t1_std": float(WN.std()),
            "ref_t1_std": float(REF.std()),
            "var_ref_over_var_reward": float(REF.var() / R.var()),
            "corr_win_ref": float(np.corrcoef(WN, REF)[0, 1]),
            "corr_win_t_win_t1": float(np.corrcoef(W, WN)[0, 1]),
            "corr_ref_t1_reward_t1": float(np.corrcoef(REF, R)[0, 1]),
            "v2_nn_baseline_reward_r2": 0.5337,  # coord_seq_wm_meta.json
        },
        "sets": {},
    }
    print(f"[ceiling] 转移 {len(R)} 条 | reward std={R.std():.3f} "
          f"win[t+1] std={WN.std():.3f} ref[t+1] std={REF.std():.3f}")
    print(f"[ceiling] Var(ref)/Var(reward)={REF.var() / R.var():.3f} "
          f"| Corr(win[t+1], ref[t+1])={np.corrcoef(WN, REF)[0, 1]:+.3f}")
    print(f"[ceiling] Corr(win[t], win[t+1])={np.corrcoef(W, WN)[0, 1]:+.3f} "
          f"| Corr(ref[t+1], reward[t+1])="
          f"{np.corrcoef(REF, R)[0, 1]:+.3f}")
    print("[ceiling] v2 NN 基线: pooled heldout reward R2 = 0.5337 "
          "(coord_seq_wm_meta.json)")
    print("[ceiling] GBM 5 折 LOSO:")
    run_loso("base(30)→reward [v2 输入]", base, R, F, out)
    run_loso("v3(47)→reward", v3, R, F, out)
    run_loso("v3(47)→win_obj[t+1]", v3, WN, F, out)
    run_loso("v3(47)→ref_obj[t+1]", v3, REF, F, out)
    run_loso("oracle_ctx(30)→reward", oracle_ctx, R, F, out)
    run_loso("oracle(31)→reward [上界]", oracle, R, F, out)

    dst = ROOT / "datalake" / "generated" / "wm_ceiling_probe.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    print(f"OK: 天花板探针完成。结果写入 {dst.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
