#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
闭环评测 (Closed-Loop Evaluation): 策略在仿真器上真实执行
============================================================
目的: 现有评测仅在世界模型 (WM) 内部计奖励。本脚本补上关键一环 —
      把各策略推荐的动作施加到仿真器 (TinyTwin 同根分支), 用真实 KPI 轨迹对比。

协议 (与反事实数据生成 generate_actions.py 完全同根):
  1. 从基准分支 held-out 段 (t ≥ 8000ms) 采样 (场景, UE, t) 决策窗
  2. 各策略在同一历史窗上给出动作 a:
       CEM-MPC   — 世界模型内规划 (wm/planner.py)
       Policy    — 蒸馏策略网络一步前馈 (models/policy_net.pt)
       Random    — 纯随机动作 (负控制: 动作空间均匀采样, 无选择)
       NoAction  — 不干预 (零动作基线)
  3. 动作 a 施加于同根 twin (scene seed 与训练数据集一致, 确定性) → 真实 KPI 轨迹
  4. 真实奖励 r_real = mean_{k=1..K} [ w · z(s_{t+k}) ], 与 WM 奖励同权重/同归一化
  5. 汇报: 各策略真实奖励与相对 NoAction 的提升 (配对 lift + bootstrap CI)、
          WM 预测奖励 vs 真实奖励的一致性 (决策保真度)

用法: .venv/bin/python scripts/eval_closedloop.py [--n-per-scene 6] [--seed 123]
输出: docs/closedloop/closedloop_results.json (+ 控制台表格)
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

from wm import data as D                                   # noqa: E402
from wm.planner import (WorldModel, CEMPlanner, RewardConfig, make_reward,  # noqa: E402
                        ACTION_SPACE, ACTION_TYPES, DEFAULT_W)
from scripts.generate_actions import run_branch            # noqa: E402

DOCS = ROOT / "docs" / "closedloop"
SCENES = ["城区", "郊区", "低空", "机器人工厂"]
SEEDS = {s: 9000 + i * 13 for i, s in enumerate(SCENES)}   # 与 generate_actions.py 一致
K_HORIZON = 8            # 与 RewardConfig.k 一致 (×50ms = 400ms)
STRATEGIES = ["CEM-MPC", "Policy", "Random", "NoAction"]

# ---- 加载蒸馏策略网络 (复用 train_planner.PolicyNet 定义) ----
_spec = importlib.util.spec_from_file_location("tp", ROOT / "scripts" / "train_planner.py")
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)


