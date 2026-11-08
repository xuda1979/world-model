#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校准动作模型闭环评测 — 会话 26
===============================
在两类 held-out 窗口上, 把会话 26 训练的校准版动作模型 (policy_cal / policy_uncal)
与 CEM-MPC 参照做闭环对比, 统一在**校准 twin 权威执行器**上真实施加动作:

  Windows A: 真实网络 held-out 窗 (40 窗, 与 E3 closedloop_calib_compare 同协议,
             seed-2026 shuffle 确定性复现同一窗口集)
  Windows B: 校园孪生窗 (24 窗, 与 E4 campus_action_report 同协议)

策略 × 场景:
  pol-cal   : wm_cal + policy_cal_net (会话 26 蒸馏)
  pol-uncal : wm_uncal + policy_uncal_net
  cem-cal   : wm_cal + CEM-MPC        (参照, 复算 E3/E4)
  cem-uncal : wm_uncal + CEM-MPC
  none      : NoAction 基线 (同窗真实奖励)

输出: docs/closedloop/closedloop_calib_policy.json
用法: python scripts/eval_policy_calib.py [--window-n 40] [--campus-n 24]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.calibrated import TinyTwin as CalTwin
from wm.planner import (WorldModel, CEMPlanner, CEMConfig, RewardConfig,
                        make_reward, ACTION_SPACE)
from wm.data import STATE_COLS, HIST

PKG_DIR = ROOT
MODELS = PKG_DIR / "models"
OUT = PKG_DIR / "docs" / "closedloop" / "closedloop_calib_policy.json"

SEEDS = {"城区": 9000, "郊区": 9007, "低空": 9014, "机器人工厂": 9021,
         "campus": 9028}
W = np.array([0.10, 0.20, 0.60, 0.10], dtype=np.float32)
MU = np.array([-110.0, 15.0, 110.0, 13.0], dtype=np.float64)
SD = np.array([12.0, 8.0, 60.0, 3.0], dtype=np.float64)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 复用 train_planner.PolicyNet (不复制代码)
__tp = _load("tp", ROOT / "scripts" / "train_planner.py")


def load_policy(tag: str):
    """加载会话 26 蒸馏的策略网络 (权重 + meta 归一化)"""
    net = __tp.PolicyNet()
    net.load_state_dict(torch.load(MODELS / f"policy_{tag}_net.pt",
                                   map_location="cpu"))
    net.eval()
    meta = json.loads((MODELS / f"policy_{tag}_meta.json").read_text())
    return net, np.array(meta["mu"], dtype=np.float32), \
        np.array(meta["sd"], dtype=np.float32)


def action_of_policy(net, hist_s, mu, sd) -> tuple[str, float]:
    x = torch.tensor(((hist_s - mu) / sd).reshape(1, -1), dtype=torch.float32)
    with torch.no_grad():
        logits, vals = net(x)
    t_i = int(logits.argmax(1)[0])
    if t_i == 0:
        return "none", 0.0
    atype = __tp.INT_TYPES[t_i]
    val = float(net.denorm_value(vals)[0, t_i - 1])
    return atype, val


def load_real_windows():
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


def run_branch(scene: str, seed: int, action: dict | None, n_ue: int = 8):
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
    return out


class CachedExecutor:
    def __init__(self, n_ue: int = 8):
        self.n_ue = n_ue
        self.cache = {}
        self.n_runs = 0

    def run(self, scene: str, action: dict | None) -> dict[int, np.ndarray]:
        key = (scene, None if action is None else tuple(sorted(action.items())))
        if key not in self.cache:
            self.cache[key] = run_branch(scene, SEEDS[scene], action,
                                         n_ue=self.n_ue)
            self.n_runs += 1
        return self.cache[key]


class CampusExecutor:
    """E4 同构: campus seed=9028, n_ue=8, 动作缓存"""
    def __init__(self):
        self.cache = {}

    def run(self, action: dict | None) -> dict[int, np.ndarray]:
        key = "base" if action is None else tuple(sorted(action.items()))
        if key not in self.cache:
            tw = CalTwin("campus", n_cell=3, n_ue=8, seed=SEEDS["campus"])
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


def actual_reward(states, lo, hi):
    z = (states[lo:hi] - MU) / SD
    return float((z @ W).mean())


