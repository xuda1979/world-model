#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
闭环决策对比: 校准世界模型 vs 未校准世界模型 (唯一变量 = WM 来自哪个仿真器)
===========================================================================
在相同真实 held-out 窗口上, 用两个 WM (cal-WM / uncal-WM) 各自做 CEM-MPC
决策, 交给**同一个权威执行器 (校准 twin, 同根分支)** 真实施加动作,
计算真实奖励 lift vs NoAction → 校验"校准仿真器训练出的 WM 更强"。

设计关键: 只有 WM 不同, 窗口/执行器/奖励全部统一 → 无混淆。

用法: python scripts/eval_closedloop_calib.py [--n-windows 16] [--window 8000 9000]
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
OUT = PKG_DIR / "docs" / "closedloop" / "closedloop_calib_compare.json"

N_UE_DEFAULT = 8
SEEDS = {"城区": 9000, "郊区": 9007, "低空": 9014, "机器人工厂": 9021,
         "campus": 9028}
W = np.array([0.10, 0.20, 0.60, 0.10], dtype=np.float32)   # 同 DEFAULT_W
STEP_MS = 50.0


def load_real_windows():
    """真实 (城区/郊区/低空/工厂) + 校准园区窗口"""
    import duckdb
    con = duckdb.connect(str(PKG_DIR / "datalake/lake.duckdb"), read_only=True)
    lab = {r[0]: r[1].split("（")[0] for r in con.execute(
        "SELECT episode_id, MAX(CASE WHEN 字段='类型' THEN 描述 END) FROM map "
        "GROUP BY episode_id").fetchall()}
    ue = con.execute("SELECT * FROM ue").fetchdf()
    con.close()
    ue["scene"] = ue["episode_id"].map(lab)
    ue = ue[ue["scene"].isin(["城区", "郊区", "低空", "机器人工厂"])]
    ue = ue.sort_values(["episode_id", "UEID", "时间戳(ms)"]).reset_index(drop=True)
    wins = []
    for (sid, eid, uid), g in ue.groupby(["scene", "episode_id", "UEID"]):
        g = g.sort_values("时间戳(ms)")
        st = g[STATE_COLS].values.astype(np.float32)
        ts = g["时间戳(ms)"].values.astype(np.float64)
        if len(st) < HIST + 8:
            continue
        wins.append({"scene": sid, "eid": int(eid), "ue": int(uid),
                     "states": st, "times": ts})
    return wins


