#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
闭环评测 — 融合奖励蒸馏策略 (Policy-Fused) 在仿真器上真实执行
==============================================================
背景: train_planner_fused.py 把 λ=0.5 融合奖励 CEM-MPC 蒸馏为单网络
policy_fused_net.pt。本脚本用与 eval_closedloop.py **逐位兼容**的协议
(96 held-out 窗, seed 2026, 同根 twin 执行, MS 奖励尺度) 把该策略的
闭环真实提升与生产 Policy (MS 蒸馏) / CEM-MPC / Random / NoAction 对齐比较。

内置一致性校验 (与 committed closedloop_results.json 对齐):
  CEM-MPC  lift +0.2852  CI [+0.1133, +0.4578]  P(lift>0)=0.9995
  Policy   lift +0.2825
首测失败即 assert — 协议裂纹检测, 而非静默偏差。

用法: .venv/bin/python scripts/eval_closedloop_fused.py [--n-per-scene 24] [--seed 2026]
输出: docs/closedloop/closedloop_fused.json
"""
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


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("ec", ROOT / "scripts" / "eval_closedloop.py")     # TwinExecutor / action_of_cem /
tp = _load("tp", ROOT / "scripts" / "train_planner.py")        # PolicyNet / action_of_policy

DOCS = ROOT / "docs" / "closedloop"
SCENES = ec.SCENES
STRATEGIES = ["CEM-MPC", "Policy", "Policy-Fused", "Random", "NoAction"]
POLICY_FUSED = ROOT / "models" / "policy_fused_net.pt"


def action_of_policy_fused(pol, hist_s: np.ndarray, mu, sd) -> tuple[str, float]:
    return ec.action_of_policy(pol, hist_s, mu, sd)   # 同一前馈接口, 不同权重


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-scene", type=int, default=24)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    t0 = time.time()
    torch.manual_seed(0)
    rng = np.random.default_rng(args.seed)

    ds = pd.read_parquet(ROOT / "datalake" / "generated" / "twin_action_dataset.parquet")
    groups = ec.D.load_branches(ds)
    by = {(g.scene, int(g.ue)): g for g in groups}

    wm = ec.WorldModel()
    planner = ec.CEMPlanner(wm=wm)
    reward_fn = ec.make_reward(ec.RewardConfig())
    mu, sd, w = wm.mu, wm.sd, ec.DEFAULT_W

    pol = tp.PolicyNet()
    pol.load_state_dict(torch.load(ROOT / "models" / "policy_net.pt", map_location="cpu"))
    pol.eval()

    pol_f = tp.PolicyNet()
    pol_f.load_state_dict(torch.load(POLICY_FUSED, map_location="cpu"))
    pol_f.eval()

    ex = ec.TwinExecutor()
    log = lambda *a: print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

    # ---- 同根一致性校验 (与 eval_closedloop 相同) ----
    log("同根校验: 数据集 baseline vs 现场 twin 重跑 (逐场景逐 UE)")
    for scene in SCENES:
        fresh = ex.run(scene, None)
        for ue in range(8):
            ref = by[(scene, ue)].baseline.states
            got = fresh[ue]
            assert len(ref) == len(got), f"{scene}/UE{ue}: 长度不一致"
            assert np.allclose(ref, got, atol=1e-6), f"{scene}/UE{ue}: 同根校验失败"
    log("  ✓ 4 场景 × 8 UE 全部一致 (twin 确定性 + 数据集种子对齐)")

    # ---- 决策窗采样 (与原协议逐位一致的采样顺序) ----
    wins = []
    for scene in SCENES:
        times = by[(scene, 0)].baseline.times_ms
        t_cand = [t for t in range(ec.D.HIST - 1, len(times) - ec.K_HORIZON)
                  if times[t] >= ec.D.TRAIN_MS]
        picks = rng.choice(len(t_cand), size=args.n_per_scene, replace=False)
        for pi in picks:
            t = t_cand[pi]
            ue = int(rng.integers(0, 8))
            wins.append((scene, ue, t))
    log(f"决策窗: {len(wins)} (每场景 {args.n_per_scene}, held-out t≥{int(ec.D.TRAIN_MS)}ms, "
        f"种子 {args.seed})")

    # ---- 逐窗执行 ----
    rows = []
    for wi, (scene, ue, t) in enumerate(wins):
        base_states = by[(scene, ue)].baseline.states
        hist_s = base_states[t - ec.D.HIST + 1: t + 1]

        acts = {
            "CEM-MPC": ec.action_of_cem(planner, hist_s),
            "Policy": ec.action_of_policy(pol, hist_s, mu, sd),
            "Policy-Fused": action_of_policy_fused(pol_f, hist_s, mu, sd),
            "Random": ec.action_of_random(rng),
            "NoAction": ("none", 0.0),
        }
        for strat, (atype, value) in acts.items():
            pred = ec.wm_predicted_reward(reward_fn, wm, hist_s, atype, value)
            if atype == "none":
                branch = base_states
            else:
                branch = ex.run(scene, {atype: value})[ue]
            real = ec.actual_reward(branch, t + 1, t + 1 + ec.K_HORIZON, mu, sd, w)
            rows.append(dict(window=wi, scene=scene, ue=ue, t_idx=t,
                             t_ms=float(by[(scene, ue)].baseline.times_ms[t]),
                             strategy=strat, action=atype, value=round(value, 4),
                             wm_pred=pred, real=real))
        log(f"  [{wi+1}/{len(wins)}] {scene}/UE{ue}@{int(rows[-1]['t_ms'])}ms "
            + " ".join(f"{s}:{dict((r['action'], r['value']) for r in rows if r['window']==wi and r['strategy']==s)}"
                       for s in ["CEM-MPC", "Policy", "Policy-Fused", "Random"]))

    df = pd.DataFrame(rows)
    log(f"twin 执行: {ex.n_runs} 次; 总耗时 {time.time()-t0:.1f}s")

    # ---- 汇总 (与原协议同构) ----
    summary: dict = {"strategy": {}, "wm_fidelity": {}, "per_scene": {}}
    none_by_win = {r["window"]: r["real"] for r in rows if r["strategy"] == "NoAction"}
    for strat in STRATEGIES:
        sub = df[df.strategy == strat]
        reals = [float(sub[sub.window == w].real.iloc[0]) for w in sorted(none_by_win)]
        nones = [none_by_win[w] for w in sorted(none_by_win)]
        summary["strategy"][strat] = {
            "mean_real_reward": float(np.mean(reals)),
            "action_rate": float((sub.action != "none").mean()),
            "lift_vs_noaction": ec.paired_lift(reals, nones, seed=args.seed),
        }

    preds, reals_all = df.wm_pred.to_numpy(), df.real.to_numpy()
    summary["wm_fidelity"] = {
        "pearson_r": float(np.corrcoef(preds, reals_all)[0, 1]),
        "spearman_r": float(pd.Series(preds).corr(pd.Series(reals_all), method="spearman")),
        "mean_abs_gap": float(np.abs(preds - reals_all).mean()),
        "n_pairs": int(len(df)),
    }

    for scene in SCENES:
        sub = df[df.scene == scene]
        s_n = {r.window: r.real for r in sub.itertuples() if r.strategy == "NoAction"}
        summary["per_scene"][scene] = {}
        for strat in ["CEM-MPC", "Policy", "Policy-Fused", "Random"]:
            reals_s, nones_s = [], []
            for r in sub.itertuples():
                if r.strategy == strat:
                    reals_s.append(r.real)
                    nones_s.append(s_n[r.window])
            summary["per_scene"][scene][strat] = ec.paired_lift(reals_s, nones_s, seed=args.seed)

    summary["actions"] = {
        s: df[df.strategy == s].action.value_counts().to_dict() for s in STRATEGIES
    }

    # ---- 一致性校验: 已被提交基准逐位对齐 (协议裂纹检测) ----
    ref = json.loads((DOCS / "closedloop_results.json").read_text(encoding="utf-8"))
    for strat, tol in [("CEM-MPC", 1e-6), ("Policy", 1e-6)]:
        a = summary["strategy"][strat]["lift_vs_noaction"]["mean"]
        b = ref["summary"]["strategy"][strat]["lift_vs_noaction"]["mean"]
        assert abs(a - b) < tol, f"{strat} lift 与基准不一致: 本次 {a:.6f} vs 已提交 {b:.6f}"
    log("  ✓ CEM-MPC / Policy lift 与已提交 closedloop_results.json 逐位一致")

    DOCS.mkdir(parents=True, exist_ok=True)
    out = {
        "meta": {
            "date": time.strftime("%Y-%m-%d %H:%M"),
            "seed": args.seed,
            "n_windows": len(wins),
            "n_per_scene": args.n_per_scene,
            "horizon_steps": ec.K_HORIZON,
            "horizon_ms": ec.K_HORIZON * 50.0,
            "reward_weights": {"RSRP": 0.10, "SINR": 0.20, "DL": 0.60, "CQI": 0.10},
            "same_root_verified": True,
            "twin_runs": ex.n_runs,
            "models": {"wm": "models/gru_wm_ms.pt",
                       "policy": "models/policy_net.pt",
                       "policy_fused": "models/policy_fused_net.pt"},
            "runtime_s": round(time.time() - t0, 1),
        },
        "summary": summary,
        "windows": rows,
    }
    dst = DOCS / "closedloop_fused.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 72)
    print(f"闭环评测 — 融合策略 ({len(wins)} held-out 窗 × {len(STRATEGIES)} 策略, "
          f"horizon {ec.K_HORIZON}步/{ec.K_HORIZON*50:.0f}ms, 同根校验 ✓)")
    print("=" * 72)
    print(f"{'策略':14s} {'真实奖励':>10s} {'动作率':>7s} {'lift均值':>9s} "
          f"{'95% CI':>20s} {'胜率':>6s} {'P(lift>0)':>9s}")
    for strat in STRATEGIES:
        s = summary["strategy"][strat]
        l = s["lift_vs_noaction"]
        print(f"{strat:14s} {s['mean_real_reward']:+10.4f} {s['action_rate']:7.2f} "
              f"{l['mean']:+9.4f} [{l['ci95'][0]:+.4f}, {l['ci95'][1]:+.4f}] "
              f"{l['win_rate']:6.2f} {l['p_lift_positive']:9.3f}")
    f = summary["wm_fidelity"]
    print(f"\nWM 决策保真度: pearson_r={f['pearson_r']:+.3f} spearman_r={f['spearman_r']:+.3f} "
          f"mean|gap|={f['mean_abs_gap']:.4f} (n={f['n_pairs']})")
    print(f"动作分布: {json.dumps(summary['actions'], ensure_ascii=False)}")
    print(f"\n→ {dst}")


if __name__ == "__main__":
    main()
