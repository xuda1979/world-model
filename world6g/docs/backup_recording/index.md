# V0.2 备用演示录像 (截图序列, 2026-11-13)

- **对应交付**: 方案一 §7 表4 11月13日 V0.2 "备用录像" — 13 日现场演示若不可用, 按本序列替代走查
- **对照脚本**: `docs/demo_script_v02.md` (冻结版, 2026-11-12) — 本序列按其分钟轴逐段截取, 冻结数字一致
- **形态说明**: 演示机无 ffmpeg → 采用 **逐段截图序列** (与仓库既有截图验收证据惯例一致);
  每帧 = demo_script_v02.md 对应演示段的完整工作台画面 (1600×1000)
- **生成方式**: Playwright 直驱 live 演示服务 (127.0.0.1:8811), 真实 API 调用, 非摆拍

## 时间轴 (对齐 demo_script_v02.md)

| 帧 | 演示段 (脚本分钟) | 内容 | 冻结数字对照 |
|---|---|---|---|
| `00_workbench_entry.png` | 入口 | 工作台加载 (episodes 左栏 + 3D 中栏 + KPI/对话右栏) | — |
| `01_3d_scene_twin01.png` | 数据基础 (分钟 1–2) | 左栏选 `twin_01`, 3D 场景五层渲染 | 场景名"中国移动信息港" |
| `02_coord_recommendation_frozen_inputs.png` | 覆盖重构/协同 (分钟 2–3) | 协同策略推荐: 城区, SINR=23.0,-1.4,33.0, UE=8,15,6 | Top-5 表 + 部署动作 |
| `03_tick_replay_tick8.png` | 调参与动态后果 (分钟 3–5) | tick 回放逐 tick 前进至高光步 tick 8 | Σreward 累计 +5.398 (整段 4.732) |
| `04_cross_scene_dikong.png` | 跨场景泛化 (分钟 2–3 续) | 同输入切低空场景再推荐 | 跨场景推荐一致可用 |
| `05_chat_three_queries.png` | 行为与 KPI 预测 (分钟 8–9) | 智能问答 3 例: 城区平均RSRP / 五层是什么 / twin_01 吞吐 | 规则引擎即时回答 |
| `06_guard_422.png` | 边界与优势证据 (分钟 9–10①) | 非法输入 (SINR=999 越界) → 前端红字 + 服务端 422 | 护栏生效, 不 500 |
| `07_seed10000_replay.png` | 世界模型预测 (分钟 5–7) | tick 回放切 seed 10000 再回放 (泛化切片) | 换切片回放正常 |

## 导出验证 (分钟 9–10②, 同会话机器验证)

- CSV: `GET /api/coord/ticks/export?scene=campus&seed=9000&ep_i=0` → 11 行 (表头 + 10 tick),
  `coord_ticks_campus_s9000_ep0.csv`, 表头 `scene,seed,ep_i,dec_idx,t_ms,p0,...,reward,...` ✓
- JSON: `POST /api/coord/action` (城区, 冻结输入) → 部署动作 `+6,+6,+6`, Top-5 表 ✓
- 机器证据: `datalake/generated/v02_acceptance_evidence.json` (三轮 3/3 无阻断, P95 359.6ms)

## 复现命令

```bash
# 1. 启动演示服务
.venv/bin/python3 world6g/backend/api.py   # 127.0.0.1:8811
# 2. 浏览器打开 http://127.0.0.1:8811/workbench, 按 demo_script_v02.md 时间轴操作
# 3. 机器重放证据 (替代人工截图的验收口径)
.venv/bin/python world6g/scripts/probe_v02_acceptance.py
```