def make_planner(wm):
    return CEMPlanner(wm=wm, reward=make_reward(RewardConfig(w=W)),
                      cem=CEMConfig(seed=7))


def decide(wm, policy, hist_s, which, planner=None):
    """返回 (type, value); which=cem 走 CEM-MPC (E4 语义: 传共享 planner 复现
    E4 的共享 RNG 流; 否则新建 = E3 语义), which=policy 走策略网络"""
    if which == "cem":
        if planner is None:
            planner = make_planner(wm)
        res = planner.plan(hist_s[np.newaxis].astype(np.float32), return_all=False)
        if res["recommend"]:
            return res["type"], float(res["value"])
        return "none", 0.0
    net, mu, sd = policy
    return action_of_policy(net, hist_s[np.newaxis].astype(np.float32), mu, sd)


def summarize(name, rewards, none_rewards, actions, n_boot=2000, seed=0):
    arr = np.array(rewards)
    lift = np.array(rewards) - np.array(none_rewards)
    boot = np.array([float(np.mean(np.random.default_rng(seed + s).choice(
        lift, size=len(lift), replace=True))) for s in range(n_boot)])
    dist = {}
    for a in actions:
        at = a.split("=")[0]
        dist[at] = dist.get(at, 0) + 1
    return {
        "strategy": name,
        "n": len(arr),
        "mean_real_reward": round(float(arr.mean()), 4),
        "mean_lift_vs_noaction": round(float(lift.mean()), 4),
        "lift_ci95": [round(float(np.percentile(boot, 2.5)), 4),
                      round(float(np.percentile(boot, 97.5)), 4)],
        "p_lift_positive": round(float((boot > 0).mean()), 4),
        "action_rate": round(float((np.array(actions) != "none=0.0").mean()), 3),
        "action_dist": dist,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-n", type=int, default=40)
    ap.add_argument("--campus-n", type=int, default=24)
    ap.add_argument("--campus-n-ue", type=int, default=8)
    args = ap.parse_args()

    wms = {t: WorldModel(MODELS / f"wm_{t}.pt", MODELS / f"wm_{t}_meta.json")
           for t in ("cal", "uncal")}
    pols = {t: load_policy(t) for t in ("cal", "uncal")}

    # ================= Windows A: 真实网络 held-out (同 E3 协议) ==============
    wins = load_real_windows()
    rng = np.random.default_rng(2026)
    rng.shuffle(wins)
    wins = wins[: args.window_n]
    print(f"[Windows A] {len(wins)} real held-out windows")

    executor = CachedExecutor(n_ue=8)
    strat_keys = [("pol-cal", "policy", "cal"), ("pol-uncal", "policy", "uncal"),
                  ("cem-cal", "cem", "cal"), ("cem-uncal", "cem", "uncal")]
    real = {"n_windows": len(wins), "windows": [],
            "summary": {}, "per_strategy": {}}
    rewards = {k: [] for k, *_ in strat_keys}
    none_rewards = []
    actions = {k: [] for k, *_ in strat_keys}

    for w in wins:
        scene, ue_id = w["scene"], w["ue"]
        ue_p = int(ue_id % 8)
        st = w["states"]
        mask = np.where((w["times"] >= 8000) & (w["times"] <= 9200))[0]
        if len(mask) < HIST + 2:
            continue
        t_start = int(mask[0])
        hist_s = st[t_start - HIST: t_start]
        off = t_start
        base = executor.run(scene, None)[ue_p]
        if off + 24 > len(base):
            off = len(base) - 25
        base = executor.run(scene, None)[ue_p]
        none_r = actual_reward(base, off, min(off + 24, len(base)))
        none_rewards.append(none_r)
        row = {"scene": scene, "eid": w["eid"], "ue": ue_id, "ue_p": ue_p,
               "t_ms": 8000}
        for key, which, tag in strat_keys:
            if which == "cem":
                planner = make_planner(wms[tag])     # E3 语义: 每窗新 CEM
            else:
                planner = None
            atype, aval = decide(wms[tag], pols[tag], hist_s, which, planner)
            branch = base if atype == "none" else executor.run(scene, {atype: aval})[ue_p]
            r = actual_reward(branch, off, min(off + 24, len(branch)))
            row[key] = f"{atype}={aval:.1f}"
            row[key + "_real"] = round(r, 4)
            rewards[key].append(r)
            actions[key].append(f"{atype}={aval:.1f}")
        real["windows"].append(row)

    none_arr = np.array(none_rewards)
    real["per_strategy"] = {k: summarize(k, rewards[k], none_arr, actions[k])
                            for k, *_ in strat_keys}
    cal = real["per_strategy"]["pol-cal"]
    unc = real["per_strategy"]["pol-uncal"]
    real["summary"] = {
        "cal_policy_minus_uncal_policy_mean": round(
            cal["mean_real_reward"] - unc["mean_real_reward"], 4),
        "cal_policy_lift": cal["mean_lift_vs_noaction"],
        "uncal_policy_lift": unc["mean_lift_vs_noaction"],
        "cem_cal_lift": real["per_strategy"]["cem-cal"]["mean_lift_vs_noaction"],
        "cem_uncal_lift": real["per_strategy"]["cem-uncal"]["mean_lift_vs_noaction"],
    }
    print(json.dumps(real["per_strategy"], indent=1, ensure_ascii=False))
    print(json.dumps(real["summary"], indent=1, ensure_ascii=False))

    # ================= Windows B: 校园孪生窗 (同 E4 协议) =====================
    ex = CampusExecutor()
    base = ex.run(None)
    n_steps_c = len(base[0])
    rng = np.random.default_rng(2026)
    cwins = []
    for uid in sorted(base):
        for _ in range(args.campus_n // 8 + 1):
            t = int(rng.integers(160, n_steps_c - 25))
            cwins.append({"ue": uid, "t": t})
    cwins = cwins[: args.campus_n]
    print(f"[Windows B] {len(cwins)} campus twin windows")

    campus = {"n_windows": len(cwins), "windows": [],
              "summary": {}, "per_strategy": {}}
    crewards = {k: [] for k, *_ in strat_keys}
    cnone_rewards = []
    cactions = {k: [] for k, *_ in strat_keys}
    # E4 语义: 每 tag 一个共享 CEM planner (复现 E4 的共享 RNG 流)
    cplanners = {tag: make_planner(wms[tag]) for tag in ("cal", "uncal")}
    for cw in cwins:
        uid, t = cw["ue"], cw["t"]
        hist_s = base[uid][t - HIST: t].astype(np.float32)
        none_r = actual_reward(base[uid], t, min(t + 24, n_steps_c))
        cnone_rewards.append(none_r)
        row = {"ue": uid, "t_ms": int(t * 50)}
        for key, which, tag in strat_keys:
            planner = cplanners[tag] if which == "cem" else None
            atype, aval = decide(wms[tag], pols[tag], hist_s, which, planner)
            branch = base[uid] if atype == "none" else ex.run({atype: aval})[uid]
            r = actual_reward(branch, t, min(t + 24, n_steps_c))
            row[key] = f"{atype}={aval:.1f}"
            row[key + "_real"] = round(r, 4)
            crewards[key].append(r)
            cactions[key].append(f"{atype}={aval:.1f}")
        campus["windows"].append(row)

    cnone_arr = np.array(cnone_rewards)
    campus["per_strategy"] = {k: summarize(k, crewards[k], cnone_arr, cactions[k])
                              for k, *_ in strat_keys}
    ccal = campus["per_strategy"]["pol-cal"]
    cunc = campus["per_strategy"]["pol-uncal"]
    campus["summary"] = {
        "cal_policy_minus_uncal_policy_mean": round(
            ccal["mean_real_reward"] - cunc["mean_real_reward"], 4),
        "cal_policy_lift": ccal["mean_lift_vs_noaction"],
        "uncal_policy_lift": cunc["mean_lift_vs_noaction"],
        "cem_cal_lift": campus["per_strategy"]["cem-cal"]["mean_lift_vs_noaction"],
        "cem_uncal_lift": campus["per_strategy"]["cem-uncal"]["mean_lift_vs_noaction"],
    }
    print(json.dumps(campus["per_strategy"], indent=1, ensure_ascii=False))
    print(json.dumps(campus["summary"], indent=1, ensure_ascii=False))

    out = {
        "note": ("会话 26 校准动作模型闭环评测; Windows A = 真实网络 held-out (E3 协议), "
                 "Windows B = 校园孪生窗 (E4 协议); 统一校准 twin 权威执行器"),
        "real_windows": real,
        "campus_windows": campus,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[result] {OUT}")


if __name__ == "__main__":
    main()
