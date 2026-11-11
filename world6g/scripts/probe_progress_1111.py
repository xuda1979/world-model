#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周报 2026-11-11 docx 溯源探针 (可重复运行)
==========================================
对 docs/progress_report_20261111.docx 中全部关键数字做 MUST-BE-PRESENT 断言。
期望值由本脚本从权威 JSON 独立重算 (零手抄), 格式化与
make_progress_report_1111.py 严格一致:
  coord_seq_loso / coord_seq_wm_meta / coord_seq_policy_meta /
  coord_policy_meta / coord_policy_head_sweep / closedloop_calib_compare

运行:
  .venv/bin/python world6g/scripts/probe_progress_1111.py
退出码 0 = 全部通过; 非 0 = 有未命中 (打印缺失项)。
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCX = ROOT / "docs" / "progress_report_20261111.docx"
JSONS = {
    "loso": ROOT / "datalake" / "generated" / "coord_seq_loso.json",
    "swm": ROOT / "models" / "coord_seq_wm_meta.json",
    "spm": ROOT / "models" / "coord_seq_policy_meta.json",
    "cpm": ROOT / "models" / "coord_policy_meta.json",
    "sweep": ROOT / "datalake" / "generated" / "coord_policy_head_sweep.json",
    "cc": ROOT / "docs" / "closedloop" / "closedloop_calib_compare.json",
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def docx_text(path: Path) -> str:
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    t = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\s+", " ", t)


def main() -> int:
    if not DOCX.exists():
        print(f"FAIL: report not found: {DOCX}")
        return 1
    missing = {k: v for k, v in JSONS.items() if not v.exists()}
    if missing:
        for k, v in missing.items():
            print(f"FAIL: source JSON not found: {k} -> {v}")
        return 1

    loso = load(JSONS["loso"])
    swm = load(JSONS["swm"])
    spm = load(JSONS["spm"])
    cpm = load(JSONS["cpm"])
    sweep = load(JSONS["sweep"])
    cc = load(JSONS["cc"])

    ss, swr = loso["summary"], loso["win_rate_vs_best_const"]
    m2 = loso["match_rates_mean"]
    cp_s = cpm["loso_summary"]
    per_scene = loso["per_scene_mean_return"]

    text = docx_text(DOCX)
    expects: list[tuple[str, str]] = []

    def want(label: str, value: str):
        expects.append((label, value))

    # ---- 周报关键数字 (格式化与 make_progress_report_1111.py 一致) ----
    want("E3 cal 均值奖励", f"{cc['summary']['cal_mean_real']:.2f}")
    want("E3 uncal 均值奖励", f"{cc['summary']['uncal_mean_real']:.2f}")
    want("E3 cal 动作率", f"{cc['summary']['cal_action_rate']:.3f}")
    want("coord policy 实现率", f"{cp_s['coord_gain_realized_ratio']*100:.1f}%")
    want("cp regret mean", f"{cp_s['regret_mean']:.3f}")
    want("sweep value regret", f"{sweep['agg']['value']['regret_mean']:.3f}")
    want("sweep score regret", f"{sweep['agg']['score']['regret_mean']:.3f}")
    want("seq wm 参数量", f"{swm['params']:,}")
    want("seq wm reward r2", f"{swm['loso_heldout_reward']['reward_r2']:.3f}")
    want("seq wm reward mae", f"{swm['loso_heldout_reward']['reward_mae']:.3f}")
    want("seq pol 训练轨迹数", f"{spm['n_train']:,}")
    want("seq pol 训练 acc", f"{spm['train_acc']*100:.1f}%")
    want("BC-planner 一致率", f"{loso['bc_heldout_planner_agreement_mean']*100:.1f}%")
    for ctl in ("zero", "random", "best_const", "wm_greedy", "bc_policy", "sim_greedy"):
        want(f"seq {ctl} 回报", f"{ss[ctl]['mean']:.2f}")
    for ctl, nd in (("random", 0), ("wm_greedy", 0), ("bc_policy", 0), ("sim_greedy", 0)):
        want(f"seq {ctl} 胜率", f"{swr[ctl]*100:.{nd}0f}%")
    want("match bc_vs_sim", f"{m2['bc_vs_sim']*100:.1f}%")
    want("match wm_vs_sim", f"{m2['wm_vs_sim']*100:.1f}%")
    want("runtime_s", f"{loso['runtime_s']:.0f}")
    for scene in ("campus", "城区", "郊区", "低空", "机器人工厂"):
        want(f"scene {scene} bc", f"{per_scene[scene]['bc_policy']:.2f}")
        want(f"scene {scene} sim", f"{per_scene[scene]['sim_greedy']:.2f}")

    fails: list[tuple[str, str]] = []
    for label, v in expects:
        if v in text:
            print(f"PASS  {label:22s} -> {v}")
        else:
            print(f"FAIL  {label:22s} -> {v}  (MISSING in docx)")
            fails.append((label, v))

    print("-" * 60)
    print(f"TOTAL {len(expects)}  PASS {len(expects)-len(fails)}  FAIL {len(fails)}")
    if fails:
        for l, v in fails:
            print(f"  MISSING: {l}: {v}")
        return 1
    print("OK: 周报 2026-11-11 docx 全部关键数字溯源命中, 零手抄。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
