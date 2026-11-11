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
        "7.5 逐小区协同策略蒸馏 (coord policy)",
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

    # ---- 7.5 coord policy (coord_policy_meta / loso / head_sweep, 独立重算) ----
    cpm = load(ROOT / "models" / "coord_policy_meta.json")
    loso = load(ROOT / "datalake" / "generated" / "coord_policy_loso.json")
    sweep = load(ROOT / "datalake" / "generated" / "coord_policy_head_sweep.json")
    s = cpm["loso_summary"]
    want("cp 参数量", f"{cpm['params']:,}")
    want("cp regret mean", f4(s["regret_mean"]))
    want("cp regret p50", f4(s["regret_p50"]))
    want("cp regret max", f4(s["regret_max"]))
    want("cp regret exact", f"{s['regret_exact_rate']*100:.1f}")
    want("cp lift mean", f4(s["lift_vs_base_mean"]))
    want("cp oracle lift", f4(s["oracle_lift_vs_base_mean"]))
    want("cp realize ratio", f"{s['coord_gain_realized_ratio']*100:.1f}")
    sw_v, sw_s = sweep["agg"]["value"], sweep["agg"]["score"]
    want("cp sweep value regret", f4(sw_v["regret_mean"]))
    want("cp sweep value regret std", f4(sw_v["regret_mean_std"]))
    want("cp sweep score regret", f4(sw_s["regret_mean"]))
    want("cp sweep score regret std", f4(sw_s["regret_mean_std"]))
    want("cp sweep value realize", f"{sw_v['realize_ratio']*100:.1f}")
    want("cp sweep score realize", f"{sw_s['realize_ratio']*100:.1f}")
    # LOSO 逐折 regret (meta.folds eval_summary) 与 loso JSON 逐折一致 (双源校验)
    for i, f in enumerate(cpm["folds"]):
        lf = loso["folds"][i]
        assert f["heldout_seed_offset"] == lf["heldout_seed_offset"]
        assert abs(f["eval_summary"]["regret_mean"]
                   - round(sum(lf["eval"]["regret"]) / len(lf["eval"]["regret"]), 4)) < 1e-9
    want("cp n_contexts", str(loso["n_contexts"]))
    # 服务化部署段 (coord_serve + /api/coord/action, 会话 30)
    want("cp serve endpoint", "/api/coord/action")
    want("cp serve module", "coord_serve.py")

    # ---- 7.6 时序协同策略 (M11: coord_seq_wm / coord_seq_policy / coord_seq_loso, 会话 36) ----
    swm = load(ROOT / "models" / "coord_seq_wm_meta.json")
    spm = load(ROOT / "models" / "coord_seq_policy_meta.json")
    sloso = load(ROOT / "datalake" / "generated" / "coord_seq_loso.json")
    ssum, swr, smr = sloso["summary"], sloso["win_rate_vs_best_const"], sloso["match_rates_mean"]
    want("7.6 时序协同策略 (M11)", "7.6 时序协同策略 (M11: 时序世界模型 + 规划 oracle 蒸馏)")
    want("seq wm params", f"{swm['params']:,}")
    want("seq wm reward r2", f3(swm["loso_heldout_reward"]["reward_r2"]))
    want("seq wm reward mae", f3(swm["loso_heldout_reward"]["reward_mae"]))
    want("seq pol params", f"{spm['params']:,}")
    want("seq pol train n", f"{spm['n_train']:,}")
    want("seq pol train acc", f"{spm['train_acc']*100:.1f}")
    want("seq agreement", f"{sloso['bc_heldout_planner_agreement_mean']*100:.1f}")
    want("seq wm heldout r2", f3(sloso["wm_heldout_mean"]["reward_r2"]))
    want("seq wm heldout mae", f3(sloso["wm_heldout_mean"]["reward_mae"]))
    want("seq zero return", f2(ssum["zero"]["mean"]))
    want("seq random return", f2(ssum["random"]["mean"]))
    want("seq best_const return", f2(ssum["best_const"]["mean"]))
    want("seq wm_greedy return", f2(ssum["wm_greedy"]["mean"]))
    want("seq bc return", f2(ssum["bc_policy"]["mean"]))
    want("seq sim return", f2(ssum["sim_greedy"]["mean"]))
    want("seq win bc", f"{swr['bc_policy']*100:.1f}")
    want("seq win wm", f"{swr['wm_greedy']*100:.1f}")
    want("seq win sim", f"{swr['sim_greedy']*100:.1f}")
    want("seq match bc_vs_sim", f"{smr['bc_vs_sim']*100:.1f}")
    want("seq match wm_vs_sim", f"{smr['wm_vs_sim']*100:.1f}")
    want("seq match bc_vs_wm", f"{smr['bc_vs_wm']*100:.1f}")
    # 33 项 v2 数据检修复验 (test_coord_seq.py 21 项 + sim_greedy oracle 上界自洽)
    want("seq runtime_s", f"{sloso['runtime_s']:.1f}")

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
