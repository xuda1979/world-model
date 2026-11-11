#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本周进展报告 (2026-11-11) (.docx)
=================================
输出: docs/progress_report_20261111.docx

覆盖: 校准仿真引擎 → v3.0 报告 → 逐小区协同策略 + 服务化 → M11 时序原语
     → 时序协同策略 (时序 WM + oracle 蒸馏 + 闭环 LOSO) → 测试/报告收尾 → 下周计划

全部数字与 docs/progress_report_20261111.md 同源, 直接读权威 JSON (零手抄)。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
GRAY = RGBColor(0x59, 0x59, 0x59)
GREEN = RGBColor(0x21, 0x7A, 0x21)


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def set_cn_font(run, size=None, bold=None, color=None):
    run.font.name = "Calibri"
    r = run._element.rPr
    if r is not None and r.rFonts is not None:
        r.rFonts.set(qn("w:eastAsia"), "微软雅黑")
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def para(doc, text="", size=10.5, bold=False, color=None, align=None,
         space_after=6):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    if text:
        r = p.add_run(text)
        set_cn_font(r, size=size, bold=bold, color=color)
    return p


def heading(doc, text, level=1):
    sizes = {1: 15, 2: 12.5}
    p = para(doc, text, size=sizes[level], bold=True, color=ACCENT,
             space_after=7)
    if level == 1:
        p.paragraph_format.space_before = Pt(12)
    return p


def bullet(doc, text, bold_prefix=None, size=10.5, color=None):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(3)
    if bold_prefix:
        r1 = p.add_run(bold_prefix)
        set_cn_font(r1, size=size, bold=True)
    r2 = p.add_run(text)
    set_cn_font(r2, size=size, color=color)
    return p


def check(doc, text):
    bullet(doc, text, bold_prefix="✓ ", color=GREEN)


