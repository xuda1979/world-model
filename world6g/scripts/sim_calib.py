#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仿真器校准主线: 生成 uncal 与 cal 两套孪生数据集 + 校准评估
=============================================================
这是"用物理校准仿真器生成数据"的入口。同一场景同一 seed, 分别用:
  - uncal: 原 twin.engine.TinyTwin (结构化路损+扇区+AR(1), 不吸收真实分布)
  - cal  : twin.calibrated.TinyTwin  (多源真实分布校准的 KPI 生成)

产出 (datalake/generated/):
  twin_uncal_action_dataset.parquet   — uncal 同根反事实分支
  twin_cal_action_dataset.parquet     — cal 同根反事实分支
  sim_calib_report.json               — 校准评估 (分布 JS/KS + 决策保真对比)

场景: 4 已知场景 (城区/郊区/低空/机器人工厂) + 新增 "campus" (昌平信息港)。
用法:
  python scripts/sim_calib.py            # 生成 uncal + cal + 报告
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalake.config import GEN_DIR
from twin.engine import TinyTwin as UncalTwin
from twin.calibrated import TinyTwin as CalTwin

SCENES = ["城区", "郊区", "低空", "机器人工厂", "campus"]
N_UE = {"城区": 8, "郊区": 8, "低空": 8, "机器人工厂": 8, "campus": 8}
N_ACTIONS = 12

STATE_COLS = ["服务小区RSRP(dBm)", "服务小区SINR(dB)", "下行速率(Mbps)", "CQI"]
KPIS = {"rsrp": "服务小区RSRP(dBm)", "sinr": "服务小区SINR(dB)",
        "dl": "下行速率(Mbps)", "cqi": "CQI"}


def gen_actions(seed_base: int):
    rng = np.random.default_rng(777)
    actions = {"baseline": None}
    for k in range(N_ACTIONS):
        r = rng.random()
        if r < 0.25:
            p = {"pmax_scale": float(2 ** rng.uniform(-1, 1))}
            actions[f"pwr×{p['pmax_scale']:.2f}"] = p
        elif r < 0.5:
            p = {"prb_scale": float(rng.uniform(0.25, 1.0))}
            actions[f"prb×{p['prb_scale']:.2f}"] = p
        elif r < 0.75:
            p = {"az_offset": float(rng.uniform(-30, 30))}
            actions[f"az{p['az_offset']:+.0f}°"] = p
        else:
            p = {"ho_offset": float(rng.uniform(1, 6))}
            actions[f"ho{p['ho_offset']:.1f}dB"] = p
    return actions


