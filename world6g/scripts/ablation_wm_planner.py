#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
消融实验: 闭环提升是否依赖世界模型的多步一致性训练?
=====================================================
对比两个已训练 WM (同架构、同数据、同 seed=42, 唯一差异 = 训练目标):
  - gru_wm_ms.pt  (v0.3-MS, 多步一致性 + 异方差 NLL)   [生产模型]
  - gru_wm.pt     (v0.2,    单步 teacher-forcing)      [消融对照]

以 CEM-MPC 为公共策略外壳, 评测协议与 eval_closedloop.py 完全一致:
  - 同 seed → 决策窗逐一相同 (held-out t≥TRAIN_MS, 分层采样)
  - 同根 twin 执行 (TwinExecutor, 确定性缓存)
  - 同一奖励尺度: mu/sd 取自生产模型 (MS), w=DEFAULT_W
两模型的差异只允许通过「规划器内部的世界模型预测」进入决策。

内置一致性检查: v0.3-MS 行应逐一复现 closedloop_results.json 的 CEM-MPC
(lift +0.2852, CI [+0.1133, +0.4578], P>0=0.9995, seed 2026, 96 窗)。

用法: .venv/bin/python scripts/ablation_wm_planner.py [--n-per-scene 24] [--seed 2026]
输出: docs/closedloop/ablation_wm_planner.json
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
from wm.planner import (WorldModel, CEMPlanner, RewardConfig, make_reward)  # noqa: E402

SCENES = ["城区", "郊区", "低空", "机器人工厂"]

# 复用 eval_closedloop 的执行器 (同根执行 + 缓存) 与奖励口径
_spec = importlib.util.spec_from_file_location(
    "ec", ROOT / "scripts" / "eval_closedloop.py")
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)


