#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
世界模型校准对比评测: cal-WM vs uncal-WM 在真实 data 上的预测能力
=================================================================
在 4 已知场景的真实 episodes (held-out, 前 8s 用历史 / 8000ms+ 预测) 上,
用两个 WM 分别做 h∈{1,8,40} 步开环 rollout, 反归一化到原始单位,
比较 RMSE (RSRP/SINR/DL/CQI) 与曲线形态。

结论口径: 校准数据训练出的 WM 应在真实轨迹预测上更准 (RMSE 更低)。

用法: python scripts/eval_wm_calib.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wm.models as M
from wm.data import STATE_COLS, HIST, Norm

PKG_DIR = Path(__file__).resolve().parents[1]
MODELS_DIR = PKG_DIR / "models"
OUT = PKG_DIR / "docs" / "wm_suite" / "wm_calib_eval.json"


def load_real_windows():
    """真实 ue 轨迹 (4 场景 episodes), 前 8s 历史, 8000ms+ 目标"""
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
    groups = []   # (scene, states [T,S], times)
    for (scene, eid, uid), g in ue.groupby(["scene", "episode_id", "UEID"]):
        g = g.sort_values("时间戳(ms)")
        st = g[STATE_COLS].values.astype(np.float32)
        if len(st) < HIST + 40:
            continue
        groups.append((scene, st, g["时间戳(ms)"].values.astype(np.float64)))
    return groups


def rollout(model, norm, states, h):
    """真实 states [T,S]: 用 norm 归一化, 取最后 HIST 步做 h 步开环"""
    T = len(states)
    xn = (states - norm.mu) / norm.sd
    hist = xn[T - HIST: T]
    pred = []
    cur = hist
    xa = np.array([[0, 0.0]], dtype=np.float32)   # baseline
    with torch.no_grad():
        for _ in range(h):
            y = model(torch.tensor(cur[None], dtype=torch.float32),
                      torch.tensor(xa)).numpy()[0]
            pred.append(y)
            cur = np.vstack([cur[1:], y])
    pred = np.array(pred)                          # [h,S] 归一化
    pred_raw = pred * norm.sd + norm.mu
    true_raw = states[-h:] if T >= h else states
    return true_raw, pred_raw


def main():
    groups = load_real_windows()
    print(f"real windows: {len(groups)}")

    results = {"n_windows": len(groups), "windows": groups[:0],
               "models": {}}
    for tag in ["cal", "uncal"]:
        meta = json.loads((MODELS_DIR / f"wm_{tag}_meta.json").read_text())
        norm = Norm(mu=np.array(meta["mu"]), sd=np.array(meta["sd"]),
                    a_mu=meta["a_mu"], a_sd=meta["a_sd"])
        model = M.build_model("GRU-WM", norm=norm)
        model.load_state_dict(torch.load(MODELS_DIR / f"wm_{tag}.pt",
                                         map_location="cpu"))
        model.eval()
        rmses = {h: {c: [] for c in STATE_COLS} for h in (1, 8, 40)}
        for scene, st, _ts in groups:
            for h in rmses:
                true, pred = rollout(model, norm, st, h)
                for i, c in enumerate(STATE_COLS):
                    rmses[h][c].append(float(np.sqrt(
                        np.mean((true[:, i] - pred[:, i]) ** 2))))
        results["models"][tag] = {
            f"rmse_h{h}": {c: round(float(np.mean(v)), 4) for c, v in rc.items()}
            for h, rc in rmses.items()}
        results["models"][tag]["mean_all"] = round(float(np.mean(
            [v for rc in rmses.values() for x in rc.values() for v in x])), 4)
        print(f"[{tag}] rmse_h1 = {results['models'][tag]['rmse_h1']}")
        print(f"       rmse_h8 = {results['models'][tag]['rmse_h8']}")
        print(f"       rmse_h40 = {results['models'][tag]['rmse_h40']}")

    # 对比
    cal, uncal = results["models"]["cal"], results["models"]["uncal"]
    gains = {}
    for h in (1, 8, 40):
        k = f"rmse_h{h}"
        gains[k] = {c: round(float(uncal[k][c] - cal[k][c]), 4)
                    for c in STATE_COLS}
    gains["mean_all"] = round(float(uncal["mean_all"] - cal["mean_all"]), 4)
    results["cal_gain_over_uncal"] = gains
    print(f"\n[增益] mean_all = {gains['mean_all']:+.4f} (uncal−cal; 正=cal 更强)")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[result] {OUT}")


if __name__ == "__main__":
    main()