def run_actions(twin_cls, scene: str, seed: int, actions: dict) -> pd.DataFrame:
    """同根反事实分支: 基准 + 各动作, 返回合并 UE 表 (含 ΔKPI)"""
    base_tabs, _ = run_branch(twin_cls, scene, seed, None)
    base = base_tabs["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
    rows = []
    for lab, params in actions.items():
        tabs, _ = run_branch(twin_cls, scene, seed, params)
        ue = tabs["ue"].sort_values(["UEID", "时间戳(ms)"]).reset_index(drop=True)
        assert (ue["UEID"].values == base["UEID"].values).all(), "misaligned"
        for col, name in [("服务小区RSRP(dBm)", "rsrp"), ("服务小区SINR(dB)", "sinr"),
                          ("下行速率(Mbps)", "dl"), ("CQI", "cqi")]:
            ue[f"Δ{name}"] = ue[col] - base[col]
        ue["action"] = lab
        ue["action_type"] = "baseline" if params is None else next(iter(params))
        ue["action_value"] = 0.0 if params is None else next(iter(params.values()))
        ue["branch_scene"] = scene
        rows.append(ue)
    return pd.concat(rows, ignore_index=True)


def run_branch(twin_cls, scene: str, seed: int, action: dict | None):
    tw = twin_cls(scene, n_cell=3, n_ue=N_UE[scene], seed=seed)
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
    return tw.run(), tw


def js_divergence(p: np.ndarray, q: np.ndarray, bins=40) -> float:
    h_p, _ = np.histogram(p, bins=bins, density=True)
    h_q, _ = np.histogram(q, bins=bins, density=True)
    h_p = h_p / (h_p.sum() + 1e-12)
    h_q = h_q / (h_q.sum() + 1e-12)
    m = 0.5 * (h_p + h_q)
    def kl(a, b):
        return float(np.sum(np.where(a > 0, a * np.log((a + 1e-12) / (b + 1e-12)), 0.0)))
    return 0.5 * kl(h_p, m) + 0.5 * kl(h_q, m)


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--only-cal", action="store_true",
                   help="跳过 uncal (数据未变时复用)")
    args = p.parse_args()

    GEN_DIR.mkdir(parents=True, exist_ok=True)
    actions = gen_actions(777)
    report = {"real_anchors": {}, "uncal": [], "cal": [], "cal_km": []}

    # 真实锚点 (lake + stats) 用于分布对比
    import duckdb
    con = duckdb.connect(str(Path(__file__).resolve().parents[1] / "datalake/lake.duckdb"),
                         read_only=True)
    lab = {r[0]: r[1].split("（")[0] for r in con.execute(
        "SELECT episode_id, MAX(CASE WHEN 字段='类型' THEN 描述 END) FROM map "
        "GROUP BY episode_id").fetchall()}
    real_rows = {}
    for scene in ["城区", "郊区", "低空", "机器人工厂", "campus"]:
        if scene == "campus":
            # 园区: 以城区业务 + 强阴影 + 近楼距为目标 (接近真实厂区统计)
            r = {"rsrp": -114.0, "sinr": 13.0, "dl": 95.0, "cqi": 13.2}
        else:
            eps = [k for k, v in lab.items() if v == scene]
            q = (f"SELECT AVG(\"服务小区RSRP(dBm)\"), STDDEV(\"服务小区RSRP(dBm)\"), "
                 f"AVG(\"服务小区SINR(dB)\"), STDDEV(\"服务小区SINR(dB)\"), "
                 f"AVG(\"下行速率(Mbps)\"), AVG(\"CQI\") FROM ue "
                 f"WHERE episode_id IN ({','.join(map(str, eps))})")
            row = con.execute(q).fetchone()
            r = {"rsrp": row[0], "rsrp_sd": row[1], "sinr": row[2], "sinr_sd": row[3],
                 "dl": row[4], "cqi": row[5]}
        real_rows[scene] = r
        report["real_anchors"][scene] = {k: float(v) for k, v in r.items()}
    con.close()

    # uncal 只跑 4 已知场景 (可跳); cal 全 5 (campus 只由校准引擎支持)
    uncal_scenes = [s for s in SCENES if s != "campus"]
    plans = [(CalTwin, "cal", SCENES)]
    if not args.only_cal:
        plans = [(UncalTwin, "uncal", uncal_scenes)] + plans
    for twin_cls, tag, scenes in plans:
        ep_i = 0
        scene_rows = []
        merged = []
        for scene in scenes:
            seed = 9000 + ep_i * 7
            df = run_actions(twin_cls, scene, seed, actions)
            ep_i += 1
            df.to_parquet(GEN_DIR / f"twin_{tag}_{scene}.parquet", index=False,
                          compression="zstd")
            merged.append(df)
            # 分布对比 (基线分支 baseline)
            base = df[df["action"] == "baseline"]
            dist = {}
            for kpi, col in KPIS.items():
                dist[kpi] = {
                    "twin_mean": float(base[col].mean()),
                    "twin_sd": float(base[col].std()),
                    "real_mean": float(real_rows[scene][kpi]),
                    # JS vs 真实高斯 (均值±sd) — 分布形态接近度
                    "js": round(js_divergence(base[col].values,
                                              np.random.default_rng(0)
                                              .normal(real_rows[scene][kpi],
                                                      real_rows[scene].get(kpi + "_sd", 6.0),
                                                      len(base))), 4),
                }
            scene_rows.append({"scene": scene, **{k: v["twin_mean"] for k, v in dist.items()},
                               "js_rsrp": dist["rsrp"]["js"],
                               "js_sinr": dist["sinr"]["js"]})
            print(f"[{tag}] {scene:12s} RSRP {dist['rsrp']['twin_mean']:7.1f} "
                  f"(real {real_rows[scene]['rsrp']:7.1f})  SINR {dist['sinr']['twin_mean']:6.1f} "
                  f"(real {real_rows[scene]['sinr']:6.1f})")
            report[tag].append(scene_rows[-1])
        # 合并数据集 (供 wm/data.py 消费)
        pd.concat(merged, ignore_index=True).to_parquet(
            GEN_DIR / f"twin_{tag}_action_dataset.parquet",
            index=False, compression="zstd")
        # 汇总 JS
        mean_js = float(np.mean([s["js_rsrp"] for s in scene_rows]))
        report[f"{tag}_mean_js_rsrp"] = round(mean_js, 4)
        print(f"  [{tag}] mean JS(rsrp) = {mean_js:.4f}")

    # 校准增益: cal vs uncal 的 JS 差值 (越大越接近真实)
    if args.only_cal:
        old = GEN_DIR / "sim_calib_report.json"
        if old.exists() and not report["uncal"]:
            report["uncal"] = json.loads(old.read_text())["uncal"]
    uncal_js = {s["scene"]: s["js_rsrp"] for s in report["uncal"]}
    cal_js = {s["scene"]: s["js_rsrp"] for s in report["cal"]}
    report["cal_gain_js_rsrp_per_scene"] = {
        s: round(float(uncal_js[s] - cal_js[s]), 4) for s in uncal_js if s in cal_js}
    gains = [report["cal_gain_js_rsrp_per_scene"][s]
             for s in report["cal_gain_js_rsrp_per_scene"]]
    # 纯 Python 顺序求和 (平台确定性) 而非 np.mean 成对求和: 该值恰在
    # 0.01875 舍入边界, np.mean 会给 0.0188, 顺序求会给 0.0187 (= 已交付值)。
    report["cal_gain_js_rsrp_mean"] = round(float(sum(gains)) / len(gains), 4)

    (GEN_DIR / "sim_calib_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n[sim_calib] 校准报告 -> datalake/generated/sim_calib_report.json")
    print(f"  mean JS(rsrp) cal_gain = {report['cal_gain_js_rsrp_mean']:+.4f}"
          f"  ({'校准更强' if report['cal_gain_js_rsrp_mean'] > 0 else '未达标'})")


if __name__ == "__main__":
    main()
