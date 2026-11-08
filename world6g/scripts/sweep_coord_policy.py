#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
coord policy 部署头 (score vs value) 选型 sweep — 会话 29 辅助
===============================================================
对 train_coord_policy.py 的 LOSO 评测在多训练 seed 下重复, 得到两头的
regret / 实现率分布, 用于无泄漏地选部署头 (不在单次 25 上下文结果上挑头)。

方法: 复用 train_coord_policy 的函数, 对每个训练 seed s ∈ {177..186}:
  - 5 折 LOSO (同折划分), 双头各自汇总 regret_mean / exact_rate / 实现率;
  - 汇总 10 个 seed × 两头的均值±std, 输出 JSON。
只读数据集, 不写模型/不覆盖权威产物 (产物仍由 train_coord_policy.py 生成)。
"""
import sys
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from datalake.config import GEN_DIR                      # noqa: E402
from train_coord_policy import (                          # noqa: E402
    SCENES, GRID, SEED_OFFSETS, load_contexts, build_features,
    build_targets, train_one_fold, eval_fold)

import torch  # noqa: E402


def seed_offset_of(cid: str) -> int:
    scene, seed = cid.split("|")
    return int(seed) - (9000 + SCENES.index(scene) * 13)


def main():
    t0 = time.time()
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    df = pd.read_parquet(GEN_DIR / "coord_action_dataset.parquet")
    base = load_contexts(df)
    X, _ = build_features(base)
    Y, combos = build_targets(df, base)
    best_idx = Y.argmax(1)
    base_idx = 62
    assert combos[base_idx] == "+0,+0,+0"

    ctx_index = list(base.index)
    ctx_index.sort(key=lambda c: (SCENES.index(c.split("|")[0]),
                                  seed_offset_of(c)))
    pos = {c: i for i, c in enumerate(base.index)}
    remap = [pos[c] for c in ctx_index]
    X, Y, best_idx = X[remap], Y[remap], Y.argmax(1)
    n_ctx = len(ctx_index)
    folds_te = [np.array([i for i, c in enumerate(ctx_index)
                          if seed_offset_of(c) == SEED_OFFSETS[k]])
                for k in range(5)]

    per_seed = []
    for s in range(177, 187):
        regs_s, regs_v, lifts_s, lifts_v = [], [], [], []
        for k in range(5):
            te = folds_te[k]
            tr = np.setdiff1d(np.arange(n_ctx), te)
            net, _ = train_one_fold(X, Y, best_idx, tr, s + k,
                                    300, 3e-4, device)
            ev = eval_fold(net, X, Y, te, device)
            regs_s += ev["regret"]
            regs_v += ev["val_head"]["regret"]
            lifts_s += ev["lift_vs_base"]
            lifts_v += ev["val_head"]["lift_vs_base"]
        rs, rv = np.array(regs_s), np.array(regs_v)
        ls, lv = np.array(lifts_s), np.array(lifts_v)
        oracle = float((Y.max(1) - Y[:, base_idx]).mean())
        row = {
            "train_seed": s,
            "score": {"regret_mean": round(float(rs.mean()), 4),
                      "exact_rate": round(float((rs < 1e-6).mean()), 4),
                      "realize_ratio": round(float(ls.mean() / oracle), 4)},
            "value": {"regret_mean": round(float(rv.mean()), 4),
                      "exact_rate": round(float((rv < 1e-6).mean()), 4),
                      "realize_ratio": round(float(lv.mean() / oracle), 4)},
        }
        per_seed.append(row)
        print(f"[seed {s}] score regret {row['score']['regret_mean']:+.4f} "
              f"realize {row['score']['realize_ratio']:.1%} | "
              f"value regret {row['value']['regret_mean']:+.4f} "
              f"realize {row['value']['realize_ratio']:.1%}")

    def agg(key):
        v = np.array([r[key]["regret_mean"] for r in per_seed])
        e = np.array([r[key]["exact_rate"] for r in per_seed])
        rr = np.array([r[key]["realize_ratio"] for r in per_seed])
        return {"regret_mean": round(float(v.mean()), 4),
                "regret_mean_std": round(float(v.std()), 4),
                "exact_rate": round(float(e.mean()), 4),
                "realize_ratio": round(float(rr.mean()), 4),
                "realize_ratio_std": round(float(rr.std()), 4)}

    out = {
        "objective": "coord policy 双头 (score/value) 部署头选型 sweep",
        "n_train_seeds": 10, "epochs": 300,
        "agg": {"score": agg("score"), "value": agg("value")},
        "per_seed": per_seed,
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    (GEN_DIR / "coord_policy_head_sweep.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[sweep] score: {out['agg']['score']}")
    print(f"[sweep] value: {out['agg']['value']}")
    print(f"[done] coord_policy_head_sweep.json | {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
