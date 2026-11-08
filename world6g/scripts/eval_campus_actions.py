#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
昌平信息港校园应用: 用校准 WM 做动作优化 (E4)
=============================================
在校园 (campus) 孪生标准窗口上, 用校准世界模型 (wm_cal) 做 CEM-MPC
多步规划, 输出推荐动作及预期/实际增益; 同时用 uncal-WM 作对照。

设计: 校园窗来自校准孪生 (无独立真实园区数据), 动作执行也走同一
校准孪生同根分支 → 自洽的 "预测 → 优化 → 真实施加" 闭环演示。

报告要点:
  - 推荐动作 type/value 分布 (合理: 偏好 az_offset/prb)
  - 相对 NoAction 的真实奖励 lift (窗口级 bootstrap CI)
  - cal-WM vs uncal-WM 在校园环境的决策对比
用法: python scripts/eval_campus_actions.py [--n-windows 24]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twin.calibrated import TinyTwin as CalTwin
from wm.planner import (WorldModel, CEMPlanner, CEMConfig, RewardConfig,
                        make_reward, ACTION_SPACE)
from wm.data import STATE_COLS, HIST

PKG_DIR = Path(__file__).resolve().parents[1]
MODELS = PKG_DIR / "models"
OUT = PKG_DIR / "docs" / "campus" / "campus_action_report.json"

SEED = 9028
W = np.array([0.10, 0.20, 0.60, 0.10], dtype=np.float32)
MU = np.array([-110.0, 15.0, 110.0, 13.0], dtype=np.float64)
SD = np.array([12.0, 8.0, 60.0, 3.0], dtype=np.float64)


class Executor:
    def __init__(self):
        self.cache = {}

    def run(self, action: dict | None) -> dict[int, np.ndarray]:
        key = "base" if action is None else tuple(sorted(action.items()))
        if key not in self.cache:
            tw = CalTwin("campus", n_cell=3, n_ue=8, seed=SEED)
            if action:
                if "pmax_scale" in action:
                    tw.tx_scale_db = 10 * np.log10(action["pmax_scale"])
                if "prb_scale" in action:
                    tw.prb_scale = action["prb_scale"]
                if "az_offset" in action:
                    for c in tw.site:
                        c["az"] = (c["az"] + action["az_offset"]) % 360
                if "ho_offset" in action:
                    tw.ho_offset_db = action["ho_offset"]
            ue = tw.run()["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
            out = {}
            for uid, g in ue.groupby("UEID"):
                out[int(uid)] = g[STATE_COLS].to_numpy(dtype=np.float64)
            self.cache[key] = out
        return self.cache[key]


def actual_reward(states, lo, hi, w=W):
    z = (states[lo:hi] - MU) / SD
    return float((z @ w).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-windows", type=int, default=24)
    args = ap.parse_args()

    ex = Executor()
    base = ex.run(None)                    # baseline 全校轨迹
    n_steps = len(base[0])
    # 窗口: 均匀散布在 8.0-9.2s (held-out) 每 UE
    rng = np.random.default_rng(2026)
    wins = []
    for uid in sorted(base):
        for _ in range(args.n_windows // 8 + 1):
            t = int(rng.integers(160, n_steps - 25))
            wins.append({"ue": uid, "t": t})
    wins = wins[: args.n_windows]

    out = {"n_windows": len(wins), "window_step_ms": 50.0, "windows": []}
    wm_pair = {}

    for tag in ("cal", "uncal"):
        wm = WorldModel(MODELS / f"wm_{tag}.pt", MODELS / f"wm_{tag}_meta.json")
        wm_pair[tag] = wm
        planner = CEMPlanner(wm=wm, reward=make_reward(RewardConfig(w=W)),
                             cem=CEMConfig(seed=7))
        rewards, actions, none_rewards = [], [], []
        for w in wins:
            uid, t = w["ue"], w["t"]
            hist_s = base[uid][t - HIST: t].astype(np.float32)
            res = planner.plan(hist_s[np.newaxis], return_all=False)
            if res["recommend"]:
                atype, aval = res["type"], float(res["value"])
            else:
                atype, aval = "none", 0.0
            branch = base[uid] if atype == "none" else ex.run({atype: aval})[uid]
            real = actual_reward(branch, t, min(t + 24, n_steps))
            none_r = actual_reward(base[uid], t, min(t + 24, n_steps))
            actions.append(f"{atype}={aval:.1f}")
            rewards.append(real)
            none_rewards.append(none_r)
            out["windows"].append({"ue": uid, "t_ms": int(t * 50), tag: {
                "action": f"{atype}={aval:.1f}", "real": round(real, 4),
                "none_real": round(none_r, 4)}})

        arr = np.array(rewards)
        lift = np.array(rewards) - np.array(none_rewards)
        boot = np.array([float(np.mean(np.random.default_rng(s).choice(
            lift, size=len(lift), replace=True))) for s in range(2000)])
        out[tag] = {
            "mean_real_reward": round(float(arr.mean()), 4),
            "mean_lift_vs_noaction": round(float(lift.mean()), 4),
            "lift_ci95": [round(float(np.percentile(boot, 2.5)), 4),
                          round(float(np.percentile(boot, 97.5)), 4)],
            "p_lift_positive": round(float((boot > 0).mean()), 4),
            "action_rate": round(float((np.array(actions) != "none=0.0").mean()), 3),
            "action_dist": {}


        }
        for a in actions:
            at = a.split("=")[0]
            out[tag]["action_dist"][at] = out[tag]["action_dist"].get(at, 0) + 1
        print(f"[{tag}] mean {out[tag]['mean_real_reward']:+.4f} "
              f"lift {out[tag]['mean_lift_vs_noaction']:+.4f} "
              f"CI{out[tag]['lift_ci95']} P>0={out[tag]['p_lift_positive']} "
              f"act={out[tag]['action_rate']} dist={out[tag]['action_dist']}")

    # ---- 自洽 rollout: 在校园孪生窗上测两 WM 预测 RMSE (E2-self) ----
    from wm.data import STATE_COLS as SCS
    self_roll = {"cal": {}, "uncal": {}}
    for tag in ("cal", "uncal"):
        wm = wm_pair[tag]
        rms = {c: [] for c in SCS}
        for w in wins:
            uid, t = w["ue"], w["t"]
            hist_s = base[uid][t - HIST: t].astype(np.float32)
            # baseline 动作的 8 步 rollout
            pred = wm.rollout(hist_s[np.newaxis], np.array([0], dtype=np.float32),
                              np.array([0.0], dtype=np.float32), steps=8)[0]
            true = base[uid][t: t + 8]
            for i, c in enumerate(SCS):
                rms[c].append(float(np.sqrt(np.mean((true[:, i] - pred[:, i]) ** 2))))
        self_roll[tag] = {c: round(float(np.mean(v)), 4) for c, v in rms.items()}
        self_roll[tag]["mean_all"] = round(float(np.mean(
            [v for vv in rms.values() for v in vv])), 4)
    print(f"\n[self-rollout (cal-twin 窗)] cal={self_roll['cal']}")
    print(f"                              uncal={self_roll['uncal']}")
    out["self_rollout"] = self_roll
    out["cal_minus_uncal_mean_lift"] = round(
        out["cal"]["mean_lift_vs_noaction"] - out["uncal"]["mean_lift_vs_noaction"], 4)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[result] {OUT}")


if __name__ == "__main__":
    main()
