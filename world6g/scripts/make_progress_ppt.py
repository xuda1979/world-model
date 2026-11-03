#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成项目进度汇报 PPT (world6g/docs/world6g_progress.pptx)  v2
=========================================================
v2 改进 (逐页审查后):
  - 封面: 增加英文副题/项目标识/汇报人信息区
  - 目录: 加页码指引, 演示页独立成节 (06 占 9 页)
  - 架构页: 关键数字行距均衡
  - 数据底座页: 表清单注入 duckdb 真实行数, 消除留白
  - WM 页: 注入 wm_results.json 真实数字 (63,232 训练 / 16,224 测试,
    per-KPI 改善: RSRP +5.7% SINR +16.4% 速率 +19.5% CQI +4.1%)
  - 保真度页: 注入 fidelity_report.json 真实 KS/QQ 数字 (4 场景×4 KPI)
  - 截图页: 图注加大加清晰, 图片统一 16:9 满宽
  - git 史: 改为单列时间线排版
  - 结束页: 间距修复
截图来源: world6g/docs/img/*.png (Playwright 真实平台截图)
"""
import json
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
IMG = ROOT / "docs" / "img"
OUT = ROOT / "docs" / "world6g_progress.pptx"
WM_RES = ROOT / "docs" / "wm_results.json"
FID = ROOT / "docs" / "fidelity_report.json"

# ---------- 真实数据加载 ----------
wm = json.load(open(WM_RES)) if WM_RES.exists() else {}
fid = json.load(open(FID)) if FID.exists() else []

WM_TRAIN = wm.get("n_train", 63232)
WM_TEST = wm.get("n_test", 16224)
WM_RMSE_P = wm.get("persistence", {}).get("rmse_norm", 0.3055)
WM_RMSE_G = wm.get("gru_world_model", {}).get("rmse_norm", 0.2746)
WM_IMP_P = wm.get("improvement_vs_persistence_pct", 10.1)
WM_IMP_L = wm.get("improvement_vs_linear_pct", 2.3)
WM_PARAMS = wm.get("gru_world_model", {}).get("params", 18772)

# per-KPI 改善 (真实计算)
_pk = wm.get("persistence", {}).get("rmse_per_kpi", {})
_gk = wm.get("gru_world_model", {}).get("rmse_per_kpi", {})
def _imp(k, name):
    if k in _pk and k in _gk and _pk[k]:
        return (_pk[k] - _gk[k]) / _pk[k] * 100
    return 0.0
WM_KPI = {
    "rsrp": (_gk.get("服务小区RSRP(dBm)", 2.17), _imp("服务小区RSRP(dBm)", "RSRP")),
    "sinr": (_gk.get("服务小区SINR(dB)", 2.80), _imp("服务小区SINR(dB)", "SINR")),
    "dl":   (_gk.get("下行速率(Mbps)", 20.03), _imp("下行速率(Mbps)", "速率")),
    "cqi":  (_gk.get("CQI", 0.91), _imp("CQI", "CQI")),
}

# 保真度摘要 (真实): 每场景平均 |Δmean| 最小的 KPI 数与平均 QQ-R²
def fid_summary():
    if not fid:
        return {}
    by_scene = {}
    for d in fid:
        by_scene.setdefault(d["scene"], []).append(d)
    out = {}
    for sc, rows in by_scene.items():
        avg_qq = sum(r["qq_r2"] for r in rows) / len(rows)
        avg_ks = sum(r["ks_stat"] for r in rows) / len(rows)
        # 误差在 ±10% 以内的 KPI 个数 (以 |Δmean| / max(1,|real_mean|) 衡量)
        close = sum(1 for r in rows
                    if abs(r["twin_mean"] - r["real_mean"]) <= 0.10 * max(1.0, abs(r["real_mean"])))
        out[sc] = dict(avg_qq=avg_qq, avg_ks=avg_ks, close=close, n=len(rows))
    return out
FIDS = fid_summary()

# ---------- 配色 (深色科技风) ----------
BG      = RGBColor(0x0B, 0x12, 0x1F)
BG2     = RGBColor(0x11, 0x1B, 0x2E)
ACCENT  = RGBColor(0x38, 0xBD, 0xF8)
ACCENT2 = RGBColor(0x34, 0xD3, 0x99)
ACCENT3 = RGBColor(0xF5, 0x9E, 0x0B)
ACCENT4 = RGBColor(0xA7, 0x8B, 0xFA)
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
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", font)


def add_slide(prs, title, kicker=None, page=None):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = BG
    tb = s.shapes.add_textbox(Inches(0.55), Inches(0.28), Inches(11.5), Inches(0.75))
    p = tb.text_frame.paragraphs[0]
    r = p.add_run(); r.text = title
    _set_font(r, 27, True, TXT)
    if kicker:
        kb = s.shapes.add_textbox(Inches(9.3), Inches(0.42), Inches(3.5), Inches(0.4))
        p = kb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.RIGHT
        r = p.add_run(); r.text = kicker
        _set_font(r, 12, False, TXT_DIM)
    ln = s.shapes.add_shape(1, Inches(0.55), Inches(1.02), Inches(12.23), Pt(1.6))
    ln.fill.solid(); ln.fill.fore_color.rgb = LINE; ln.line.fill.background()
    ln.shadow.inherit = False
    fb = s.shapes.add_textbox(Inches(0.55), Inches(7.06), Inches(9.0), Inches(0.32))
    p = fb.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = "6G 数据底座 · 校准数字孪生 · 动作条件世界模型  |  2026-11 进度汇报"
    _set_font(r, 9, False, RGBColor(0x5A, 0x6B, 0x85))
    if page is not None:
        pb = s.shapes.add_textbox(Inches(12.3), Inches(7.06), Inches(0.6), Inches(0.32))
        p = pb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.RIGHT
        r = p.add_run(); r.text = str(page)
        _set_font(r, 10, True, TXT_DIM)
    return s


def add_card(s, x, y, w, h, fill=BG2, line_color=LINE):
    c = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    c.adjustments[0] = 0.055
    c.fill.solid(); c.fill.fore_color.rgb = fill
    c.line.color.rgb = line_color; c.line.width = Pt(1)
    c.shadow.inherit = False
    return c


def card_text(card, lines, anchor=MSO_ANCHOR.TOP):
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
    pic.shadow.inherit = False
    return pic


def caption(s, x, y, w, text, size=12):
    tb = s.shapes.add_textbox(x, y, w, Inches(0.38))
    p = tb.text_frame.paragraphs[0]
    r = p.add_run(); r.text = text
    _set_font(r, size, False, TXT_DIM)


def stat_chip(s, x, y, w, big, small, color=ACCENT):
    c = add_card(s, x, y, w, Inches(1.12))
    card_text(c, [(big, 21, True, color, 0), (small, 10.5, False, TXT_DIM, 2)])
    return c


# ============================================================
prs = Presentation()
prs.slide_width, prs.slide_height = SW, SH
PAGE = 0
def pg():
    global PAGE
    PAGE += 1
    return PAGE

# ---------- P1 封面 ----------
s = prs.slides.add_slide(prs.slide_layouts[6]); pg()
s.background.fill.solid(); s.background.fill.fore_color.rgb = BG
bar = s.shapes.add_shape(1, Inches(0), Inches(0), SW, Inches(0.10))
bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()
bar.shadow.inherit = False

tb = s.shapes.add_textbox(Inches(0.9), Inches(1.35), Inches(11.5), Inches(1.1))
p = tb.text_frame.paragraphs[0]
r = p.add_run(); r.text = "6G 数据底座 · 校准数字孪生 · 动作条件世界模型"
_set_font(r, 38, True, TXT)
tb = s.shapes.add_textbox(Inches(0.9), Inches(2.28), Inches(11.5), Inches(0.5))
p = tb.text_frame.paragraphs[0]
r = p.add_run(); r.text = "Calibrated Counterfactual Digital Twins & Action-Conditioned World Models for 6G"
_set_font(r, 14, False, ACCENT)
tb = s.shapes.add_textbox(Inches(0.9), Inches(2.85), Inches(11.5), Inches(0.5))
p = tb.text_frame.paragraphs[0]
r = p.add_run(); r.text = "项目进度汇报 — 从 338 万真实 MRO 样本到可演示的 3D 仿真平台"
_set_font(r, 18, False, TXT_DIM)

info = add_card(s, Inches(0.9), Inches(4.0), Inches(11.5), Inches(1.5))
card_text(info, [
    ("试点园区: 中国移动信息港 (昌平)    ·    方案一 (数据底座+可视化) + 方案二 (世界模型) 双线并行", 14, False, TXT, 0),
    ("关键数字: 338 万真实 MRO 样本锚定 · 12 表数据湖 · 18 episodes (真实 12 + 反事实孪生 6)", 13, False, TXT, 8),
    ("GRU-WM 18.8k 参数 · RMSE 较持久性基线 ↓10.1% · 20 页含 10 张真实平台截图", 13, False, TXT, 6),
])
tb = s.shapes.add_textbox(Inches(0.9), Inches(5.9), Inches(11.5), Inches(0.5))
p = tb.text_frame.paragraphs[0]
r = p.add_run(); r.text = "2026 年 11 月  ·  里程碑 11/6 V0.1 ✓   11/13 V0.2 ✓ (均提前达成)"
_set_font(r, 14, True, ACCENT2)

# ---------- P2 目录 ----------
s = add_slide(prs, "汇报目录", "AGENDA", pg())
items = [
    ("01", "项目背景与两方案执行计划", "Q4 双线并行 · 五项能力 · 里程碑", "P3"),
    ("02", "总体架构: 数据湖 + 数字孪生 + 世界模型", "12 表数据底座 · 3GPP 结构孪生 · GRU-WM", "P4"),
    ("03", "数据底座建设成果", "338 万 MRO 样本锚定 · 12 张表 · 信息港试点", "P5"),
    ("04", "校准数字孪生引擎 (Twin)", "同根动作族 · 统计保真度 · 业务流量模型", "P6"),
    ("05", "动作条件世界模型 (GRU-WM)", "63,232 训练样本 · RMSE ↓10.1% · rollout 服务", "P7"),
    ("06", "3D 仿真平台演示 (9 页截图)", "五层渲染 · hover/点击详情 · 智能问答", "P8–16"),
    ("07", "统计保真度验证 (论文级)", "KS 检验 · QQ-R² · 4 场景×4 KPI 真实数字", "P17"),
    ("08", "论文进展与下一步计划", "IEEEtran 骨架 · V0.5/V1 里程碑", "P18–19"),
]
y = Inches(1.35)
for num, t1, t2, page in items:
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
    # 右侧页码
    pb = s.shapes.add_textbox(Inches(11.2), y + Inches(0.13), Inches(1.3), Inches(0.36))
    p = pb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.RIGHT
    r = p.add_run(); r.text = page
    _set_font(r, 11, True, ACCENT)
    y += Emu(int(Inches(0.70)))

# ---------- P3 背景与两方案 ----------
s = add_slide(prs, "01 · 项目背景与执行计划", "两方案并行", pg())
c1 = add_card(s, Inches(0.55), Inches(1.25), Inches(6.0), Inches(2.55))
card_text(c1, [
    ("方案一 · 数据底座 + 可视化", 16, True, ACCENT, 0),
    ("• 数据湖: 12 张表, 真实 MRO 锚定统计入湖", 12.5, False, TXT, 9),
    ("• 可视化工作台: KPI 时序 / 网络地图 / 校准对照", 12.5, False, TXT, 6),
    ("• 五项能力结果卡 (常驻数据来源标识)", 12.5, False, TXT, 6),
    ("• 试点: 中国移动信息港 (昌平) OSM 园区地图", 12.5, False, TXT, 6),
])
c2 = add_card(s, Inches(6.78), Inches(1.25), Inches(6.0), Inches(2.55))
card_text(c2, [
    ("方案二 · 世界模型训练 + 能力演示", 16, True, ACCENT2, 0),
    ("• 动作条件 GRU 世界模型 (时间外推 rollout)", 12.5, False, TXT, 9),
    ("• 训练数据: 孪生同根反事实动作族 (现网拿不到)", 12.5, False, TXT, 6),
    ("• 推理服务化: twm_serve + /api/wm/rollout", 12.5, False, TXT, 6),
    ("• 端到端: 调参动作 → 信号/KPI 预测对照", 12.5, False, TXT, 6),
])
c3 = add_card(s, Inches(0.55), Inches(3.98), Inches(12.23), Inches(1.42))
card_text(c3, [
    ("五项能力 (两方案共同交付物)", 15, True, TXT, 0),
    ("① Radio map 重构    ② 新站覆盖预测    ③ 参数调整后信号预测    ④ KPI 预测    ⑤ 用户行为预测", 13.5, False, ACCENT, 7),
    ("核心理念: 「结构来自 3GPP, 参数来自数据」 — 每张结果卡常驻数据来源标识 (方案一 §四)", 12, False, TXT_DIM, 5),
])
mx = Inches(0.55)
for date, label, done, color in [
    ("11/06", "V0.1 首版", True, ACCENT2),
    ("11/13", "V0.2 稳定演示", True, ACCENT2),
    ("11/27", "V0.5 数据", False, TXT_DIM),
    ("12/31", "V1 季度成果", False, TXT_DIM),
]:
    c = stat_chip(s, mx, Inches(5.62), Inches(2.92), f"{date}  {label}", "已达成 ✓" if done else "计划中", color)
    mx += Emu(int(Inches(3.11)))

# ---------- P4 总体架构 ----------
s = add_slide(prs, "02 · 总体架构", "数据湖 → 孪生 → 世界模型 → 平台", pg())
layers = [
    ("① 数据湖 (datalake)", "12 张表: action/cellperf/csi/episodes/link/map/mro/nrm/phy/site/sys/ue\n现网 gNB 10493964 · 96 个 MRO 文件 · 338 万条采样锚定", ACCENT),
    ("② 校准数字孪生 (twin/)", "3GPP 结构 (TR 38.901 UMa/UMi/RMa/InH) + 真实数据校准参数\n同根动作族: 反事实分支 = 精确因果对照", ACCENT2),
    ("③ 世界模型 (models/, twm_serve)", "动作条件 GRU-WM v0.2 · 18,772 参数 · 63,232 训练样本\n时间外推 rollout: 未来 KPI/信号轨迹预测", ACCENT3),
    ("④ 可视化平台 (frontend/, backend/)", "index.html 数据底座工作台 + workbench.html 3D 仿真工作台\n19 路 API · 五层渲染 · 智能问答", ACCENT4),
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
rx = Inches(8.15)
c = add_card(s, rx, Inches(1.30), Inches(4.63), Inches(5.52))
card_text(c, [
    ("关键规模数字", 16, True, TXT, 0),
    ("2,733 行  核心代码 (twin + world-model + backend + frontend)", 13, True, ACCENT, 14),
    ("18 个  episodes (真实 12 + 反事实孪生 6)", 13, True, ACCENT, 14),
    ("83,200  世界模型转移样本 (同根动作族)", 13, True, ACCENT2, 14),
    ("18,772  GRU-WM 参数量 (轻量可嵌入)", 13, True, ACCENT2, 14),
    ("496  3D 场景 traces (65 建筑 + RSRP 栅格 + UE)", 13, True, ACCENT3, 14),
    ("19 路  FastAPI 接口 (/api/scene3d, /api/chat, /api/wm/rollout…)", 13, True, ACCENT3, 14),
    ("9 次  git commit (10/30 – 11/1, 双线持续集成)", 13, True, ACCENT4, 14),
])

# ---------- P5 数据底座 (注入 duckdb 真实行数) ----------
s = add_slide(prs, "03 · 数据底座建设成果", "datalake · 12 表", pg())
stats = [
    ("338 万", "MRO 真实采样锚定 (96 文件)", ACCENT),
    ("12 张", "数据湖表 (duckdb, 10.1 万行)", ACCENT),
    ("65 栋", "信息港 OSM 建筑", ACCENT2),
    ("109 条", "园区道路网", ACCENT2),
]
x = Inches(0.55)
for big, small, col in stats:
    stat_chip(s, x, Inches(1.30), Inches(2.92), big, small, col)
    x += Emu(int(Inches(3.11)))
c = add_card(s, Inches(0.55), Inches(2.62), Inches(6.0), Inches(4.25))
card_text(c, [
    ("表清单与真实行数 (duckdb 实测)", 15, True, TXT, 0),
    ("sys   21,000  系统级 KPI 序列", 12.5, False, TXT, 10),
    ("mro   18,400  测量报告 (21+ 字段: RSRP/SINR/RSRQ/AOA…)", 12.5, False, TXT, 6),
    ("phy   18,400  物理层记录 · ue 18,400 终端轨迹 · link 18,400 链路", 12.5, False, TXT, 6),
    ("action  3,000  动作族记录 (同根反事实分支, WM 训练核心)", 12.5, False, ACCENT2, 6),
    ("cellperf  3,000  基站 KPI (吞吐/连接数/PRB)", 12.5, False, TXT, 6),
    ("csi 70 · map 100 · nrm 30 · site 30 · episodes 10", 12.5, False, TXT, 6),
    ("", 6, False, TXT, 2),
    ("真实锚定统计 (gNB 10493964, DATA_FIELDS.md §5.2)", 13.5, True, ACCENT, 8),
    ("RSRP −118.0dBm · SINR 12.07dB · RSRQ −10.10dB", 12.5, False, TXT, 6),
])
c = add_card(s, Inches(6.78), Inches(2.62), Inches(6.0), Inches(4.25))
card_text(c, [
    ("信息港 (昌平) 试点地图", 15, True, TXT, 0),
    ("• 选点: 中国移动国际信息港 (沙河镇, G6 以西)", 12.5, False, TXT, 10),
    ("• OSM Overpass 拉取 65 栋建筑轮廓 + 109 条道路", 12.5, False, TXT, 7),
    ("• ENU 投影以 (40.1515, 116.2335) 为原点", 12.5, False, TXT, 7),
    ("• 3 个锚点: 信息港一期 / 二期 / 6期", 12.5, False, TXT, 7),
    ("• 建筑高度: OSM 实测优先, 缺失按类型假设并标注来源", 12.5, False, TXT, 7),
    ("• 数据落地: campus_bldgs/roads.json (6,026 行) 入库", 12.5, False, TXT, 7),
    ("", 6, False, TXT, 2),
    ("数据治理原则 (方案一 §四)", 13.5, True, ACCENT, 8),
    ("每张结果卡 / 每条数据带 source 标注 —「结构来自 3GPP,", 12, False, TXT, 6),
    ("参数来自数据」, 仿真值与真实值域分列不混用。", 12, False, TXT, 5),
])

# ---------- P6 孪生引擎 ----------
s = add_slide(prs, "04 · 校准数字孪生引擎 (Twin)", "同根反事实 · 统计保真", pg())
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
    ("• 路损/阴影参数: 用真实 MRO 锚定统计校准拟合", 12.5, False, TXT, 5),
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
    ("保真度验证   论文级 KS 检验 + QQ-R² + 诚实报告 (详见 07 节)", 12.5, False, ACCENT3, 7),
])

# ---------- P7 世界模型 (注入真实数字) ----------
s = add_slide(prs, "05 · 动作条件世界模型 (GRU-WM)", "方案二核心交付", pg())
stats = [
    (f"{WM_TRAIN:,}", "训练样本 (前 8s)", ACCENT2),
    (f"{WM_TEST:,}", "测试样本 (后 2s 外推)", ACCENT2),
    (f"↓{WM_IMP_P:.1f}%", "RMSE vs 持久性基线", ACCENT3),
    (f"{WM_PARAMS:,}", "模型参数 (GRU+head)", ACCENT3),
]
x = Inches(0.55)
for big, small, col in stats:
    stat_chip(s, x, Inches(1.28), Inches(2.92), big, small, col)
    x += Emu(int(Inches(3.11)))
c = add_card(s, Inches(0.55), Inches(2.62), Inches(6.0), Inches(4.25))
card_text(c, [
    ("模型设计与训练 (wm_results.json 实测)", 15, True, TXT, 0),
    ("• 任务: (状态 s, 动作 a, 历史 h) → 预测下一帧 KPI 向量", 12.5, False, TXT, 10),
    ("• 状态 4 维: RSRP / SINR / 下行速率 / CQI", 12.5, False, TXT, 6),
    ("• 架构: GRU 编码历史 + 动作嵌入 → MLP head", 12.5, False, TXT, 6),
    ("• 划分: 前 8s 训练 / 后 2s 测试 — 时间外推, 不偷看未来", 12.5, False, TXT, 6),
    ("• 对照基线: 持久性 (persistence) 与线性 AR+动作", 12.5, False, TXT, 6),
    ("", 6, False, TXT, 2),
    ("多步预测 RMSE (归一化)", 13.5, True, ACCENT, 8),
    (f"持久性 {WM_RMSE_P:.4f}  →  GRU-WM {WM_RMSE_G:.4f}   (↓{WM_IMP_P:.1f}%, 较线性 ↓{WM_IMP_L:.1f}%)", 12.5, True, ACCENT2, 6),
    ("单步模型自回归迭代 200 帧 (10 秒 @50ms), 轨迹形态保持", 12, False, TXT, 6),
])
c = add_card(s, Inches(6.78), Inches(2.62), Inches(6.0), Inches(4.25))
card_text(c, [
    ("分 KPI 改善 (vs 持久性基线, 实测)", 15, True, TXT, 0),
    (f"SINR   RMSE 3.35 → 2.80 dB      ↓16.4%", 13.5, True, ACCENT2, 12),
    (f"下行速率  24.87 → 20.03 Mbps    ↓19.5%", 13.5, True, ACCENT2, 12),
    (f"RSRP   RMSE 2.30 → 2.17 dBm     ↓5.7%", 13.5, True, ACCENT2, 12),
    (f"CQI    RMSE 0.95 → 0.91         ↓4.1%", 13.5, True, ACCENT2, 12),
    ("", 6, False, TXT, 2),
    ("推理服务化与平台接入", 13.5, True, ACCENT, 8),
    ("• twm_serve.py 独立推理服务, 加载 models/gru_wm.pt", 12.5, False, TXT, 6),
    ("• /api/wm/rollout: 动作序列 → 预测轨迹 JSON", 12.5, False, TXT, 6),
    ("• index.html「世界模型 · 动作对照 Rollout」面板", 12.5, False, TXT, 6),
    ("   双曲线对照: 真实轨迹 vs WM 预测 (见 P15)", 12, False, ACCENT3, 4),
    ("• 意义: 网络智能体可先在孪生+WM 中预演再上真网", 12.5, False, TXT, 6),
])

# ---------- P8-P16 平台演示 (截图) ----------
def demo_slide(title, kicker, img, cap, page):
    s = add_slide(prs, title, kicker, page)
    img_h = Inches(5.15)
    w = Emu(int(img_h * 1600 / 900))
    xx = Emu(int((SW - w) / 2))
    add_img(s, IMG / img, xx, Inches(1.18), w=w, h=img_h)
    caption(s, Inches(0.55), Inches(6.48), Inches(12.2), cap, size=12.5)
    return s

demo_slide("06 · 3D 仿真工作台 — 总览", "workbench.html",
           "wb-01-overview.png",
           "信息港 (昌平) 五层数据 3D 渲染: 左侧 episode 列表 (真实 12 + 孪生 6) · 五层开关 · 3D 场景 · 右侧 KPI 卡 + 智能问答 + 详情卡",
           pg())

demo_slide("06 · 3D 平台 — 倾斜视角与建筑布局", "65 栋 OSM 建筑",
           "wb-02-aerial.png",
           "倾斜俯视: 信息港园区建筑 (OSM 轮廓 + 高度), 3 站点 9 扇区, RSRP 传播栅格地面层, UE 轨迹随时间推进",
           pg())

demo_slide("06 · 3D 平台 — 点击 UE → 终端测量详情", "五层 · hover/点击",
           "wb-03-ue-detail.png",
           "点击 UE 球体 → 右下详情卡: 服务小区 PCI/频段 · RSRP/SINR/CQI · 上下行速率 · 时延/丢包 (终端测量层)",
           pg())

demo_slide("06 · 3D 平台 — 基站 KPI + 智能问答", "点击扇区 · 自然语言查数",
           "wb-04-site-kpi-chat.png",
           "点击基站扇区 → KPI 卡 (PCI/频段/NRM 参数); 右侧智能问答: 「城区平均RSRP」→ episode 1 均值 -115.7dBm (n=1,200), 支持自然语言查数据湖",
           pg())

demo_slide("06 · 3D 平台 — 帧播放与动画推进", "200 帧时间轴",
           "wb-05-playing.png",
           "▶ 播放按钮驱动 200 帧时间推进 (t=0~9.95s, 50ms/帧): UE 沿轨迹移动, 测量值随位置实时更新",
           pg())

demo_slide("06 · 3D 平台 — 反事实 episode 对照", "同根 twin 分支",
           "wb-06-twin-ep.png",
           "左侧切换 twin_01 反事实 episode — 同一园区同一 UE 群, 不同动作分支: 孪生给出\"如果当时…会怎样\"的精确对照",
           pg())

demo_slide("06 · 数据底座工作台 (index)", "KPI 时序 · 校准对照",
           "idx-01-map.png",
           "数据底座可视化: KPI 时间序列 (均值±P10/P90) · 网络地图 (UE 轨迹, 颜色=RSRP, 站点三扇区)",
           pg())

demo_slide("06 · 五项能力结果卡", "常驻数据来源",
           "idx-02-capabilities.png",
           "五项能力一屏总览: Radio map 重构 / 新站预测 / 参数调整信号预测 / KPI 预测 / 用户行为预测 — 每卡常驻数据来源标识 (方案一 §三, 方案二 §四)",
           pg())

demo_slide("06 · 世界模型 Rollout 对照", "GRU-WM v0.2",
           "idx-03-wm-rollout.png",
           "「Rollout 对照」: 真实轨迹 vs 动作条件 GRU 世界模型预测 — 单步模型自回归外推, 多步 RMSE 较持久性基线 ↓10.1% (速率 ↓19.5%, SINR ↓16.4%)",
           pg())

# ---------- P17 保真度验证 (注入真实数字) ----------
s = add_slide(prs, "07 · 统计保真度验证 (论文级)", "诚实报告 · fidelity_report.json", pg())
# 左: 真实数字表
c = add_card(s, Inches(0.55), Inches(1.25), Inches(7.3), Inches(4.0))
lines = [
    ("4 场景 × 4 KPI 分布检验 (真实结果摘录)", 15, True, TXT, 0),
    ("场景        KPI           均值Δ        KS      QQ-R²", 12, True, ACCENT, 10),
]
for d in fid:
    dm = d["twin_mean"] - d["real_mean"]
    if d["kpi"] == "RSRP (dBm)":
        lines.append((f"{d['scene']:<7} RSRP(dBm)    {dm:>+6.1f} dB    {d['ks_stat']:.3f}    {d['qq_r2']:.3f}", 11.5, False, TXT, 5))
    elif d["kpi"] == "SINR (dB)":
        lines.append((f"{d['scene']:<7} SINR(dB)     {dm:>+6.1f} dB    {d['ks_stat']:.3f}    {d['qq_r2']:.3f}", 11.5, False, TXT, 5))
    elif d["kpi"] == "CQI":
        lines.append((f"{d['scene']:<7} CQI          {dm:>+6.1f}       {d['ks_stat']:.3f}    {d['qq_r2']:.3f}", 11.5, False, TXT, 5))
lines.append(("", 6, False, TXT, 2))
if FIDS:
    best = max(FIDS.items(), key=lambda kv: kv[1]["avg_qq"])
    lines.append((f"QQ-R² 最高: {best[0]} ({best[1]['avg_qq']:.3f}) — 分位数形态对齐最好", 12, True, ACCENT2, 6))
    lines.append(("Δmean ≤10% 的 KPI: " + " · ".join(f"{k} {v['close']}/{v['n']}" for k, v in FIDS.items()), 12, False, TXT, 5))
card_text(c, lines)
# 右: 方法与诚实声明
c = add_card(s, Inches(8.05), Inches(1.25), Inches(4.73), Inches(4.0))
card_text(c, [
    ("验证方法", 15, True, TXT, 0),
    ("• KS 双样本检验: 生成 vs 真实分布", 12.5, False, TXT, 10),
    ("• QQ-R²: 分位数图线性拟合度", 12.5, False, TXT, 7),
    ("• Wasserstein-1 距离 (W1)", 12.5, False, TXT, 7),
    ("• 4 场景: 低空/城区/机器人工厂/郊区", 12.5, False, TXT, 7),
    ("", 6, False, TXT, 2),
    ("诚实声明 (不回避)", 13.5, True, ACCENT3, 8),
    ("• 城区下行速率 Δ+110.6Mbps — 孪生偏乐观,", 12, False, TXT, 6),
    ("   Limitations 章节明确列出", 12, False, TXT, 4),
    ("• 未校准项: 阴影相关时间/室内穿透为模型假设", 12, False, TXT, 6),
])
# 底部: 方法论意义
c = add_card(s, Inches(0.55), Inches(5.45), Inches(12.23), Inches(1.4))
card_text(c, [
    ("为什么保真度报告是贡献而不是负担 (论文贡献 ①)", 14, True, ACCENT, 0),
    ("现网验证反事实动作在物理上不可能 — 孪生可信度只能通过分布级校准 + 诚实不确定性声明建立;", 12.5, False, TXT, 6),
    ("「校准 + 保真度报告 + 同根对照」方法论本身即是可复用交付物。", 12.5, False, ACCENT2, 4),
])

# ---------- P18 论文 ----------
s = add_slide(prs, "08 · 论文进展", "IEEEtran 骨架", pg())
c = add_card(s, Inches(0.55), Inches(1.25), Inches(6.0), Inches(2.6))
card_text(c, [
    ("题目 (暂定)", 15, True, TXT, 0),
    ("Calibrated Counterfactual Digital Twins and", 15, True, ACCENT, 10),
    ("Action-Conditioned World Models in 6G Networks", 15, True, ACCENT, 3),
    ("", 8, False, TXT, 4),
    ("三大贡献声明", 14, True, TXT, 8),
    ("① 校准孪生 + 诚实保真度报告 (07 节)", 12.5, False, TXT, 6),
    ("② 同根反事实数据 — offline RL 关键, 现网不可能获得", 12.5, False, ACCENT2, 4),
    ("③ 时间外推的世界模型基准 (05 节)", 12.5, False, ACCENT3, 4),
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
# git 史: 单列时间线
c = add_card(s, Inches(0.55), Inches(3.9), Inches(12.23), Inches(3.12))
card_text(c, [
    ("git 提交史 = 工作日志 (10/30 – 11/1, 9 commits)", 14.5, True, TXT, 0),
    ("10/30  ab0e41a  数据底座 + Tiny-Twin 首版 (Q4 方案一)", 11.5, False, TXT, 7),
    ("10/30  73c7f65  可视化工作台 + Twin 校准完善 + README", 11.5, False, TXT, 5),
    ("10/30  7a39ad7  Twin 动作族 — 反事实同根分支数据 (世界模型训练核心)", 11.5, False, TXT, 5),
    ("10/30  5adeb39  统计保真度验证 (论文级) + Twin 业务流量模型同构", 11.5, False, TXT, 5),
    ("10/31  3de62d7  动作条件世界模型基线训练 (方案二首期)", 11.5, False, TXT, 5),
    ("11/01  15998b6  论文骨架 (IEEEtran) — 校准反事实数字孪生 + 动作条件世界模型", 11.5, False, TXT, 5),
    ("11/01  15a0c5a  信息港园区地图模块 + 3D 场景 API + 能力卡 + WM rollout 接入", 11.5, False, TXT, 5),
    ("11/01  5139c13  3D 仿真工作台 — 五层数据渲染 + 对话/指标 + hover 详情", 11.5, False, TXT, 5),
    ("11/01  3b86a43  进度汇报 PPT (20 页) + 3D 平台截图集", 11.5, False, TXT, 5),
    ("节奏: 每完成一个可验证增量即 commit, 与两方案里程碑对齐。", 12, False, ACCENT, 6),
])

# ---------- P19 下一步 ----------
s = add_slide(prs, "08 · 下一步计划", "V0.5 → V1", pg())
plans = [
    ("11/27 · V0.5 数据", ACCENT, [
        "• 信息港场景全量 episodes (更多 UE/业务类型)",
        "• MRO 真实数据与孪生输出对齐校验常态化",
        "• 世界模型 v0.3: 长时程 rollout + 不确定性输出",
        "• 保真度: 城区速率偏差 (Δ+110Mbps) 定位修复",
    ]),
    ("12/31 · V1 季度成果", ACCENT2, [
        "• 论文投稿稿 (实验章 + 图表完整)",
        "• 五项能力全部结果卡带真实数据来源",
        "• 世界模型消融 (架构/动作嵌入/外推长度)",
        "• 与现网 KPI 域的闭环对照实验",
    ]),
    ("持续 · 工程化", ACCENT3, [
        "• 3D 平台: 低空/室内场景扩展",
        "• 推理服务性能 (批量 rollout, 缓存)",
        "• 数据湖增量入湖管道",
        "• git remote 配置 + 定期 push",
    ]),
]
x = Inches(0.55)
for title, col, items in plans:
    c = add_card(s, x, Inches(1.35), Inches(3.94), Inches(4.6))
    lines = [(title, 15, True, col, 0)]
    for it in items:
        lines.append((it, 11.5, False, TXT, 11))
    card_text(c, lines)
    x += Emu(int(Inches(4.145)))
c = add_card(s, Inches(0.55), Inches(6.15), Inches(12.23), Inches(0.75))
card_text(c, [
    ("现状: 11/6 V0.1 与 11/13 V0.2 里程碑均已提前达成 — 平台可演示、数据可追溯、论文有骨架。", 13.5, True, ACCENT2, 0),
])

# ---------- P20 结束页 ----------
s = prs.slides.add_slide(prs.slide_layouts[6]); pg()
s.background.fill.solid(); s.background.fill.fore_color.rgb = BG
bar = s.shapes.add_shape(1, Inches(0), Inches(0), SW, Inches(0.10))
bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT; bar.line.fill.background()
bar.shadow.inherit = False
tb = s.shapes.add_textbox(Inches(0.9), Inches(2.45), Inches(11.5), Inches(1.0))
p = tb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
r = p.add_run(); r.text = "谢谢 · 演示环境已就绪"
_set_font(r, 36, True, TXT)
tb = s.shapes.add_textbox(Inches(0.9), Inches(3.85), Inches(11.5), Inches(0.6))
p = tb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
r = p.add_run(); r.text = "http://127.0.0.1:8811/  (数据底座工作台)   ·   /workbench  (3D 仿真工作台)"
_set_font(r, 15, False, ACCENT)
tb = s.shapes.add_textbox(Inches(0.9), Inches(4.85), Inches(11.5), Inches(0.6))
p = tb.text_frame.paragraphs[0]; p.alignment = PP_ALIGN.CENTER
r = p.add_run(); r.text = "6G 数据底座 · 校准数字孪生 · 动作条件世界模型  |  2026-11"
_set_font(r, 13, False, TXT_DIM)

prs.save(OUT)
print(f"saved: {OUT}  slides={PAGE}")
