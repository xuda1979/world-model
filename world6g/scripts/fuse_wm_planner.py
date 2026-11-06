#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
融合奖励实验: λ·MS + (1−λ)·单步 — 能否兼得保真度与进攻性?
============================================================
背景 (见 ablation_report.md §4 改进路径): 消融显示 v0.3-MS (多步一致性)
闭环 lift +0.285 但决策保真度 +0.688, 单步 v0.2 lift +0.456 但保真度 +0.390。
本实验在相同协议 (96 held-out 窗, twin 同根执行, MS 奖励尺度) 下扫融合系数
λ ∈ {1.0, 0.7, 0.5, 0.3, 0.0}, 看是否存在 λ 让闭环 lift 接近 0.456
同时保真度接近 0.688。

融合奖励 (planner 内联合目标, 各 WM 在自身 z 空间打分):
    r_fuse = λ · r_MS(hist) + (1−λ) · r_v2(hist)
其中 r(wm) = mean_k[ (rollout_k − wm.mu) / wm.sd ] @ w   (与 make_reward 同构)

用法: ../.venv/bin/python scripts/fuse_wm_planner.py [--n-per-scene 24] [--seed 2026]
输出: docs/closedloop/fuse_wm_planner.json
"""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wm import data as D                                    # noqa: E402
from wm.planner import (WorldModel, CEMPlanner, CEMConfig, ACTION_SPACE,
                        ACTION_TYPES, make_action, DEFAULT_W)  # noqa: E402

SCENES = ["城区", "郊区", "低空", "机器人工厂"]

_spec = importlib.util.spec_from_file_location(
    "ec", ROOT / "scripts" / "eval_closedloop.py")
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)


class FusedReward:
    """λ·r_MS + (1−λ)·r_v2 — 各 WM 在自身 z 空间打分, 再在 96 窗上同一 k=8。"""
    def __init__(self, wm_ms: WorldModel, wm_v2: WorldModel, lam: float, k: int = 8):
        self.ms_ = wm_ms
        self.v2_ = wm_v2
        self.lam = lam
        self.k = k
        self.w = DEFAULT_W

    def __call__(self, wm, hist: np.ndarray, type_idx, value) -> np.ndarray:
        # 接口自带 wm (评估时用主模型), 但融合需两模型各自 rollout
        B = hist.shape[0]
        v_n = np.zeros(B, dtype=np.float32)
        for i, val in enumerate(value):
            v_n[i] = val
        r_ms = make_reward_raw(self.ms_, hist, type_idx, v_n, self.k, self.w)
        r_v2 = make_reward_raw(self.v2_, hist, type_idx, v_n, self.k, self.w)
        return self.lam * r_ms + (1 - self.lam) * r_v2


def make_reward_raw(wm, hist, type_idx, value, k, w):
    """单个 WM 在自身 z 空间的 k 步 rollout 奖励 (与 make_reward 同构, 无动作惩罚)"""
    pred = wm.rollout(np.asarray(hist, np.float32), type_idx, value, steps=k)
    z = (pred - wm.mu) / wm.sd
    return z.mean(axis=1) @ w


def run_fused(name, hist_states, wm_ms, wm_v2, lam, **kw):
    """单窗融合规划: 用融合奖励跑 CEM, 返回 (atype, value, score)。"""
    frw = FusedReward(wm_ms, wm_v2, lam, k=ec.K_HORIZON)
    pl = CEMPlanner(wm=wm_ms, reward=frw)
    res = pl.plan(hist_states[np.newaxis], return_all=False)
    if res["recommend"]:
        return res["type"], float(res["value"]), float(res["score"])
    return "none", 0.0, float(res["baseline_score"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-scene", type=int, default=24)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    t0clock = time.time()
    torch.manual_seed(0)
    ds = pd.read_parquet(ROOT / "datalake" / "generated" / "twin_action_dataset.parquet")
    groups = D.load_branches(ds)
    by = {(g.scene, int(g.ue)): g for g in groups}
    ex = ec.TwinExecutor()

    rng = np.random.default_rng(args.seed)
    wins = []
    for scene in SCENES:
        g0 = by[(scene, 0)]
        ts = g0.baseline.times_ms
        tc = [t for t in range(D.HIST - 1, len(ts) - ec.K_HORIZON) if ts[t] >= D.TRAIN_MS]
        picks = rng.choice(len(tc), size=args.n_per_scene, replace=False)
        for pi in picks:
            ue = int(rng.integers(0, 8))
            wins.append((scene, ue, tc[pi]))
    print(f"决策窗: {len(wins)} (每场景 {args.n_per_scene}, held-out, 种子 {args.seed})", flush=True)

    wm_ms = WorldModel()
    wm_v2 = WorldModel(wm_path=ROOT / "models" / "gru_wm.pt",
                       meta_path=ROOT / "models" / "gru_wm_meta.json")
    eval_mu, eval_sd, w = wm_ms.mu, wm_ms.sd, ec.DEFAULT_W

    nones = np.array([
        ec.actual_reward(by[(s, u)].baseline.states, t + 1, t + 1 + ec.K_HORIZON,
                         eval_mu, eval_sd, w)
        for s, u, t in wins])

    lams = [1.0, 0.7, 0.5, 0.3, 0.0]
    out = {"meta": {"n_windows": len(wins), "seed": args.seed,
                    "horizon_steps": ec.K_HORIZON,
                    "policy": "CEM-MPC (fused reward)",
                    "reward_scale": "eval 用 MS mu/sd; 融合内部各 WM 用自身 z",
                    "lams": lams},
           "lams": {}}

    for lam in lams:
        rows = []
        t0 = time.time()
        for wi, (scene, ue, t) in enumerate(wins):
            base = by[(scene, ue)].baseline.states
            hist = base[t - D.HIST + 1: t + 1]
            atype, aval, score = run_fused(
                f"λ={lam}", hist, wm_ms, wm_v2, lam)
            if atype == "none":
                branch = base
            else:
                branch = ex.run(scene, {atype: aval})[ue]
            real = ec.actual_reward(branch, t + 1, t + 1 + ec.K_HORIZON,
                                    eval_mu, eval_sd, w)
            pred = score  # fused wm 内奖励 (λ·MS + (1−λ)·v2)
            rows.append(dict(window=wi, scene=scene, ue=ue, t_idx=t,
                             action=atype, value=round(float(aval), 4),
                             wm_pred=pred, real=real))
            if (wi + 1) % 24 == 0:
                print(f"  [λ={lam}] {wi+1}/{len(wins)} 窗 "
                      f"({ex.n_runs} twin, {time.time()-t0:.0f}s)", flush=True)
        reals = np.array([r["real"] for r in rows])
        lifts = reals - nones
        stat = ec.paired_lift(reals, nones, seed=args.seed)
        preds = np.array([r["wm_pred"] for r in rows])
        per_scene = {}
        for scene in SCENES:
            sel = np.array([r["scene"] == scene for r in rows])
            per_scene[scene] = {"lift_mean": float(lifts[sel].mean()),
                                "win_rate": float((lifts[sel] > 0).mean()),
                                "n": int(sel.sum())}
        out["lams"][str(lam)] = {
            "lift_mean": stat["mean"], "ci95": stat["ci95"],
            "win_rate": stat["win_rate"], "p_lift_positive": stat["p_lift_positive"],
            "action_dist": {t: int(sum(1 for r in rows if r["action"] == t))
                            for t in sorted({r["action"] for r in rows})},
            "per_scene": per_scene,
            "fidelity_chosen": {
                "pearson_r": float(np.corrcoef(preds, reals)[0, 1]),
                "spearman_r": float(pd.Series(preds).corr(pd.Series(reals),
                                                          method="spearman")),
                "n": len(rows)},
            "n_windows": len(rows)}
        print(f"[λ={lam}] lift={stat['mean']:+.4f} CI={stat['ci95']} "
              f"P>0={stat['p_lift_positive']:.4f} win={stat['win_rate']:.3f} "
              f"fid(pearson)={out['lams'][str(lam)]['fidelity_chosen']['pearson_r']:+.3f} "
              f"actions={out['lams'][str(lam)]['action_dist']}", flush=True)

    out["meta"]["twin_runs"] = ex.n_runs
    out["meta"]["runtime_s"] = round(time.time() - t0clock, 1)
    out["windows"] = wins
    dst = ROOT / "docs" / "closedloop" / "fuse_wm_planner.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"twin: {ex.n_runs} 次; 总耗时 {out['meta']['runtime_s']}s → {dst}")


if __name__ == "__main__":
    main()
