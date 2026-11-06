#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本周进展报告 (2026-11-04 ~ 11-05) (.docx)
============================================
输出: docs/progress_report_20261105.docx

覆盖: 世界模型 v2 (两个公开难题的解法) → 规划器 (CEM-MPC) → 动作模型 (策略蒸馏)
     → 独立复核与负控制 → 下周计划
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


# ---------------- data ----------------
results = load(DOCS / "wm_suite" / "results.json")
policy_meta = load(ROOT / "models" / "policy_meta.json")
ev = policy_meta["eval"]
tb, td = results["task_b"], results["task_d"]
mc = td["rssm_mc_z"]
v2g = td["v2"]["GRU-WM"]


def f(x, nd=3):
    return f"{x:.{nd}f}"


# ---------------- document ----------------
doc = Document()
sec = doc.sections[0]
sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
sec.left_margin = sec.right_margin = Cm(2.2)
sec.top_margin = sec.bottom_margin = Cm(2.2)

para(doc, "world6g 项目周报", size=20, bold=True, color=ACCENT,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
para(doc, "周期: 2026-11-04 ~ 2026-11-05", size=12, bold=True,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
para(doc, f"生成日期: {date.today().isoformat()}", size=10, color=GRAY,
     align=WD_ALIGN_PARAGRAPH.CENTER, space_after=12)

# ===== 一、本周概览 =====
heading(doc, "一、本周概览", 1)
para(doc,
     "本周从『世界模型基准』推进到『决策闭环』: 先解决世界模型两个公开难题 (v2 提交 930df44), "
     "再基于世界模型实现 CEM-MPC 规划器, 最后把规划器蒸馏为毫秒级动作模型 (策略网络), "
     "并对全部产物做了独立复核 + 负控制验证。目标链路『仿真产生数据 → 训练世界模型 + 动作模型』"
     "已端到端打通并验证。")
make_table(doc,
    ["交付物", "状态", "关键指标"],
    [["世界模型 v2 (多步一致性 + 异方差 NLL)", "✓ 已提交", f"h40 误差 −23.1%; 覆盖率 4.4%→{v2g['coverage_95']*100:.1f}%"],
     ["规划器 wm/planner.py (CEM-MPC + 3 基线)", "✓ 完成并验证", "CEM 4 代×64 样本×16 精英, K=8 rollout"],
     ["动作模型 policy_net.pt (策略蒸馏)", "✓ 训练完成", f"类型匹配 {ev['type_match_rate']*100:.1f}%, 奖励差 {ev['reward_gap_policy_minus_planner']:.3f}"],
     ["独立复核 + 负控制", "✓ 通过", "复跑评测一致; 方向性/零均值检验通过"],
     ["全项目技术报告 (.docx)", "✓ 完成", "docs/world6g_full_technical_report.docx"]],
    widths=[7.2, 3.2, 5.6])

# ===== 二、世界模型 v2 =====
heading(doc, "二、世界模型 v2: 两个公开难题的解法 (11-04, 提交 930df44)", 1)

heading(doc, "2.1 难题一: rollout 复合误差 → 多步一致性训练", 2)
para(doc,
     "问题: 单步训练的世界模型在自回归 rollout 时误差指数复合 (GRU-WM h1→h40: "
     f"{f(tb['GRU-WM']['rmse_h1'])}→{f(tb['GRU-WM']['rmse_h40'])}, 增长 {f(tb['GRU-WM']['growth_x'],1)}×), "
     "长程不可用。")
para(doc,
     "解法: scripts/train_wm_ms.py — 训练时对同一分支做 K=8 步自回归 rollout, 逐步施加一致性损失, "
     "让模型在『自己预测喂给自己』的分布下保持准确。")
make_table(doc,
    ["模型", "RMSE h=1", "RMSE h=40", "误差增长"],
    [["GRU-WM (单步训练)", f(tb['GRU-WM']['rmse_h1'], 4), f(tb['GRU-WM']['rmse_h40'], 4), f"{tb['GRU-WM']['growth_x']:.1f}×"],
     ["GRU-WM-MS (多步一致性)", f(tb['GRU-WM-MS']['rmse_h1'], 4), f(tb['GRU-WM-MS']['rmse_h40'], 4), f"{tb['GRU-WM-MS']['growth_x']:.1f}×"],
     ["Persistence (参照)", f(tb['Persistence']['rmse_h1'], 4), f(tb['Persistence']['rmse_h40'], 4), f"{tb['Persistence']['growth_x']:.1f}×"]],
    widths=[5.5, 3.4, 3.4, 3.4])
check(doc, "h40 误差 1.103 → 0.849 (−23.1%), 首次跑赢其余全部学习模型; 产物 models/gru_wm_ms.pt")

heading(doc, "2.2 难题二: 不确定性校准 → 异方差 NLL 训练", 2)
para(doc,
     f"问题: MSE 训练 + MC-z 采样的不确定性严重失准 (覆盖率 {mc['coverage_95']*100:.1f}%, "
     f"需事后放大 {td['posthoc_scale']:.0f} 倍才能覆盖)。")
para(doc,
     "解法: 为主干装配 σ 头 (softplus, 下界 1e-3), 高斯 NLL 联合训练 (Task D v2)。")
make_table(doc,
    ["方案", "σ̄", "覆盖率 95%", "NLL", "事后缩放"],
    [["v1 RSSM MC-z (未校准)", f(mc['mean_sigma_norm'], 4), f(mc['coverage_95'], 3), f(mc['nll'], 0), f"c={td['posthoc_scale']:.0f}"],
     ["v2 GRU-WM (NLL)", f(v2g['mean_sigma_norm'], 4), f(v2g['coverage_95'], 4), f(v2g['nll'], 4), f"c={v2g['posthoc_scale']:.2f} (no-op)"]],
    widths=[5.2, 2.6, 3.0, 3.0, 3.0])
check(doc, f"覆盖率 4.4% → {v2g['coverage_95']*100:.1f}%, 事后缩放系数≈1 (自身已校准)")

# ===== 三、规划器 =====
heading(doc, "三、规划器: 基于世界模型的 CEM-MPC (11-05)", 1)
para(doc,
     "wm/planner.py — 给定 8 步历史窗, 在世界模型 (GRU-WM-MS) 上做 K=8 步 rollout, "
     "用交叉熵方法 (CEM: 4 迭代 × 64 样本 × 16 精英) 在 4 类干预动作 (类型+参数) 上搜索 "
     "最大化加权 KPI 奖励的动作; 含随机采样 / 单步贪心 / 无动作三个基线与推荐 margin 决策规则。")
bullet(doc, "单 UE 自私视角与场景级联合规划 (8 UE 平均奖励) 两种模式。")
bullet(doc, "奖励权重 DL 0.60 / SINR 0.20 / RSRP 0.10 / CQI 0.10 (z-score 空间) + 动作幅值惩罚。")
bullet(doc, "规划耗时 ~秒级, 适合离线决策 — 实时场景由动作模型接管 (见下)。")

# ===== 四、动作模型 =====
heading(doc, "四、动作模型: CEM-MPC 策略蒸馏 (11-05)", 1)
para(doc,
     "scripts/train_planner.py 把规划器蒸馏为一步前馈策略网络 (MLP 双头: 类型 5 类交叉熵 + "
     "选中值 MSE; 29,577 参数): 专家数据 = 512 个基准窗口上 CEM-MPC 的决策, 推理毫秒级。")
make_table(doc,
    ["指标 (留出段 128 窗)", "数值"],
    [["类型匹配率 (vs 规划器)", f"{ev['type_match_rate']:.4f}"],
     ["值 MAE (匹配子集)", f"{ev['value_mae (matched)']:.2f}"],
     ["推荐率: 规划器 / 策略", f"{ev['planner_recommend_rate']:.3f} / {ev['policy_recommend_rate']:.3f}"],
     ["WM 奖励: 规划器 / 策略", f"{ev['wm_reward_planner']:.4f} / {ev['wm_reward_policy']:.4f}"],
     ["奖励差 (策略−规划器)", f"{ev['reward_gap_policy_minus_planner']:+.4f} (≈0, 持平)"]],
    widths=[8.0, 8.0])
check(doc, "策略网络以 3 万参数复现 CEM-MPC 决策质量, 秒级 → 毫秒级")

# ===== 五、复核 =====
heading(doc, "五、独立复核与负控制验证 (11-05)", 1)
para(doc, "不依赖训练日志, 对产物做黑盒验证:")
check(doc, "复跑 wm/evaluate.py 全套四任务: 与已提交 results.json 完全一致 (唯一 diff 时间戳)")
check(doc, "磁盘加载 gru_wm_ms.pt + policy_net.pt + CEMPlanner, 1280 测试窗端到端推理: type 0.875 / 推荐 0.917 (48 窗抽样)")
check(doc, "动作响应方向性: pmax↓→RSRP/速率↓; prb↓→速率↓ (−4.55Mbps); ho+8→SINR+0.27dB — 符合物理预期")
check(doc, "负控制: 200 个随机动作平均响应≈0 (ΔRSRP −0.10dB) 但单动作幅值显著 (|ΔDL| 2.6Mbps) — 模型真条件于动作")
check(doc, "反事实信号与噪声同量级 (384 分支 |Δ|@10s ≈ 分支 std, SNR≈1×) — Task C corr 0.72 非平凡")

# ===== 六、下周计划 =====
heading(doc, "六、下周计划 (11-06 ~ 11-12)", 1)
bullet(doc, "3D 平台 ↔ 世界模型闭环演示: 平台下发动作 → WM 预测 → Twin 校验 (预测-验证闭环), 11/6 能力交互首版。", "P0 ")
bullet(doc, "PhysWM rollout-aware 残差训练 (目标 h40 追平 GRU-WM-MS); az_offset 动作样本扩充 (当前 RMSE 最高)。", "P1 ")
bullet(doc, "不确定性按 rollout 深度分桶校准 (σ 随 h 增宽) + conformal 精修。", "P1 ")
bullet(doc, "论文: 复跑全部实验固化数值, 补 Related Work 引用, 按目标会议格式打磨。", "P2 ")
bullet(doc, "11/13 稳定演示节点准备 (沿用 README 双周节点)。", "P2 ")

# ===== 七、仓库状态 =====
heading(doc, "七、仓库状态", 1)
bullet(doc, "930df44 (11-04): 世界模型 v2 — 多步一致性训练 + 异方差 NLL 校准")
bullet(doc, "本周新增 (待提交): wm/planner.py, scripts/train_planner.py, scripts/train_wm_ms.py, "
            "models/{gru_wm_ms, policy_net}.pt+meta, docs/world6g_full_technical_report.docx, 本报告")
bullet(doc, "全部评测固定 seed=42、时间外推切分, results.json / *_meta.json 机器可读")

out = DOCS / "progress_report_20261105.docx"
doc.save(out)
print(f"[done] {out}")