# ============================================================
# 同根仿真执行 (带缓存)
# ============================================================
class TwinExecutor:
    """同根分支执行器: (scene, action) → 全部 UE 的状态轨迹 [T,S] (确定性缓存)"""

    def __init__(self):
        self.cache: dict[tuple, dict[int, np.ndarray]] = {}
        self.n_runs = 0

    def run(self, scene: str, action: dict | None) -> dict[int, np.ndarray]:
        """返回 {ue_id: states[T, S]}; action=None 为基准分支"""
        key = (scene, None if action is None else tuple(sorted(action.items())))
        if key in self.cache:
            return self.cache[key]
        tabs, _ = run_branch(scene, SEEDS[scene], action)
        ue = tabs["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
        out = {}
        for uid, g in ue.groupby("UEID"):
            out[int(uid)] = g[D.STATE_COLS].to_numpy(dtype=np.float64)
        self.cache[key] = out
        self.n_runs += 1
        return out


def actual_reward(states: np.ndarray, lo: int, hi: int,
                  mu: np.ndarray, sd: np.ndarray, w: np.ndarray) -> float:
    """真实奖励: s[lo:hi] (原始单位) → 每步 z·w 后对时间平均 (与 make_reward 同构)"""
    z = (states[lo:hi] - mu) / sd          # [k, S]
    return float((z @ w).mean())


# ============================================================
# 策略动作生成
# ============================================================
def action_of_cem(planner: CEMPlanner, hist_s: np.ndarray) -> tuple[str, float]:
    res = planner.plan(hist_s[np.newaxis], return_all=False)
    if res["recommend"]:
        return res["type"], float(res["value"])
    return "none", 0.0


def action_of_policy(pol, hist_s: np.ndarray, mu, sd) -> tuple[str, float]:
    x = torch.tensor(((hist_s - mu) / sd).reshape(1, -1), dtype=torch.float32)
    with torch.no_grad():
        logits, vals = pol(x)
    t_i = int(logits.argmax(1)[0])
    if t_i == 0:
        return "none", 0.0
    atype = tp.INT_TYPES[t_i]
    val = float(pol.denorm_value(vals)[0, t_i - 1])
    return atype, val


def action_of_random(rng: np.random.Generator) -> tuple[str, float]:
    atype = ACTION_TYPES[int(rng.integers(1, len(ACTION_TYPES)))]   # 不含 baseline
    spec = ACTION_SPACE[atype]
    return atype, float(rng.uniform(spec["lo"], spec["hi"]))


def wm_predicted_reward(reward_fn, wm: WorldModel, hist_s: np.ndarray,
                        atype: str, value: float) -> float:
    """WM 内奖励 (预测): 与 make_reward 完全同构 (接口收原始单位 hist, 内部自行归一化)"""
    ti = np.array([ACTION_SPACE[atype]["idx"] if atype in ACTION_SPACE else 0],
                  dtype=np.float32)
    v = np.array([value], dtype=np.float32)
    return float(reward_fn(wm, hist_s[np.newaxis].astype(np.float32), ti, v)[0])


# ============================================================
# 统计: 配对 lift + bootstrap CI
# ============================================================
def paired_lift(strat_real: list[float], none_real: list[float],
                n_boot: int = 4000, seed: int = 0) -> dict:
    a = np.asarray(strat_real)
    b = np.asarray(none_real)
    d = a - b
    rng = np.random.default_rng(seed)
    n = len(d)
    boots = np.array([d[rng.integers(0, n, n)].mean() for _ in range(n_boot)])
    return {
        "mean": float(d.mean()),
        "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
        "win_rate": float((d > 0).mean()),
        "p_lift_positive": float((boots > 0).mean()),
    }


# ============================================================
# main
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-scene", type=int, default=6,
                    help="每场景采样决策窗数 (在 8 UE × held-out 段上分层采样)")
    ap.add_argument("--seed", type=int, default=123)
    args = ap.parse_args()

    t0 = time.time()
    torch.manual_seed(0)
    rng = np.random.default_rng(args.seed)

    # ---- 数据 / 模型 ----
    ds = pd.read_parquet(ROOT / "datalake" / "generated" / "twin_action_dataset.parquet")
    groups = D.load_branches(ds)
    wm = WorldModel()
    planner = CEMPlanner(wm=wm)
    reward_fn = make_reward(RewardConfig())
    mu, sd, w = wm.mu, wm.sd, DEFAULT_W

    pol = tp.PolicyNet()
    pol.load_state_dict(torch.load(ROOT / "models" / "policy_net.pt", map_location="cpu"))
    pol.eval()

    ex = TwinExecutor()
    log = lambda *a: print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

    # ---- 同根一致性校验: 数据集基准分支 == 现场重跑 twin 基准 ----
    log("同根校验: 数据集 baseline vs 现场 twin 重跑 (逐场景逐 UE)")
    by_scene_ue = {(g.scene, int(g.ue)): g for g in groups}   # ue 键统一为 int
    for scene in SCENES:
        fresh = ex.run(scene, None)
        for ue in range(8):
            ref = by_scene_ue[(scene, ue)].baseline.states
            got = fresh[ue]
            assert len(ref) == len(got), f"{scene}/UE{ue}: 长度不一致"
            assert np.allclose(ref, got, atol=1e-6), f"{scene}/UE{ue}: 同根校验失败"
    log("  ✓ 4 场景 × 8 UE 全部一致 (twin 确定性 + 数据集种子对齐)")

    # ---- 决策窗采样 (held-out: t≥8000ms, 且 t+K 在段内) ----
    wins = []   # (scene, ue, t_idx)
    for scene in SCENES:
        grp_ue = by_scene_ue[(scene, 0)]
        times = grp_ue.baseline.times_ms
        t_cand = [t for t in range(D.HIST - 1, len(times) - K_HORIZON)
                  if times[t] >= D.TRAIN_MS]
        picks = rng.choice(len(t_cand), size=args.n_per_scene, replace=False)
        for pi in picks:
            t = t_cand[pi]
            ue = int(rng.integers(0, 8))
            wins.append((scene, ue, t))
    log(f"决策窗: {len(wins)} (每场景 {args.n_per_scene}, held-out t≥{int(D.TRAIN_MS)}ms, "
        f"种子 {args.seed})")

    # ---- 逐窗执行 ----
    rows = []
    for wi, (scene, ue, t) in enumerate(wins):
        base_states = by_scene_ue[(scene, ue)].baseline.states
        hist_s = base_states[t - D.HIST + 1: t + 1]

        acts = {
            "CEM-MPC": action_of_cem(planner, hist_s),
            "Policy": action_of_policy(pol, hist_s, mu, sd),
            "Random": action_of_random(rng),
            "NoAction": ("none", 0.0),
        }
        for strat, (atype, value) in acts.items():
            pred = wm_predicted_reward(reward_fn, wm, hist_s, atype, value)
            if atype == "none":
                branch = base_states
            else:
                branch = ex.run(scene, {atype: value})[ue]
            real = actual_reward(branch, t + 1, t + 1 + K_HORIZON, mu, sd, w)
            rows.append(dict(window=wi, scene=scene, ue=ue, t_idx=t,
                             t_ms=float(base_states.shape[0] and
                                        by_scene_ue[(scene, ue)].baseline.times_ms[t]),
                             strategy=strat, action=atype, value=round(value, 4),
                             wm_pred=pred, real=real))
        log(f"  [{wi+1}/{len(wins)}] {scene}/UE{ue}@{int(rows[-1]['t_ms'])}ms "
            + " ".join(f"{s}:{dict((r['action'], r['value']) for r in rows if r['window']==wi and r['strategy']==s)}"
                       for s in ["CEM-MPC", "Policy", "Random"]))

    df = pd.DataFrame(rows)
    log(f"twin 执行: {ex.n_runs} 次 (含缓存命中前); 总耗时 {time.time()-t0:.1f}s")

    # ---- 汇总 ----
    summary: dict = {"strategy": {}, "wm_fidelity": {}, "per_scene": {}}
    none_by_win = {r["window"]: r["real"] for r in rows if r["strategy"] == "NoAction"}
    for strat in STRATEGIES:
        sub = df[df.strategy == strat]
        reals = [float(sub[sub.window == w].real.iloc[0]) for w in sorted(none_by_win)]
        nones = [none_by_win[w] for w in sorted(none_by_win)]
        summary["strategy"][strat] = {
            "mean_real_reward": float(np.mean(reals)),
            "action_rate": float((sub.action != "none").mean()),
            "lift_vs_noaction": paired_lift(reals, nones, seed=args.seed),
        }

    # WM 预测 vs 真实 (决策保真度)
    preds, reals_all = df.wm_pred.to_numpy(), df.real.to_numpy()
    summary["wm_fidelity"] = {
        "pearson_r": float(np.corrcoef(preds, reals_all)[0, 1]),
        "spearman_r": float(pd.Series(preds).corr(pd.Series(reals_all), method="spearman")),
        "mean_abs_gap": float(np.abs(preds - reals_all).mean()),
        "n_pairs": int(len(df)),
    }

    # 每场景 CEM-MPC / Policy lift
    for scene in SCENES:
        sub = df[df.scene == scene]
        s_n = {r.window: r.real for r in sub.itertuples() if r.strategy == "NoAction"}
        summary["per_scene"][scene] = {}
        for strat in ["CEM-MPC", "Policy", "Random"]:
            reals_s, nones_s = [], []
            for r in sub.itertuples():
                if r.strategy == strat:
                    reals_s.append(r.real)
                    nones_s.append(s_n[r.window])
            summary["per_scene"][scene][strat] = paired_lift(reals_s, nones_s, seed=args.seed)

    # 动作分布
    summary["actions"] = {
        s: df[df.strategy == s].action.value_counts().to_dict() for s in STRATEGIES
    }

    # ---- 输出 ----
    DOCS.mkdir(parents=True, exist_ok=True)
    out = {
        "meta": {
            "date": time.strftime("%Y-%m-%d %H:%M"),
            "seed": args.seed,
            "n_windows": len(wins),
            "n_per_scene": args.n_per_scene,
            "horizon_steps": K_HORIZON,
            "horizon_ms": K_HORIZON * 50.0,
            "reward_weights": {"RSRP": 0.10, "SINR": 0.20, "DL": 0.60, "CQI": 0.10},
            "same_root_verified": True,
            "twin_runs": ex.n_runs,
            "models": {"wm": "models/gru_wm_ms.pt", "policy": "models/policy_net.pt"},
            "runtime_s": round(time.time() - t0, 1),
        },
        "summary": summary,
        "windows": rows,
    }
    (DOCS / "closedloop_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 控制台报告 ----
    print("\n" + "=" * 72)
    print(f"闭环评测结果 ({len(wins)} held-out 窗 × {len(STRATEGIES)} 策略, "
          f"horizon {K_HORIZON}步/{K_HORIZON*50:.0f}ms, 同根校验 ✓)")
    print("=" * 72)
    print(f"{'策略':10s} {'真实奖励':>10s} {'动作率':>7s} {'lift均值':>9s} "
          f"{'95% CI':>20s} {'胜率':>6s} {'P(lift>0)':>9s}")
    for strat in STRATEGIES:
        s = summary["strategy"][strat]
        l = s["lift_vs_noaction"]
        print(f"{strat:10s} {s['mean_real_reward']:+10.4f} {s['action_rate']:7.2f} "
              f"{l['mean']:+9.4f} [{l['ci95'][0]:+.4f}, {l['ci95'][1]:+.4f}] "
              f"{l['win_rate']:6.2f} {l['p_lift_positive']:9.3f}")
    f = summary["wm_fidelity"]
    print(f"\nWM 决策保真度: pearson_r={f['pearson_r']:+.3f} spearman_r={f['spearman_r']:+.3f} "
          f"mean|gap|={f['mean_abs_gap']:.4f} (n={f['n_pairs']})")
    print(f"动作分布: {json.dumps(summary['actions'], ensure_ascii=False)}")
    print(f"\n→ {DOCS}/closedloop_results.json")


if __name__ == "__main__":
    main()
