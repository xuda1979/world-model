#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Twin 动作族: 反事实动作执行数据 (世界模型训练核心)
====================================================
对每个基准 twin episode, 施加动作干预生成"同根分支" (same-root branch):
基准轨迹 (a=∅) 与 动作轨迹 (a≠∅) 共享同一 UE 几何/阴影/噪声随机流
→ (s, a, s') 样本中动作因果效应可精确分离 (反事实差分 Δs = s'|a − s'|∅)。

动作空间 (与真实动作表 9 维对齐):
  功率分配 (Pmax W)          — 发射功率调整 [×0.5 .. ×2.0] (dB: ±3)
  下行 PRB 资源 (比例)        — [0.25 .. 1.0]
  波束方位 (度)              — 扇区方位角偏移 [−30° .. +30°]
  切换偏移 A3-offset (dB)    — [1 .. 6]

输出 (datalake/generated/):
  twin_action_<ep>_<k>_ue.parquet   — 动作分支轨迹 (带 action 列)
  twin_action_dataset.parquet       — 合并 (s,a,s') 数据集 (世界模型直接可用)
  action_report.json                — 动作效应摘要 (ΔRSRP/ΔSINR/ΔDL 按动作类型)

用法: .venv/bin/python world6g/scripts/generate_actions.py [--n-actions 12]
"""
import sys, json, argparse
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datalake.config import GEN_DIR, PKG_DIR
from twin.engine import TinyTwin

SCENES = ["城区", "郊区", "低空", "机器人工厂"]


def run_branch(scene: str, seed: int, action: dict | None, n_ue: int = 8):
    """运行一条分支: action=None 为基准; 否则施加干预。
    共享 seed → UE 初始几何/运动/阴影随机流完全一致 (同根)"""
    tw = TinyTwin(scene, n_cell=3, n_ue=n_ue, seed=seed)
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
    else:
        tw.tx_scale_db = 0.0
        tw.prb_scale = 1.0
        tw.ho_offset_db = 3.0
    tabs = tw.run()
    return tabs, tw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-actions", type=int, default=12)
    args = ap.parse_args()
    rng = np.random.default_rng(777)

    # 动作空间采样 (每个动作 = 一条同根分支)
    actions = [{"label": "baseline", "params": None}]
    for k in range(args.n_actions):
        r = rng.random()
        if r < 0.25:
            p = {"pmax_scale": float(2 ** rng.uniform(-1, 1))}          # ×0.5..×2
            lab = f"pwr×{p['pmax_scale']:.2f}"
        elif r < 0.5:
            p = {"prb_scale": float(rng.uniform(0.25, 1.0))}
            lab = f"prb×{p['prb_scale']:.2f}"
        elif r < 0.75:
            p = {"az_offset": float(rng.uniform(-30, 30))}
            lab = f"az{p['az_offset']:+.0f}°"
        else:
            p = {"ho_offset": float(rng.uniform(1, 6))}
            lab = f"ho{p['ho_offset']:.1f}dB"
        actions.append({"label": lab, "params": p})

    dataset_rows, report = [], []
    for si, scene in enumerate(SCENES):
        seed = 9000 + si * 13
        print(f"=== {scene} (seed={seed}) ===")
        # 基准分支
        base_tabs, _ = run_branch(scene, seed, None)
        base_ue = base_tabs["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
        for a in actions:
            tabs, tw = run_branch(scene, seed, a["params"])
            ue = tabs["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
            # 同根校验: UEID/时间对齐
            assert (ue["UEID"] == base_ue["UEID"]).all(), "branch misaligned"
            # ΔKPI (动作因果效应, 每 UE×时刻)
            for col, name in [("服务小区RSRP(dBm)", "rsrp"), ("服务小区SINR(dB)", "sinr"),
                              ("下行速率(Mbps)", "dl"), ("CQI", "cqi")]:
                ue[f"Δ{name}"] = ue[col] - base_ue[col]
            ue["action"] = a["label"]
            ue["action_type"] = ("baseline" if a["params"] is None else
                                 next(iter(a["params"])))
            ue["action_value"] = 0.0 if a["params"] is None else next(iter(a["params"].values()))
            ue["branch_scene"] = scene
            dataset_rows.append(ue)
            if a["params"] is not None:
                report.append({
                    "scene": scene, "action": a["label"], "type": ue["action_type"].iloc[0],
                    "value": float(ue["action_value"].iloc[0]),
                    "mean_Δrsrp": round(float(ue["Δrsrp"].mean()), 2),
                    "mean_Δsinr": round(float(ue["Δsinr"].mean()), 2),
                    "mean_Δdl": round(float(ue["Δdl"].mean()), 2),
                    "mean_Δcqi": round(float(ue["Δcqi"].mean()), 2),
                })
                print(f"  {a['label']:14s} Δrsrp={report[-1]['mean_Δrsrp']:+7.2f} "
                      f"Δsinr={report[-1]['mean_Δsinr']:+6.2f} Δdl={report[-1]['mean_Δdl']:+8.2f}")

    ds = pd.concat(dataset_rows, ignore_index=True)
    ds.to_parquet(GEN_DIR / "twin_action_dataset.parquet", index=False, compression="zstd")
    (GEN_DIR / "action_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n[dataset] twin_action_dataset.parquet: {len(ds):,} rows × {len(ds.columns)} cols")
    print(f"  branches: {len(actions)} actions × {len(SCENES)} scenes + {len(SCENES)} baselines")
    print(f"  actions: {[a['label'] for a in actions][:8]}...")


if __name__ == "__main__":
    main()