def run_branch(scene: str, seed: int, action: dict | None, n_ue: int = 4):
    """校准 twin 同根执行 (动作少走快点)"""
    tw = CalTwin(scene, n_cell=3, n_ue=n_ue, seed=seed)
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
    tabs = tw.run()
    ue = tabs["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
    out = {}
    for uid, g in ue.groupby("UEID"):
        out[int(uid)] = g[STATE_COLS].to_numpy(dtype=np.float64)
    return out, tw


class CachedExecutor:
    def __init__(self, n_ue: int = 4):
        self.n_ue = n_ue
        self.cache = {}
        self.n_runs = 0

    def run(self, scene: str, action: dict | None) -> dict[int, np.ndarray]:
        key = (scene, None if action is None else tuple(sorted(action.items())))
        if key not in self.cache:
            st, _ = run_branch(scene, SEEDS[scene], action, n_ue=self.n_ue)
            self.cache[key] = st
            self.n_runs += 1
        return self.cache[key]


def actual_reward(states, lo, hi, mu, sd, w):
    z = (states[lo:hi] - mu) / sd
    return float((z @ w).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-windows", type=int, default=16)
    ap.add_argument("--n-ue", type=int, default=8,
                    help="执行器 UE 数 (与窗口 UEID 对齐: ue' = ue_id % n_ue)")
    ap.add_argument("--window-start", default=8000, type=int)
    ap.add_argument("--window-end", default=9200, type=int)
    args = ap.parse_args()

    wins = load_real_windows()
    rng = np.random.default_rng(2026)
    rng.shuffle(wins)
    wins = wins[: args.n_windows]
    print(f"[windows] {len(wins)} held-out real windows")

    # 两个 WM
    wms = {}
    for tag in ["cal", "uncal"]:
        meta = json.loads((MODELS / f"wm_{tag}_meta.json").read_text())
        wm = WorldModel(MODELS / f"wm_{tag}.pt", MODELS / f"wm_{tag}_meta.json")
        wms[tag] = wm

    reward_fn = make_reward(RewardConfig(w=W))
    mu = np.array([-110.0, 15.0, 110.0, 13.0], dtype=np.float64)   # 统一奖励域
    sd = np.array([12.0, 8.0, 60.0, 3.0], dtype=np.float64)

    executor = CachedExecutor(n_ue=args.n_ue)
    results = {"n_windows": len(wins), "windows": [], "per_scene": {},
               "models": {t: {"lifts": [], "rec": []} for t in wms}}

    frac_lo = int(args.window_start / STEP_MS)
    frac_hi = int(args.window_end / STEP_MS)
    n_steps = len(executor.run(wins[0]["scene"], None)[wins[0]["ue"]])

    for w in wins:
        scene, ue_id = w["scene"], w["ue"]
        ue_p = int(ue_id % args.n_ue)                    # twin 中对应 UE (时序对齐)
        # 窗口切在时间轴 8-9.2s (held-out 时间外推区) — 真实与 twin 同刻
        st = w["states"]
        mask = np.where((w["times"] >= args.window_start)
                        & (w["times"] <= args.window_end))[0]
        if len(mask) < HIST + 2:
            continue
        t_start = int(mask[0])
        hist_s = st[t_start - HIST: t_start]           # 真实历史 [H,S]
        off = t_start                                   # 执行器轨迹对齐 (同刻 200 步内)
        # ---- 统一执行器: 该场景同根 twin 分支 (UE 对齐), 两个 WM 共用 ----
        if off + 24 > len(executor.run(scene, None)[ue_p]):
            off = len(executor.run(scene, None)[ue_p]) - 25
        base = executor.run(scene, None)[ue_p]

        row = {"scene": scene, "eid": w["eid"], "ue": ue_id, "ue_p": ue_p,
               "t_ms": args.window_start}
        for tag, wm in wms.items():
            # CEM 决策 (同进程, 固定 planner seed → 确定)
            planner = CEMPlanner(wm=wm, reward=reward_fn,
                                 cem=CEMConfig(seed=7))
            res = planner.plan(hist_s[np.newaxis].astype(np.float32),
                               return_all=False)
            if res["recommend"]:
                atype, aval = res["type"], float(res["value"])
            else:
                atype, aval = "none", 0.0
            # 执行动作 (校准 twin 分支) — 动作加在窗口区间
            if atype == "none":
                branch = base
            else:
                spec = ACTION_SPACE[atype]
                branch = executor.run(scene, {atype: aval})[ue_p]
            real = actual_reward(branch, off, min(off + 24, len(branch)), mu, sd, W)
            row[f"{tag}_action"] = f"{atype}={aval:.1f}"
            row[f"{tag}_real"] = round(real, 4)
            results["models"][tag]["lifts"].append(real)
            results["models"][tag]["rec"].append(1 if atype != "none" else 0)
        results["windows"].append(row)
        print(f"[{scene}] ue={ue_id} cal={row.get('cal_action')} {row.get('cal_real')} "
              f"| uncal={row.get('uncal_action')} {row.get('uncal_real')}")

    # 汇总: 配对 lift (win 内 cal > uncal)
    cal_l = np.array([r["cal_real"] for r in results["windows"]])
    uncal_l = np.array([r["uncal_real"] for r in results["windows"]])
    results["summary"] = {
        "cal_mean_real": round(float(cal_l.mean()), 4),
        "uncal_mean_real": round(float(uncal_l.mean()), 4),
        "cal_minus_uncal_mean": round(float((cal_l - uncal_l).mean()), 4),
        "n_wins_cal": int((cal_l > uncal_l).sum()),
        "n_wins_uncal": int((uncal_l > cal_l).sum()),
        "n_ties": int((cal_l == uncal_l).sum()),
        "cal_action_rate": round(float(np.mean(results["models"]["cal"]["rec"])), 3),
        "uncal_action_rate": round(float(np.mean(results["models"]["uncal"]["rec"])), 3),
    }
    print(json.dumps(results["summary"], indent=1))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[result] {OUT}")


if __name__ == "__main__":
    main()
