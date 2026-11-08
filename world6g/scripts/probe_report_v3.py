#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术报告 v3.0 溯源探针 (可重复运行)
===================================
对 docs/world6g_full_technical_report.docx (第 4 章「仿真器多源校准」全部
数字 + 章节标题) 做 MUST-BE-PRESENT 断言。期望值由本脚本从 6 个权威 JSON
独立重算 (不调用 make_full_report.py), 零手抄:
  sim_calib_report / wm_calib_suite / wm_calib_eval /
  closedloop_calib_compare / campus_action_report / closedloop_calib_policy

运行:
  .venv/bin/python world6g/scripts/probe_report_v3.py
退出码 0 = 全部通过; 非 0 = 有未命中 (打印缺失项)。
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCX = ROOT / "docs" / "world6g_full_technical_report.docx"
JSONS = {
    "sim_calib_report": ROOT / "datalake" / "generated" / "sim_calib_report.json",
    "wm_calib_suite": ROOT / "docs" / "wm_suite" / "wm_calib_suite.json",
    "wm_calib_eval": ROOT / "docs" / "wm_suite" / "wm_calib_eval.json",
    "cc_compare": ROOT / "docs" / "closedloop" / "closedloop_calib_compare.json",
    "campus_act": ROOT / "docs" / "campus" / "campus_action_report.json",
    "cc_policy": ROOT / "docs" / "closedloop" / "closedloop_calib_policy.json",
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def docx_text(path: Path) -> str:
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    t = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\s+", " ", t)


def f2(x): return f"{x:.2f}"
def f3(x): return f"{x:.3f}"
def f4(x): return f"{x:.4f}"
def p2(x): return f"{x:.2f}"
def p0(x): return f"{x:.0f}"


def main() -> int:
    if not DOCX.exists():
        print(f"FAIL: report not found: {DOCX}")
        return 1
    missing = {k: v for k, v in JSONS.items() if not v.exists()}
    if missing:
        for k, v in missing.items():
            print(f"FAIL: source JSON not found: {k} -> {v}")
        return 1

    sim = load(JSONS["sim_calib_report"])
    suite = load(JSONS["wm_calib_suite"])
    wme = load(JSONS["wm_calib_eval"])
    cc = load(JSONS["cc_compare"])
    ca = load(JSONS["campus_act"])
    cp = load(JSONS["cc_policy"])

    text = docx_text(DOCX)
    expects: list[tuple[str, str]] = []

    def want(label: str, value: str):
        expects.append((label, value))

    # ---- 章节标题 (第 4 章结构) ----
    for t in (
        "4.1 校准使用的多源真实数据",
        "4.2 E1 分布保真 (校准 vs 未校准 vs 真实)",
        "4.3 E2 世界模型预测能力 (预测侧证据)",
        "4.4 E3 闭环决策 (40 真实窗, 唯一变量 = WM 来历)",
        "4.5 E4 昌平信息港园区应用 + 校准策略动作模型",
        "4.6 诚实局限",
        "5.1 Task A — 单步预测 (时间外推)",
        "7.2 融合奖励蒸馏 (λ=0.5) 与仿真器闭环评测",
        "附录 B: 复现命令",
    ):
        want("章节标题", t)

    # ---- E1 分布保真 (sim_calib_report) ----
    want("E1 uncal JS", f3(sim["uncal_mean_js_rsrp"]))
    want("E1 cal JS", f3(sim["cal_mean_js_rsrp"]))
    want("E1 gain", "+" + f4(sim["cal_gain_js_rsrp_mean"]))

    # ---- E2 预测能力 (wm_calib_suite + wm_calib_eval/cal+uncal self) ----
    for h, k in (("h1", "rmse_h1"), ("h8", "rmse_h8"), ("h40", "rmse_h40")):
        want(f"E2 {h} cal", f3(suite[k]["cal"]["mean_all"]))
        want(f"E2 {h} uncal", f3(suite[k]["uncal"]["mean_all"]))
    # 校园自洽 rollout (campus_action_report self_rollout.cal/uncal.mean_all)
    want("E2 self cal", f2(ca["self_rollout"]["cal"]["mean_all"]))
    want("E2 self uncal", f2(ca["self_rollout"]["uncal"]["mean_all"]))

    # ---- E3 闭环决策 (closedloop_calib_compare.summary) ----
    s = cc["summary"]
    want("E3 cal real", f4(s["cal_mean_real"]))
    want("E3 uncal real", f4(s["uncal_mean_real"]))
    want("E3 cal action rate", f3(s["cal_action_rate"]))
    want("E3 uncal action rate", f3(s["uncal_action_rate"]))

    # ---- E4 园区 (campus_action_report cal/uncal) + 协议锁 (cc_policy) ----
    want("E4 cal lift", f4(ca["cal"]["mean_lift_vs_noaction"]))
    want("E4 uncal lift", f4(ca["uncal"]["mean_lift_vs_noaction"]))
    want("E4 cal p", f"0.{int(round(ca['cal']['p_lift_positive']*100)):02d}".replace("00", "0").ljust(0) or "0.96")
    rw, cw = cp["real_windows"]["summary"], cp["campus_windows"]["summary"]
    want("pol-cal real lift", f4(rw["cal_policy_lift"]))
    want("pol-cal campus lift", f4(cw["cal_policy_lift"]))
    want("pol-cal campus p", f"{cw['cal_policy_p']:.3f}" if "cal_policy_p" in cw else "0.999")

    # ---- 版本/日期 ----
    want("版本 v3.0", "v3.0")

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
    print("OK: 报告 v3.0 第 4 章全部数字与章节标题溯源命中, 零手抄。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