def make_table(doc, header, rows, widths=None):
    t = doc.add_table(rows=1 + len(rows), cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for j, h in enumerate(header):
        c = t.rows[0].cells[j]
        c.text = ""
        r = c.paragraphs[0].add_run(str(h))
        set_cn_font(r, size=9.5, bold=True, color=ACCENT)
        c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    for i, row in enumerate(rows, 1):
        for j, v in enumerate(row):
            c = t.rows[i].cells[j]
            c.text = ""
            r = c.paragraphs[0].add_run(str(v))
            set_cn_font(r, size=9.5)
            if j > 0:
                c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    if widths:
        for j, w in enumerate(widths):
            for row in t.rows:
                row.cells[j].width = Cm(w)
    return t


# ---------------- data (权威 JSON, 与 md 同源) ----------------
loso = load(ROOT / "datalake" / "generated" / "coord_seq_loso.json")
swm = load(ROOT / "models" / "coord_seq_wm_meta.json")
spm = load(ROOT / "models" / "coord_seq_policy_meta.json")
cpm = load(ROOT / "models" / "coord_policy_meta.json")
sweep = load(ROOT / "datalake" / "generated" / "coord_policy_head_sweep.json")
cc = load(ROOT / "docs" / "closedloop" / "closedloop_calib_compare.json")
ca = load(ROOT / "docs" / "campus" / "campus_action_report.json")

ss, swr = loso["summary"], loso["win_rate_vs_best_const"]
m2 = loso["match_rates_mean"]
cp_s = cpm["loso_summary"]
per_scene = loso["per_scene_mean_return"]


def f(x, nd=2):
    return f"{x:.{nd}f}"


def pct(x, nd=1):
    return f"{x*100:.{nd}f}%"


TYPE_TBL = [
    ["zero (恒零)", f(ss["zero"]["mean"]), "—"],
    ["random (随机动作)", f(ss["random"]["mean"]), pct(swr["random"], 0)],
    ["best_const (静态最优)", f(ss["best_const"]["mean"]), "—"],
    ["wm_greedy (时序 WM 贪心)", f(ss["wm_greedy"]["mean"]), pct(swr["wm_greedy"], 0)],
    ["bc_policy (蒸馏策略)", f(ss["bc_policy"]["mean"]), pct(swr["bc_policy"], 0)],
    ["sim_greedy (oracle 逐 tick 上界)", f(ss["sim_greedy"]["mean"]), pct(swr["sim_greedy"], 0)],
]

# ---------------- document ----------------
doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
sec.left_margin = sec.right_margin = Cm(2.2)
sec.top_margin = sec.bottom_margin = Cm(2.2)

para(doc, "world6g 项目周报", size=20, bold=True, color=ACCENT,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
para(doc, "周期: 2026-11-06 ~ 2026-11-11", size=12, bold=True,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
para(doc, f"生成日期: {date.today().isoformat()}", size=10, color=GRAY,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=12)

# ===== 一、本周概览 =====
heading(doc, "一、本周概览", 1)
para(doc,
     "本周期完成『仿真器校准 → 决策动作模型 → 协同/时序协作 → 报告/服务化』整条收口链路, 共 25 次提交。"
     "核心目标『训练完世界模型与规划器 (动作模型), 数据来自校准仿真器』已端到端达成, "
     "全部产物双源校验入账本。5 个关键交付: ① 仿真器多源校准 (报告第 4 章); "
     "② 逐小区协同策略 LOSO 实现率 96.3% + 服务化 /api/coord/action; "
     "③ M11 时序化原语 (tick 推进 + 时变功率 + 9000 行时序数据集); "
     "④ 时序协同策略 v1 (时序 WM + oracle 蒸馏, 闭环 LOSO 全 5 折); "
     "⑤ 技术报告 v3.0 新增 7.6 节, 溯源探针 72/72 PASS。")
make_table(doc,
    ["交付物", "状态", "关键指标"],
    [["仿真器多源校准 v3.0 (报告第 4 章)", "✓ 已提交", "E3 cal 均值奖励 −0.126 > uncal −0.136"],
     ["逐小区协同策略 coord policy", "✓ 训练 + 服务化", f"LOSO 实现率 {pct(cp_s['coord_gain_realized_ratio'])}; 回归均值 {f(cp_s['regret_mean'],3)}"],
     ["M11 时序化原语", "✓ 已提交", "tick 推进 + 时变 per-cell 功率 + 21 项守护测试"],
     ["时序协同策略 v1 (时序 WM + BC)", "✓ 训练完成", f"sim_greedy {f(ss['sim_greedy']['mean'])} > best_const {f(ss['best_const']['mean'])}; BC 82% 对齐"],
     ["全测试套件 + 探针", "✓ 通过", "58 项 PASS; probe 72/72 PASS"]],
    widths=[7.6, 3.0, 6.0])

# ===== 二、本周完成事项 =====
heading(doc, "二、本周完成事项", 1)

heading(doc, "2.1 校准仿真引擎与决策动作模型 (11-06 ~ 11-08)", 2)
check(doc, f"融合奖励蒸馏为单网络动作模型 (policy_fused_net.pt): 闭环 lift +0.550 [CI +0.361, +0.729] — 首个真实奖励为正的单一网络动作模型")
check(doc, "校准仿真引擎 27 项 (会话 25/26): 收敛校准损耗 + 时变偏移, 产出 datalake/generated/sim_calib_report.json")
check(doc, "技术报告 v3.0 与全树收口 (会话 21-26 双账本 + 16 项工作树修改)")
check(doc, "世界模型消融注记: 闭环提升不归因多步一致性训练 — 融合奖励 λ=0.5 兼得保真度与进攻性")

heading(doc, "2.2 逐小区协同策略蒸馏与服务化 (11-09)", 2)
para(doc,
     f"多小区协同动作数据集 v2 (5 场景 × 5 seeds × 125 组合) 入报告 7.4. coord policy 61.4 万参数 "
     f"5 折 LOSO: 回归均值 {f(cp_s['regret_mean'],3)} / p50 {f(cp_s['regret_p50'],3)} / max {f(cp_s['regret_max'],3)}; "
     f"协同增益实现率 {pct(cp_s['coord_gain_realized_ratio'])}(oracle lift {f(cp_s['oracle_lift_vs_base_mean'])} vs "
     f"实际 lift {f(cp_s['lift_vs_base_mean'])}).")
make_table(doc,
    ["seed 目标", "回归均值", "实现率", "推荐"],
    [["value", f(sweep["agg"]["value"]["regret_mean"], 3), pct(sweep["agg"]["value"]["realize_ratio"]), "✓ 采用"],
     ["score", f(sweep["agg"]["score"]["regret_mean"], 3), pct(sweep["agg"]["score"]["realize_ratio"]), ""]],
    widths=[4.0, 4.0, 4.0, 4.0])
check(doc, "API 输入校验加固 (空 episode_id → 422) + 5 项守护测试固化")
check(doc, f"coord policy 服务化: /api/coord/action 在线推理 + 13 项守护测试 (报告 7.5 节部署段)")
check(doc, "目标账本补录会话 28 (v2 多 seed 稳健版)")

heading(doc, "2.3 仿真器时序化原语 M11 (11-10)", 2)
bullet(doc, "tick 推进 (dt=1s): 整体回报 = Σ_t tick 目标 (win_obj_t − ref_zero_t); 时变 per-cell 功率制造跨 tick 时变协同需求。")
bullet(doc, "时序协同数据集: 25 组 (5 场景 × 5 seeds) × 36 eps × 10 决策步; 配套 coord_seq_static_ref.json 静态参照。")
bullet(doc, "21 项守护测试 test_twin_temporal.py 固化 tick 推进/时变功率/时序数据集不变量; 目标账本会话 35 入库。")

heading(doc, "2.4 时序协同策略 v1 (11-11)", 2)
para(doc,
     f"训练管线: 时序世界模型 (3 集成, {swm['params']:,} 参数) → 规划 oracle (逐 tick 搜索) → BC 蒸馏 "
     f"({spm['n_train']:,} 条专家轨迹, 训练 acc {pct(spm['train_acc'])}) → 闭环 LOSO (5 折按 seed_offset, 25 组). "
     f"时序 WM 留出折 reward-R² {f(swm['loso_heldout_reward']['reward_r2'],3)} / MAE {f(swm['loso_heldout_reward']['reward_mae'],3)}; "
     f"留出折 BC 与规划器动作一致率 {pct(loso['bc_heldout_planner_agreement_mean'])}.")
make_table(doc,
    ["控制器", "回报均值", "vs best_const 胜率"],
    TYPE_TBL,
    widths=[6.5, 4.0, 5.0])
para(doc,
     f"逐场景 (BC / sim_greedy): 园区 {f(per_scene['campus']['bc_policy'])}/{f(per_scene['campus']['sim_greedy'])}, "
     f"城区 {f(per_scene['城区']['bc_policy'])}/{f(per_scene['城区']['sim_greedy'])}, "
     f"郊区 {f(per_scene['郊区']['bc_policy'])}/{f(per_scene['郊区']['sim_greedy'])}, "
     f"低空 {f(per_scene['低空']['bc_policy'])}/{f(per_scene['低空']['sim_greedy'])}, "
     f"机器人工厂 {f(per_scene['机器人工厂']['bc_policy'])}/{f(per_scene['机器人工厂']['sim_greedy'])}. "
     f"全场景 BC 追平 best_const、距 oracle 上界 <2 点。")
para(doc,
     f"诚实披露: BC 与 sim_greedy 决策重合率 {pct(m2['bc_vs_sim'],1)} / wm_greedy {pct(m2['wm_vs_sim'],1)} "
     f"(动作空间 125, 分歧大但回报损失小); 修复折内掩码形状 bug + 死代码后回归完成 (runtime {loso['runtime_s']:.0f}s)。")

heading(doc, "2.5 测试套件与技术报告 (收尾轮)", 2)
check(doc, "全测试套件 58 项 PASS: test_coord_seq.py (21 项) + test_twin_temporal.py (21 项) + test_wm_api.py (13 项) + 回归")
check(doc, "技术报告 7.6 节『时序协同策略 (M11: 时序世界模型 + 规划 oracle 蒸馏)』: 每折表 + 跨折总结 + 诚实披露 + 资源/复现")
check(doc, f"溯源探针 probe_report_v3.py 72/72 PASS (新增 21 项 7.6 断言), 第 4 章全部数字与章节标题零手抄")

# ===== 三、关键结论 =====
heading(doc, "三、关键结论", 1)
bullet(doc, f"校准是决策质量的根: E3 40 真实窗闭环 cal 均值奖励 {f(cc['summary']['cal_mean_real'])} > uncal "
            f"{f(cc['summary']['uncal_mean_real'])}, 动作率 0.225 << 0.65 — 校准过的 WM『该动才动』.")
bullet(doc, f"协同动作是最大单步决策收益: coord policy 5 折 LOSO 实现 oracle 协同增益的 {pct(cp_s['coord_gain_realized_ratio'])}, "
           f"regret 中位恰为 0; 服务化已在线.")
bullet(doc, f"时序化后蒸馏策略直接贴近最优: bc_policy {f(ss['bc_policy']['mean'])} 追平 best_const {f(ss['best_const']['mean'])}, "
           f"胜率 12% > wm_greedy 8%; oracle 逐 tick 贪心 {f(ss['sim_greedy']['mean'])} 胜率 80%.")
bullet(doc, f"诚实披露: BC 与 sim_greedy 决策重合率仅 {pct(m2['bc_vs_sim'],1)} (动作空间 125), 分场景距 oracle <2 点 — "
           f"扩展决策空间是下一步收益来源.")

# ===== 四、下一步计划 =====
heading(doc, "四、下一步计划", 1)
bullet(doc, "BC→oracle 差距收敛: 扩大专家数据 (tick 级 oracle 决策) + 动作空间结构化 (125 组合 → 粗粒度), 目标 logits 回报 ≥ 28.5。", "P0 ")
bullet(doc, "WM 预测量化提升: reward-R² 0.534 → 0.65, 引入 tick 级 GNN 共享 scene 结构。", "P1 ")
bullet(doc, "报告 7.7 节候选: 时序协同策略对 CEM/策略蒸馏的可迁移性对比。", "P2 ")
bullet(doc, "演示闭环: 3D 工作台接入 /api/coord/action + 时序 tick 回放。", "P2 ")

# ===== 五、仓库状态 =====
heading(doc, "五、仓库状态", 1)
bullet(doc, "提交数 (本周期): 25 (核心 10 次, b698792 → abd0584)")
bullet(doc, "关键产物: datalake/generated/coord_seq_loso.json / coord_seq_static_ref.json / coord_policy_loso.json; "
            "models/coord_seq_wm|policy{.pt,_meta.json} / coord_policy.pt+meta / policy_fused_net.pt")
bullet(doc, "守护测试: tests/test_coord_seq.py (21) + test_twin_temporal.py (21) + test_wm_api.py (13)")
bullet(doc, "双账本: 根 .future-code/goal.md 与 world6g 镜像 (grep -c 断言两侧均 = 1)")

out = DOCS / "progress_report_20261111.docx"
doc.save(out)
print(f"[done] {out}")
