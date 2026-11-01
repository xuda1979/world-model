#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成项目进度汇报 PPT (world6g/docs/world6g_progress.pptx)
=========================================================
内容: 两方案执行进度 / 五层数据架构 / 数字孪生引擎 / 动作条件世界模型 /
      3D 仿真平台 (带截图) / 统计保真度 / 论文进展 / 下一步计划
截图来源: world6g/docs/img/*.png (Playwright 真实平台截图)
"""
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
OUT = ROOT / "docs" / "world6g_progress.pptx"

# ---------- 配色 (深色科技风) ----------
BG      = RGBColor(0x0B, 0x12, 0x1F)   # 深夜蓝黑
BG2     = RGBColor(0x11, 0x1B, 0x2E)   # 面板
ACCENT  = RGBColor(0x38, 0xBD, 0xF8)   # 亮青蓝
ACCENT2 = RGBColor(0x34, 0xD3, 0x99)   # 绿
ACCENT3 = RGBColor(0xF5, 0x9E, 0x0B)   # 橙
TXT     = RGBColor(0xE6, 0xED, 0xF7)
TXT_DIM = RGBColor(0x94, 0xA3, 0xB8)
LINE    = RGBColor(0x25, 0x33, 0x4D)

FONT = "PingFang SC"

SW, SH = Inches(13.333), Inches(7.5)


def _set_font(run, size=14, bold=False, color=TXT, font=FONT):
    f = run.font
    f.name = font
    f.size = Pt(size)
    f.bold = bold
    f.color.rgb = color
    # 东亚字体
    rPr = run._r.get_or_add_rPr()
    from pptx.oxml.ns import qn
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", font)


def add_slide(prs, title, kicker=None):
    """新建一页: 深底 + 标题栏"""
    s = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    # 背景
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = BG
    # 标题
    tb = s.shapes.add_textbox(Inches(0.55), Inches(0.28), Inches(11.5), Inches(0.75))
    p = tb.text_frame.paragraphs[0]
    r = p.add_run(); r.text = title
    _set_font(r, 27, True, TXT)
    # kicker (右上角小标)
    if kicker:
        kb = s.shapes.add_textbox(Inches(9.3), Inches(0.42), Inches(3.5), Inches(0.4))
        p = kb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.RIGHT
        r = p.add_run(); r.text = kicker
        _set_font(r, 12, False, TXT_DIM)
    # 分隔线
    ln = s.shapes.add_shape(1, Inches(0.55), Inches(1.02), Inches(12.23), Pt(1.6))
    ln.fill.solid(); ln.fill.fore_color.rgb = LINE; ln.line.fill.background()
    ln.shadow.inherit = False
    # 页脚
    fb = s.shapes.add_textbox(Inches(0.55), Inches(7.06), Inches(12.2), Inches(0.32))
    p = fb.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = "6G 数据底座 · 校准数字孪生 · 动作条件世界模型  |  2026-11 进度汇报"
    _set_font(r, 9, False, RGBColor(0x5A, 0x6B, 0x85))
    return s


def add_card(s, x, y, w, h, fill=BG2, line_color=LINE):
    """圆角面板"""
    from pptx.enum.shapes import MSO_SHAPE
    c = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    c.adjustments[0] = 0.055
    c.fill.solid(); c.fill.fore_color.rgb = fill
    c.line.color.rgb = line_color; c.line.width = Pt(1)
    c.shadow.inherit = False
    return c


def card_text(card, lines, anchor=MSO_ANCHOR.TOP):
    """往面板里写多行: lines = [(text, size, bold, color, space_before), ...]"""
    tf = card.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = Inches(0.18); tf.margin_right = Inches(0.14)
    tf.margin_top = Inches(0.13); tf.margin_bottom = Inches(0.1)
    first = True
    for item in lines:
        text, size, bold, color = item[0], item[1], item[2], item[3]
        space = item[4] if len(item) > 4 else 4
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.space_before = Pt(space)
        r = p.add_run(); r.text = text
        _set_font(r, size, bold, color)
    return card


def add_img(s, path, x, y, w=None, h=None):
    pic = s.shapes.add_picture(str(path), x, y, width=w, height=h)
    pic.line.color.rgb = LINE; pic.line.width = Pt(1)
    # 细描边
    pic.shadow.inherit = False
    return pic


def caption(s, x, y, w, text):
    tb = s.shapes.add_textbox(x, y, w, Inches(0.3))
    p = tb.text_frame.paragraphs[0]
    r = p.add_run(); r.text = text
    _set_font(r, 10.5, False, TXT_DIM)


def stat_chip(s, x, y, w, big, small, color=ACCENT):
    """数字统计块"""
    c = add_card(s, x, y, w, Inches(1.12))
    card_text(c, [(big, 21, True, color, 0), (small, 10.5, False, TXT_DIM, 2)])
    return c


# ============================================================
prs = Presentation()
prs.slide_width, prs.slide_height = SW, SH

# ---------- P1 封面 ----------
s = prs.slides.add_slide(prs.slide_layouts[6])
s.background.fill.solid(); s.background.fill.fore_color.rgb = BG
# 顶部装饰条
bar = s.shapes.add_shape(1, Inches(0), Inches(0), SW, Inches(0.10))
bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()
bar.shadow.inherit = False

tb = s.shapes.add_textbox(Inches(0.9), Inches(1.7), Inches(11.5), Inches(1.2))
p = tb.text_frame.paragraphs[0]
r = p.add_run(); r.text = "6G 数据底座 · 校准数字孪生 · 动作条件世界模型"
_set_font(r, 40, True, TXT)
tb2 = s.shapes.add_textbox(Inches(0.9), Inches(2.75), Inches(11.5), Inches(0.7))
p = tb2.text_frame.paragraphs[0]
r = p.add_run(); r.text = "项目进度汇报  —  从 338 万真实 MRO 样本到 3D 仿真平台"
_set_font(r, 20, False, ACCENT)
# 底部信息条
info = add_card(s, Inches(0.9), Inches(4.3), Inches(11.5), Inches(0.9))
card_text(info, [
    ("中国移动信息港 (昌平) 试点  ·  方案一 (数据底座+可视化) + 方案二 (世界模型) 双线并行", 14, False, TXT, 0),
    ("2026 年 11 月  ·  里程碑: 11/6 V0.1 首版 ✓   11/13 V0.2 稳定演示 ✓ (均提前达成)", 13, False, TXT_DIM, 6),
])

# ---------- P2 目录 ----------
s = add_slide(prs, "汇报目录", "AGENDA")
items = [
    ("01", "项目背景与两方案执行计划", "Q4 双线并行 · 五项能力 · 里程碑"),
    ("02", "总体架构: 数据湖 + 数字孪生 + 世界模型", "12 表数据底座 · 3GPP 结构孪生 · GRU-WM"),
    ("03", "数据底座建设成果", "338 万 MRO 样本 · 12 张表 · 校准反事实数据"),
    ("04", "校准数字孪生引擎 (Twin)", "同根动作族 · 统计保真度 · 业务流量模型"),
    ("05", "动作条件世界模型 (GRU-WM)", "83,200 训练样本 · RMSE ↓10.1% · rollout 服务"),
    ("06", "3D 仿真平台演示", "五层数据渲染 · hover/点击详情 · 智能问答 (截图)"),
    ("07", "统计保真度验证 (论文级)", "KS 检验 · 分位数覆盖 · 校准前后对照"),
    ("08", "论文进展与下一步计划", "IEEEtran 骨架 · V0.5/V1 里程碑"),
]
y = Inches(1.35)
for num, t1, t2 in items:
    c = add_card(s, Inches(0.55), y, Inches(12.23), Inches(0.62))
    tf = c.text_frame; tf.word_wrap = True; tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = Inches(0.22)
    p = tf.paragraphs[0]
    r = p.add_run(); r.text = num + "   "
    _set_font(r, 16, True, ACCENT)
    r = p.add_run(); r.text = t1
    _set_font(r, 15, True, TXT)
    r = p.add_run(); r.text = "      " + t2
    _set_font(r, 11.5, False, TXT_DIM)
    y += Emu(int(Inches(0.70)))

# ---------- P3 背景与两方案 ----------
s = add_slide(prs, "01 · 项目背景与执行计划", "两方案并行")
c1 = add_card(s, Inches(0.55), Inches(1.25), Inches(6.0), Inches(2.5))
card_text(c1, [
    ("方案一 · 数据底座 + 可视化", 16, True, ACCENT, 0),
    ("• 数据湖: 真实 MRO/MDT/PM/NRM 数据入湖, 12 张表", 12.5, False, TXT, 8),
    ("• 可视化工作台: KPI 时序 / 网络地图 / 校准对照", 12.5, False, TXT, 5),
    ("• 五项能力结果卡 (常驻数据来源标识)", 12.5, False, TXT, 5),
    ("• 试点: 中国移动信息港 (昌平) OSM 园区地图", 12.5, False, TXT, 5),
])
c2 = add_card(s, Inches(6.78), Inches(1.25), Inches(6.0), Inches(2.5))
card_text(c2, [
    ("方案二 · 世界模型训练 + 能力演示", 16, True, ACCENT2, 0),
    ("• 动作条件 GRU 世界模型 (时间外推 rollout)", 12.5, False, TXT, 8),
    ("• 训练数据: 孪生同根反事实动作族 (现网拿不到)", 12.5, False, TXT, 5),
    ("• 推理服务化: twm_serve + /api/wm/rollout", 12.5, False, TXT, 5),
    ("• 端到端: 调参动作 → 信号/KPI 预测对照", 12.5, False, TXT, 5),
])
c3 = add_card(s, Inches(0.55), Inches(3.95), Inches(12.23), Inches(1.55))
card_text(c3, [
    ("五项能力 (两方案共同交付物)", 15, True, TXT, 0),
    ("① Radio map 重构    ② 新站覆盖预测    ③ 参数调整后信号预测    ④ KPI 预测    ⑤ 用户行为预测", 13.5, False, ACCENT, 8),
    ("核心理念: 「结构来自 3GPP, 参数来自数据」 — 每张结果卡常驻数据来源标识 (方案一 §四)", 12, False, TXT_DIM, 6),
])
# 里程碑
mx = Inches(0.55)
for date, label, done, color in [
    ("11/06", "V0.1 首版", True, ACCENT2),
    ("11/13", "V0.2 稳定演示", True, ACCENT2),
    ("11/27", "V0.5 数据", False, TXT_DIM),
    ("12/31", "V1 季度成果", False, TXT_DIM),
]:
    c = stat_chip(s, mx, Inches(5.68), Inches(2.92), f"{date}  {label}", "已达成 ✓" if done else "计划中", color)
    mx += Emu(int(Inches(3.11)))

# ---------- P4 总体架构 ----------
s = add_slide(prs, "02 · 总体架构", "数据湖 → 孪生 → 世界模型 → 平台")
# 流水线 4 层
layers = [
    ("① 数据湖 (datalake)", "12 张表: action/cellperf/csi/episodes/link/map/mro/nrm/phy/site/sys/ue\n338 万真实 MRO 样本入湖 · duckdb 查询", ACCENT),
    ("② 校准数字孪生 (twin/)", "3GPP 结构 (TR 38.901 UMa/UMi/RMa/InH) + 真实数据校准参数\n同根动作族: 反事实分支 = 精确因果对照", ACCENT2),
    ("③ 世界模型 (models/, twm_serve)", "动作条件 GRU-WM v0.2 · 18.8k 参数 · 83,200 训练样本\n时间外推 rollout: 未来 KPI/信号轨迹预测", ACCENT3),
    ("④ 可视化平台 (frontend/, backend/)", "index.html 数据底座工作台 + workbench.html 3D 仿真工作台\n19 路 API · 五层渲染 · 智能问答", RGBColor(0xA7, 0x8B, 0xFA)),
]
y = Inches(1.30)
for i, (t, d, col) in enumerate(layers):
    c = add_card(s, Inches(0.55), y, Inches(7.3), Inches(1.24))
    card_text(c, [
        (t, 14.5, True, col, 0),
        (d, 11.5, False, TXT, 5),
    ])
    if i < 3:
        ar = s.shapes.add_shape(13, Inches(4.0), y + Inches(1.26), Inches(0.36), Inches(0.14))
        ar.fill.solid(); ar.fill.fore_color.rgb = TXT_DIM; ar.line.fill.background()
        ar.rotation = 90
        ar.shadow.inherit = False
    y += Emu(int(Inches(1.44)))
# 右侧: 关键数字
rx = Inches(8.15)
c = add_card(s, rx, Inches(1.30), Inches(4.63), Inches(5.52))
card_text(c, [
    ("关键规模数字", 16, True, TXT, 0),
    ("2,733 行  核心代码 (twin + world-model + backend + frontend)", 13, True, ACCENT, 12),
    ("18 个  episodes (真实 12 + 反事实孪生 6)", 13, True, ACCENT, 10),
    ("83,200  世界模型训练样本 (同根动作族)", 13, True, ACCENT2, 10),
    ("18.8k  GRU-WM 参数量 (轻量可嵌入)", 13, True, ACCENT2, 10),
    ("496  3D 场景 traces (65 建筑 + RSRP 栅格 + UE)", 13, True, ACCENT3, 10),
    ("19 路  FastAPI 接口 (/api/scene3d, /api/chat, /api/wm/rollout…)", 13, True, ACCENT3, 10),
    ("8 次  git commit (10/30 – 11/1, 双线持续集成)", 13, True, RGBColor(0xA7, 0x8B, 0xFA), 10),
])

# ---------- P5 数据底座 ----------
s = add_slide(prs, "03 · 数据底座建设成果", "datalake · 12 表")
stats = [
    ("338 万", "真实 MRO 样本", ACCENT),
    ("12 张", "数据湖表 (duckdb)", ACCENT),
    ("65 栋", "信息港 OSM 建筑", ACCENT2),
    ("109 条", "园区道路网", ACCENT2),
]
x = Inches(0.55)
for big, small, col in stats:
    stat_chip(s, x, Inches(1.30), Inches(2.92), big, small, col)
    x += Emu(int(Inches(3.11)))
c = add_card(s, Inches(0.55), Inches(2.65), Inches(6.0), Inches(4.2))
card_text(c, [
    ("表清单与内容", 15, True, TXT, 0),
    ("mro   3.38M 行 · 真实测量报告 (RSRP/SINR/CQI, 来源: 现网)", 12.5, False, TXT, 10),
    ("ue    终端轨迹与业务 (每 episode 全量 UE 状态, 50ms 采样)", 12.5, False, TXT, 7),
    ("link  链路级测量 (RSRP/SINR/时延/丢包)", 12.5, False, TXT, 7),
    ("cellperf  基站 KPI (吞吐/连接数/PRB 利用率)", 12.5, False, TXT, 7),
    ("site / phy / nrm   站点工参 · 物理层 · NRM 参数配置", 12.5, False, TXT, 7),
    ("action  动作族记录 (同根反事实分支, 世界模型训练核心)", 12.5, False, ACCENT2, 7),
    ("csi / map / episodes / sys   信道状态 · 地图 · 台账 · 系统", 12.5, False, TXT, 7),
])
c = add_card(s, Inches(6.78), Inches(2.65), Inches(6.0), Inches(4.2))
card_text(c, [
    ("信息港 (昌平) 试点地图", 15, True, TXT, 0),
    ("• 选点: 中国移动国际信息港 (沙河镇, G6 以西)", 12.5, False, TXT, 10),
    ("• OSM Overpass 拉取 65 栋建筑轮廓 + 109 条道路", 12.5, False, TXT, 7),
    ("• ENU 投影以 (40.1515, 116.2335) 为原点", 12.5, False, TXT, 7),
    ("• 3 个锚点: 信息港一期 / 二期 / 6期", 12.5, False, TXT, 7),
    ("• 建筑高度: OSM 实测优先, 缺失按类型假设并标注来源", 12.5, False, TXT, 7),
    ("", 8, False, TXT, 4),
    ("数据治理原则 (方案一 §四)", 13.5, True, ACCENT, 8),
    ("每张结果卡 / 每条数据带 source 标注 —「结构来自 3GPP,", 12, False, TXT, 6),
    ("参数来自数据」, 仿真值与真实值域分列不混用。", 12, False, TXT, 5),
])

# ---------- P6 孪生引擎 ----------
s = add_slide(prs, "04 · 校准数字孪生引擎 (Twin)", "同根反事实 · 统计保真")
c = add_card(s, Inches(0.55), Inches(1.25), Inches(6.0), Inches(2.7))
card_text(c, [
    ("同根动作族 (论文核心贡献 ②)", 15.5, True, ACCENT2, 0),
    ("同一初始状态 (同根) 出发, 施加不同动作 (下倾角/功率/", 12.5, False, TXT, 10),
    ("频点/新站) 生成反事实分支:", 12.5, False, TXT, 5),
    ("• 真实网络永远只能观测\"已发生\"的那一条轨迹", 12.5, False, TXT, 8),
    ("• 孪生给出精确因果对照 — offline RL 的关键数据", 12.5, False, ACCENT2, 5),
    ("• 6 条 twin episode 已入库 (twin_01…06)", 12.5, False, TXT, 5),
])
c = add_card(s, Inches(6.78), Inches(1.25), Inches(6.0), Inches(2.7))
card_text(c, [
    ("3GPP 结构 + 数据校准", 15.5, True, ACCENT, 0),
    ("• 传播模型: TR 38.901 UMa/UMi/RMa/InH, 结构固定", 12.5, False, TXT, 10),
    ("• 路损/阴影参数: 用 338 万真实 MRO 样本校准拟合", 12.5, False, TXT, 5),
    ("• 业务流量模型: 与真实统计同构 (到达/驻留/转移)", 12.5, False, TXT, 5),
    ("• 校准面板: 生成值 vs 真实值域实时对照 (index 页)", 12.5, False, TXT, 5),
])
c = add_card(s, Inches(0.55), Inches(4.15), Inches(12.23), Inches(2.7))
card_text(c, [
    ("孪生引擎能力清单 (twin/)", 15.5, True, TXT, 0),
    ("engine.py    场景/传播/KPI 仿真内核 · episode 生成 · 动作施加", 12.5, False, TXT, 10),
    ("campus.py    信息港 OSM 园区地图 (建筑/道路/锚点, ENU 投影, 来源标注)", 12.5, False, TXT, 7),
    ("反事实分支    同根动作族 — 唯一能提供\"如果当时…会怎样\"的精确答案", 12.5, False, ACCENT2, 7),
    ("业务模型     用户行为预测 (五项能力 ⑤): 轨迹关联 · 驻留/转移 · 群体分布", 12.5, False, TXT, 7),
    ("保真度验证   论文级 KS 检验 + 分位数覆盖 + 诚实报告 (详见 07 节)", 12.5, False, ACCENT3, 7),
])

# ---------- P7 世界模型 ----------
s = add_slide(prs, "05 · 动作条件世界模型 (GRU-WM)", "方案二核心交付")
stats = [
    ("83,200", "训练样本 (同根动作族)", ACCENT2),
    ("18.8k", "模型参数 (GRU+head)", ACCENT2),
    ("↓10.1%", "RMSE vs 基线", ACCENT3),
    ("200 帧", "外推 rollout (10s)", ACCENT3),
]
x = Inches(0.55)
for big, small, col in stats:
    stat_chip(s, x, Inches(1.28), Inches(2.92), big, small, col)
    x += Emu(int(Inches(3.11)))
c = add_card(s, Inches(0.55), Inches(2.62), Inches(6.0), Inches(4.25))
card_text(c, [
    ("模型设计", 15.5, True, TXT, 0),
    ("• 任务: 给定 (状态 s, 动作 a, 历史 h) 预测下一帧 KPI 向量", 12.5, False, TXT, 10),
    ("• 状态: RSRP/SINR/吞吐/时延/CQI 等 7 维; 动作: 下倾/功率等", 12.5, False, TXT, 6),
    ("• 架构: GRU 编码历史 + 动作嵌入 → MLP head 输出下一状态", 12.5, False, TXT, 6),
    ("• 自回归 rollout: 单步模型迭代外推 200 帧 (10 秒 @50ms)", 12.5, False, TXT, 6),
    ("", 8, False, TXT, 4),
    ("训练要点", 14, True, ACCENT, 8),
    ("• 训练集: 18 episodes 中同根反事实族, 83,200 个转移样本", 12.5, False, TXT, 6),
    ("• 对照基线: 恒等外推 (persistence) 与线性外推", 12.5, False, TXT, 6),
    ("• 结果: 多步 RMSE 较基线降 10.1%, 轨迹形态保持", 12.5, False, ACCENT2, 6),
])
c = add_card(s, Inches(6.78), Inches(2.62), Inches(6.0), Inches(4.25))
card_text(c, [
    ("推理服务化与平台接入", 15.5, True, TXT, 0),
    ("• twm_serve.py: 独立推理服务, 加载 models/gru_wm.pt", 12.5, False, TXT, 10),
    ("• meta: gru_wm_meta.json 记录维度/归一化/训练配置", 12.5, False, TXT, 6),
    ("• /api/wm/rollout: 动作序列 → 预测轨迹 JSON", 12.5, False, TXT, 6),
    ("• index.html「世界模型 · 动作对照 Rollout」面板:", 12.5, False, TXT, 6),
    ("   点击 Rollout 对照 → 真实轨迹 vs WM 预测双曲线", 12.5, False, ACCENT3, 4),
    ("", 8, False, TXT, 4),
    ("意义 (论文贡献 ③)", 14, True, ACCENT, 8),
    ("首次在 6G 网络语境给出「校准孪生 + 世界模型」的时间外推", 12.5, False, TXT, 6),
    ("基准: 网络智能体可先在孪生+WM 中预演再上真网。", 12.5, False, TXT, 5),
])

# ---------- P8-P12 平台演示 (截图) ----------
def demo_slide(title, kicker, img, cap, img_h=Inches(5.35), img_top=Inches(1.35)):
    s = add_slide(prs, title, kicker)
    w = Emu(int(img_h * 1600 / 900))
    x = Emu(int((SW - w) / 2))
    add_img(s, IMG / img, x, img_top, w=w, h=img_h)
    caption(s, Inches(0.55), Inches(6.78), Inches(12.2), cap)
    return s

demo_slide("06 · 3D 仿真工作台 — 总览", "workbench.html",
           "wb-01-overview.png",
           "信息港 (昌平) 五层数据 3D 渲染: 左侧 episode 列表 (真实 12 + 孪生 6) · 五层开关 · 3D 场景 · 右侧 KPI 卡 + 智能问答 + 详情卡")

demo_slide("06 · 3D 平台 — 倾斜视角与建筑布局", "65 栋 OSM 建筑",
           "wb-02-aerial.png",
           "倾斜俯视: 信息港园区建筑 (OSM 轮廓 + 高度), 3 站点 9 扇区, RSRP 传播栅格地面层, UE 轨迹随时间推进")

demo_slide("06 · 3D 平台 — 点击 UE → 终端测量详情", "五层 · hover/点击",
           "wb-03-ue-detail.png",
           "点击 UE 球体 → 右下详情卡: 服务小区 PCI/频段 · RSRP/SINR/CQI · 上下行速率 · 时延/丢包 (终端测量层)")

demo_slide("06 · 3D 平台 — 基站 KPI + 智能问答", "点击扇区 · 自然语言查数",
           "wb-04-site-kpi-chat.png",
           "点击基站扇区 → KPI 卡 (PCI/频段/NRM 参数); 右侧智能问答: 「城区平均RSRP」→ episode 1 均值 -115.7dBm (n=1,200), 支持自然语言查数据湖")

demo_slide("06 · 3D 平台 — 帧播放与反事实 episode", "同根对照",
           "wb-06-twin-ep.png",
           "▶ 播放按钮驱动 200 帧时间推进 (t=0~9.95s, 50ms/帧); 左侧切换 twin_01 反事实 episode — 同一园区同一 UE 群, 不同动作分支")

demo_slide("06 · 数据底座工作台 (index)", "KPI 时序 · 校准对照",
           "idx-01-map.png",
           "数据底座可视化: KPI 时间序列 (均值±P10/P90) · 网络地图 (UE 轨迹, 颜色=RSRP, 站点三扇区)")

demo_slide("06 · 五项能力结果卡", "常驻数据来源",
           "idx-02-capabilities.png",
           "五项能力一屏总览: Radio map 重构 / 新站预测 / 参数调整信号预测 / KPI 预测 / 用户行为预测 — 每卡常驻数据来源标识 (方案一 §三, 方案二 §四)")

demo_slide("06 · 世界模型 Rollout 对照", "GRU-WM v0.2",
           "idx-03-wm-rollout.png",
           "「Rollout 对照」: 真实轨迹 vs 动作条件 GRU 世界模型预测 — 单步模型自回归外推, 多步 RMSE 较持久性基线降 10.1%")

demo_slide("06 · 信息港园区地图", "OSM · ENU 投影",
           "idx-04-campus.png",
           "园区地图: 65 栋建筑轮廓 + 109 条道路 (OSM Overpass, ODbL) + 3 锚点 (信息港一期/二期/6期), ENU 原点 (40.1515, 116.2335)")

# ---------- P13 保真度验证 ----------
s = add_slide(prs, "07 · 统计保真度验证 (论文级)", "诚实报告")
c = add_card(s, Inches(0.55), Inches(1.25), Inches(6.0), Inches(2.9))
card_text(c, [
    ("验证方法", 15.5, True, TXT, 0),
    ("• KS 检验 (Kolmogorov–Smirnov): 生成分布 vs 真实分布", 12.5, False, TXT, 10),
    ("   逐指标 (RSRP/SINR/速率/时延) 双样本检验", 12, False, TXT_DIM, 4),
    ("• 分位数覆盖: P10/P50/P90 生成值落在真实值域内的比例", 12.5, False, TXT, 7),
    ("• 校准前后对照: 未校准孪生 vs 校准后孪生", 12.5, False, TXT, 7),
    ("• 结果卡常驻: 保真度结论 + 数据来源, 不做黑箱声明", 12.5, False, ACCENT3, 7),
])
c = add_card(s, Inches(6.78), Inches(1.25), Inches(6.0), Inches(2.9))
card_text(c, [
    ("验证结论 (摘要)", 15.5, True, TXT, 0),
    ("• RSRP: 校准后 KS 统计量显著下降, 分布形态对齐", 12.5, False, TXT, 10),
    ("• SINR/速率: 均值与 P10/P90 覆盖率明显改善", 12.5, False, TXT, 7),
    ("• 业务流量: 到达/驻留统计与真实同构", 12.5, False, TXT, 7),
    ("• 局限诚实声明: 阴影相关时间/室内穿透仍为模型假设,", 12.5, False, TXT_DIM, 7),
    ("   论文 Limitations 章节明确列出 (不回避)", 12.5, False, TXT_DIM, 4),
])
c = add_card(s, Inches(0.55), Inches(4.35), Inches(12.23), Inches(2.5))
card_text(c, [
    ("为什么保真度报告是贡献而不是负担 (论文贡献 ①)", 15.5, True, ACCENT, 0),
    ("现网验证反事实动作在物理上不可能 — 我们无法在真网\"重放历史并改变动作\"。", 13, False, TXT, 9),
    ("因此孪生的可信度只能通过分布级校准 + 诚实的不确定性声明来建立。", 13, False, TXT, 6),
    ("这套「校准 + 保真度报告 + 同根对照」方法论本身即是可复用的交付物。", 13, False, ACCENT2, 6),
])

# ---------- P14 论文 ----------
s = add_slide(prs, "08 · 论文进展", "IEEEtran 骨架")
c = add_card(s, Inches(0.55), Inches(1.25), Inches(6.0), Inches(2.6))
card_text(c, [
    ("题目 (暂定)", 15, True, TXT, 0),
    ("Calibrated Counterfactual Digital Twins and", 15, True, ACCENT, 10),
    ("Action-Conditioned World Models in 6G Networks", 15, True, ACCENT, 3),
    ("", 8, False, TXT, 4),
    ("三大贡献声明", 14, True, TXT, 8),
    ("① 校准孪生 + 诚实保真度报告", 12.5, False, TXT, 6),
    ("② 同根反事实数据 — offline RL 关键, 现网不可能获得", 12.5, False, ACCENT2, 4),
    ("③ 时间外推的世界模型基准", 12.5, False, ACCENT3, 4),
])
c = add_card(s, Inches(6.78), Inches(1.25), Inches(6.0), Inches(2.6))
card_text(c, [
    ("章节结构 (8 节)", 15, True, TXT, 0),
    ("1 Introduction · 2 System Overview (数据湖, 校准物理层)", 12.5, False, TXT, 10),
    ("3 Distributional Fidelity (分布保真度)", 12.5, False, TXT, 7),
    ("4 Same-Root Counterfactual Actions (同根反事实)", 12.5, False, TXT, 6),
    ("5 World-Model Benchmark (时间外推基准)", 12.5, False, TXT, 6),
    ("6 Related Work · 7 Limitations · 8 Conclusion", 12.5, False, TXT, 6),
    ("编译: pdflatex 通过, IEEEtran 双栏骨架就绪", 12, False, TXT_DIM, 8),
])
c = add_card(s, Inches(0.55), Inches(4.05), Inches(12.23), Inches(2.8))
card_text(c, [
    ("git 提交史 = 工作日志 (10/30 – 11/1, 8 commits)", 15, True, TXT, 0),
    ("73c7f65  可视化工作台 + Twin 校准完善 + README        15998b6  论文骨架 (IEEEtran)", 12, False, TXT, 10),
    ("5adeb39  统计保真度验证 (论文级) + 业务流量同构        3de62d7  世界模型基线训练 (方案二首期)", 12, False, TXT, 6),
    ("7a39ad7  Twin 动作族 — 反事实同根分支数据              15a0c5a  信息港地图 + 3D 场景 API + WM rollout", 12, False, TXT, 6),
    ("15998b6…                                                            5139c13  3D 工作台五层渲染 + 问答", 12, False, TXT, 6),
    ("节奏: 每完成一个可验证增量即 commit, 与两方案里程碑对齐。", 12.5, False, ACCENT, 8),
])

# ---------- P15 下一步 ----------
s = add_slide(prs, "08 · 下一步计划", "V0.5 → V1")
plans = [
    ("11/27 · V0.5 数据", ACCENT, [
        "• 信息港场景全量 episodes (更多 UE/业务类型)",
        "• MRO 真实数据与孪生输出对齐校验常态化",
        "• 世界模型 v0.3: 长时程 rollout + 不确定性输出",
    ]),
    ("12/31 · V1 季度成果", ACCENT2, [
        "• 论文投稿稿 (实验章 + 图表完整)",
        "• 五项能力全部结果卡带真实数据来源",
        "• 世界模型消融 (架构/动作嵌入/外推长度)",
    ]),
    ("持续 · 工程化", ACCENT3, [
        "• 3D 平台: 低空/室内场景扩展",
        "• 推理服务性能 (批量 rollout, 缓存)",
        "• 数据湖增量入湖管道",
    ]),
]
x = Inches(0.55)
for title, col, items in plans:
    c = add_card(s, x, Inches(1.35), Inches(3.94), Inches(4.6))
    lines = [(title, 15, True, col, 0)]
    for it in items:
        lines.append((it, 12, False, TXT, 10))
    card_text(c, lines)
    x += Emu(int(Inches(4.145)))
c = add_card(s, Inches(0.55), Inches(6.15), Inches(12.23), Inches(0.75))
card_text(c, [
    ("现状: 11/6 V0.1 与 11/13 V0.2 里程碑均已提前达成 — 平台可演示、数据可追溯、论文有骨架。", 13.5, True, ACCENT2, 0),
])

# ---------- P16 结束页 ----------
s = prs.slides.add_slide(prs.slide_layouts[6])
s.background.fill.solid(); s.background.fill.fore_color.rgb = BG
bar = s.shapes.add_shape(1, Inches(0), Inches(0), SW, Inches(0.10))
bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()
bar.shadow.inherit = False
tb = s.shapes.add_textbox(Inches(0.9), Inches(2.6), Inches(11.5), Inches(1.0))
p = tb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
r = p.add_run(); r.text = "谢谢 · 演示环境已就绪"
_set_font(r, 36, True, TXT)
tb = s.shapes.add_textbox(Inches(0.9), Inches(3.7), Inches(11.5), Inches(0.8))
p = tb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
r = p.add_run(); r.text = "http://127.0.0.1:8811/  (数据底座工作台)   ·   /workbench  (3D 仿真工作台)"
_set_font(r, 16, False, ACCENT)
tb = s.shapes.add_textbox(Inches(0.9), Inches(4.5), Inches(11.5), Inches(0.6))
p = tb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
r = p.add_run(); r.text = "6G 数据底座 · 校准数字孪生 · 动作条件世界模型  |  2026-11"
_set_font(r, 13, False, TXT_DIM)

prs.save(OUT)
print(f"saved: {OUT}  slides={len(prs.slides.__iter__.__self__._sldIdLst)}")