def run_cem(name, wm, wins, by_scene_ue, ex, eval_mu, eval_sd, w):
    """CEM-MPC 公共外壳 (wm 可插拔): 逐窗规划 → 同根执行 → 真实奖励。"""
    planner = CEMPlanner(wm=wm)
    reward_fn = make_reward(RewardConfig())
    rows = []
    t0 = time.time()
    for wi, (scene, ue, t) in enumerate(wins):
        base_states = by_scene_ue[(scene, ue)].baseline.states
        hist_s = base_states[t - D.HIST + 1: t + 1]
        atype, value = ec.action_of_cem(planner, hist_s)
        pred = ec.wm_predicted_reward(reward_fn, wm, hist_s, atype, value)
        if atype == "none":
            branch = base_states
        else:
            branch = ex.run(scene, {atype: value})[ue]
        real = ec.actual_reward(branch, t + 1, t + 1 + ec.K_HORIZON,
                                eval_mu, eval_sd, w)
        rows.append(dict(window=wi, scene=scene, ue=ue, t_idx=t,
                         action=atype, value=round(float(value), 4),
                         wm_pred=pred, real=real))
        if (wi + 1) % 12 == 0:
            print(f"  [{name}] {wi+1}/{len(wins)} 窗 "
                  f"(twin runs: {ex.n_runs}, {time.time()-t0:.0f}s)", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-scene", type=int, default=24)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    t0clock = time.time()
    torch.manual_seed(0)

    ds = pd.read_parquet(ROOT / "datalake" / "generated" / "twin_action_dataset.parquet")
    groups = D.load_branches(ds)
    by_scene_ue = {(g.scene, int(g.ue)): g for g in groups}
    ex = ec.TwinExecutor()

    # ---- 决策窗采样: 与 eval_closedloop 完全一致 (同 seed → 同窗) ----
    rng = np.random.default_rng(args.seed)
    wins = []
    for scene in SCENES:
        grp_ue = by_scene_ue[(scene, 0)]
        times = grp_ue.baseline.times_ms
        t_cand = [t for t in range(D.HIST - 1, len(times) - ec.K_HORIZON)
                  if times[t] >= D.TRAIN_MS]
        picks = rng.choice(len(t_cand), size=args.n_per_scene, replace=False)
        for pi in picks:
            t = t_cand[pi]
            ue = int(rng.integers(0, 8))
            wins.append((scene, ue, t))
    print(f"决策窗: {len(wins)} (每场景 {args.n_per_scene}, held-out, 种子 {args.seed})",
          flush=True)

    # ---- 奖励尺度: 固定为生产模型 (MS) 的 mu/sd, 与 closedloop 基准同尺 ----
    ms_wm = WorldModel()
    eval_mu, eval_sd, w = ms_wm.mu, ms_wm.sd, ec.DEFAULT_W

    # ---- NoAction 基准奖励 (逐窗, 两模型共用) ----
    nones = np.array([
        ec.actual_reward(by_scene_ue[(s, u)].baseline.states,
                         t + 1, t + 1 + ec.K_HORIZON, eval_mu, eval_sd, w)
        for s, u, t in wins])

    v02_wm = WorldModel(wm_path=ROOT / "models" / "gru_wm.pt",
                        meta_path=ROOT / "models" / "gru_wm_meta.json")

    out = {"meta": {"n_windows": len(wins), "seed": args.seed,
                    "horizon_steps": ec.K_HORIZON, "policy": "CEM-MPC (公共外壳)",
                    "reward_scale": "mu/sd 取自 gru_wm_ms.pt (与 closedloop 基准一致)",
                    "wm_models": {"v0.3-MS (多步一致性)": "models/gru_wm_ms.pt",
                                  "v0.2 (单步, 消融)": "models/gru_wm.pt"}},
           "models": {}}

    all_rows = {}
    for name, wm in [("v0.3-MS (多步一致性)", ms_wm),
                     ("v0.2 (单步, 消融)", v02_wm)]:
        rows = run_cem(name, wm, wins, by_scene_ue, ex, eval_mu, eval_sd, w)
        all_rows[name] = rows
        reals = np.array([r["real"] for r in rows])
        lifts = reals - nones
        stat = ec.paired_lift(reals, nones, seed=args.seed)
        per_scene = {}
        for scene in SCENES:
            sel = np.array([r["scene"] == scene for r in rows])
            per_scene[scene] = {"lift_mean": float(lifts[sel].mean()),
                                "win_rate": float((lifts[sel] > 0).mean()),
                                "n": int(sel.sum())}
        # 决策保真度: 该模型对「自己选定动作」的预测奖励 vs 真实奖励
        preds = np.array([r["wm_pred"] for r in rows])
        out["models"][name] = {
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
        print(f"[{name}] lift={stat['mean']:+.4f} CI={stat['ci95']} "
              f"P>0={stat['p_lift_positive']:.4f} win_rate={stat['win_rate']:.3f} "
              f"fid(pearson)={out['models'][name]['fidelity_chosen']['pearson_r']:+.3f} "
              f"actions={out['models'][name]['action_dist']}", flush=True)

    # ---- 配对消融统计: 逐窗 real(MS) - real(v0.2) ----
    ms_r = np.array([r["real"] for r in all_rows["v0.3-MS (多步一致性)"]])
    v2_r = np.array([r["real"] for r in all_rows["v0.2 (单步, 消融)"]])
    diff = ec.paired_lift(ms_r.tolist(), v2_r.tolist(), seed=args.seed)
    out["ablation_paired_ms_minus_v02"] = diff
    n_agree = int(sum(1 for a, b in zip(all_rows["v0.3-MS (多步一致性)"],
                                        all_rows["v0.2 (单步, 消融)"])
                      if a["action"] == b["action"]))
    out["meta"]["action_agreement_windows"] = n_agree
    print(f"[配对消融] real(MS) - real(v0.2): mean={diff['mean']:+.4f} "
          f"CI={diff['ci95']} P>0={diff['p_lift_positive']:.4f} "
          f"win_rate={diff['win_rate']:.3f} | 动作一致窗 {n_agree}/{len(wins)}",
          flush=True)

    # ---- 逐窗明细 (两模型共用窗口, 供动作一致/歧义子集上的保真度配对分析) ----
    wd = []
    for i, (s, u, t) in enumerate(wins):
        row = {"window": i, "scene": s, "ue": u, "t_idx": t}
        for name in all_rows:
            r = all_rows[name][i]
            row[name] = {"action": r["action"], "value": r["value"],
                         "wm_pred": r["wm_pred"], "real": r["real"]}
        wd.append(row)

    out["meta"]["twin_runs"] = ex.n_runs
    out["meta"]["runtime_s"] = round(time.time() - t0clock, 1)
    out["windows"] = wd
    dst = ROOT / "docs" / "closedloop" / "ablation_wm_planner.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"twin 执行: {ex.n_runs} 次; 总耗时 {out['meta']['runtime_s']}s")
    print(f"→ {dst}")


if __name__ == "__main__":
    main()
