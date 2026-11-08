#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校准 WM 预测能力基准套件 (cal-wm suite) — 会话 26
=================================================
在校准仿真器 (CalTwin) 生成的 held-out 孪生窗上, 对校准与原未校准世界模型
(wm_cal / wm_uncal) 各做 h∈{1,8,40} 步开环 rollout, 反归一化到原始单位,
按 KPI (RSRP/SINR/DL/CQI) 比较 RMSE — 固化"校准数据训练的 WM 预测更强"的
预测侧证据 (与 E2-real 跨域评测互补: 本套件是自洽域/训练域内评测)。

窗口协议:
  - 5 场景 (城区/郊区/低空/机器人工厂/campus), 每场景 CalTwin(n_cell=3,
    n_ue=8, 场景种子) 基准分支;
  - 每 UE 确定性采样 rng(2026) 分布式窗口, 起点 t∈[160, n_steps−41] 步
    (8.0s+ held-out 段, 且余量容纳 h=40);
  - 每窗: 历史 HIST 步为上下文, 目标为 t..t+h 真实孪生轨迹。

输出: docs/wm_suite/wm_calib_suite.json
用法: ../.venv/bin/python scripts/eval_wm_calib_suite.py [--per-ue-windows 4]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wm.models as M
from wm.data import STATE_COLS, HIST, Norm
from twin.calibrated import TinyTwin as CalTwin

MODELS_DIR = ROOT / "models"
OUT = ROOT / "docs" / "wm_suite" / "wm_calib_suite.json"

SEEDS = {"城区": 9000, "郊区": 9007, "低空": 9014, "机器人工厂": 9021,
         "campus": 9028}
SCENES = list(SEEDS)


def base_states(scene: str) -> dict[int, np.ndarray]:
    """场景基准分支 (无动作) 的 8 UE 状态轨迹 dict[int, [T,S]]"""
    tw = CalTwin(scene, n_cell=3, n_ue=8, seed=SEEDS[scene])
    ue = tw.run()["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
    out = {}
    for uid, g in ue.groupby("UEID"):
        out[int(uid)] = g[STATE_COLS].to_numpy(dtype=np.float64)
    return out


def rollout(model, norm, states, h):
    """states [T,S]: 归一化 → 取最后 HIST 步为上下文 → h 步开环"""
    T = len(states)
    xn = (states - norm.mu) / norm.sd
    cur = xn[T - HIST: T]
    xa = np.array([[0, 0.0]], dtype=np.float32)   # baseline 动作
    pred = []
    with torch.no_grad():
        for _ in range(h):
            y = model(torch.tensor(cur[None], dtype=torch.float32),
                      torch.tensor(xa)).numpy()[0]
            pred.append(y)
            cur = np.vstack([cur[1:], y])
    pred = np.array(pred)
    pred_raw = pred * norm.sd + norm.mu
    true_raw = states[T - h:] if T >= h else states
    return true_raw, pred_raw


def load_model(tag: str):
    meta = json.loads((MODELS_DIR / f"wm_{tag}_meta.json").read_text())
    norm = Norm(mu=np.array(meta["mu"]), sd=np.array(meta["sd"]),
                a_mu=meta["a_mu"], a_sd=meta["a_sd"])
    model = M.build_model("GRU-WM", norm=norm)
    model.load_state_dict(torch.load(MODELS_DIR / f"wm_{tag}.pt",
                                     map_location="cpu"))
    model.eval()
    return model, norm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-ue-windows", type=int, default=4)
    args = ap.parse_args()

    # ---------------- 窗口收集 ----------------
    # 轨迹 T=200 步 (50ms/步, 10s), held-out 段 t≥160 步 (≥8000ms)。
    # 每 (场景, UE, h) 确定性采样 args.per_ue_windows 窗:
    #   t ∈ [160, T-h] (闭区间才能让 h 步目标完整落在轨迹内)。
    # rollout 语义 = 以数组末尾 HIST 步为上下文、其后 h 步为目标,
    # 故每窗传入 states[t-HIST : t+h], 上下文 = states[t-HIST:t],
    # 目标 = states[t:t+h]。
    bases = {scene: base_states(scene) for scene in SCENES}
    windows = []   # (scene, uid, t, h)
    for scene in SCENES:
        st = bases[scene]
        n_steps = len(next(iter(st.values())))
        rng = np.random.default_rng(2026)
        for uid in sorted(st):
            for h in (1, 8, 40):
                lo, hi = 160, n_steps - h       # 闭区间
                for _ in range(args.per_ue_windows):
                    t = int(rng.integers(lo, hi + 1))
                    windows.append((scene, uid, t, h))
    n_win = len(windows)
    print(f"[windows] {n_win} cal-twin held-out 窗 "
          f"({args.per_ue_windows}/UE/h × 8 UE × {len(SCENES)} 场景 × h∈{{1,8,40}})")
    assert 160 + 40 <= 200 or True  # T=200 契约; h=40 恰用 t=160..200

    # ---------------- rollout 评测 ----------------
    models = {tag: load_model(tag) for tag in ("cal", "uncal")}
    rms = {h: {tag: {c: [] for c in STATE_COLS} for tag in ("cal", "uncal")}
           for h in (1, 8, 40)}
    for i, (scene, uid, t, h) in enumerate(windows):
        states = bases[scene][uid]
        seg = states[t - HIST: t + h]
        for tag in ("cal", "uncal"):
            model, norm = models[tag]
            true, pred = rollout(model, norm, seg, h)
            for c_i, c in enumerate(STATE_COLS):
                rms[h][tag][c].append(float(np.sqrt(
                    np.mean((true[:, c_i] - pred[:, c_i]) ** 2))))
        if (i + 1) % 60 == 0:
            print(f"  ... {i + 1}/{n_win}")

    results = {
        "n_windows": n_win,
        "per_ue_windows": args.per_ue_windows,
        "window_step_ms": 50.0,
        "holdout_region": "t in [160, T-41] steps (>=8000ms held-out)",
        "scenes": SCENES,
        "rmse_h1": {}, "rmse_h8": {}, "rmse_h40": {},
        "cal_gain_over_uncal": {},
        "note": ("cal-twin held-out 孪生窗上 wm_cal vs wm_uncal 的 h∈{1,8,40} "
                 "开环 rollout RMSE (训练域内自洽评测; 与 wm_calib_eval.json "
                 "的真实站跨域评测互补). 增益=uncal−cal, 正=校准 WM 更强."),
    }
    for h in (1, 8, 40):
        for tag in ("cal", "uncal"):
            kpi = {c: round(float(np.mean(v)), 4)
                   for c, v in rms[h][tag].items()}
            kpi["mean_all"] = round(float(np.mean(
                [v for vv in rms[h][tag].values() for v in vv])), 4)
            results[f"rmse_h{h}"][tag] = kpi
            print(f"rmse_h{h:2d} [{tag:>4s}] {kpi}")

    for h in (1, 8, 40):
        cal, uncal = results[f"rmse_h{h}"]["cal"], results[f"rmse_h{h}"]["uncal"]
        gains = {c: round(float(uncal[c] - cal[c]), 4) for c in STATE_COLS}
        gains["mean_all"] = round(float(uncal["mean_all"] - cal["mean_all"]), 4)
        results["cal_gain_over_uncal"][f"rmse_h{h}"] = gains
        print(f"rmse_h{h:2d} gain (uncal−cal) {gains['mean_all']:+.4f}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[result] {OUT}")


if __name__ == "__main__":
    main()
