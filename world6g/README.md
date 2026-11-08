# 6G 数据底座与可视化 + Tiny-Twin + GRU 世界模型闭环 (v2)

基于两份 Q4 执行方案 (数据底座与可视化 / 世界模型训练与能力演示) 搭建。
v2 已完成方案二 §四的核心闭环: **预测 → 执行 → 验证** (GRU-WM 多步 rollout vs Tiny-Twin 同 seed 重放真值)。

## 架构

```
world-model/
├── Q4执行方案一_数据底座与可视化_20261030.docx   # 源方案
├── Q4执行方案二_世界模型训练与能力演示_20261030.docx
└── world6g/
    ├── datalake/          # 数据底座 (三层: raw → curated → generated)
    │   ├── config.py      # 路径/物理常量/真实锚定统计 (单一事实来源)
    │   ├── raw/registry.json       # 源数据指纹 + 真实 MRO 锚定 (可追溯)
    │   ├── curated/                # 10 episodes 真实数据 → Parquet + DuckDB
    │   └── generated/              # Tiny-Twin 8 episodes + 同根反事实动作分支
    ├── twin/              # Tiny-Twin 引擎 (可校准参数结构, 同 seed 确定性重放)
    ├── scripts/
    │   ├── ingest.py               # 真实数据入湖
    │   ├── generate_twin.py        # Twin 批量生成 + 校准报告
    │   ├── generate_actions.py     # 同根反事实分支数据集 (twin_action_dataset)
    │   ├── validate_twin.py        # Twin 校准验证
    │   ├── train_world_model.py    # GRU-WM 训练入口 (v0.2)
    │   └── train_wm_ms.py          # 多步一致性训练 (v0.3-MS)
    ├── wm/                # 世界模型训练套件 (方案二)
    │   ├── data.py models.py train.py   # 数据 / 模型 / 训练循环
    │   └── metrics.py evaluate.py       # 评测指标与基准
    ├── models/            # gru_wm.pt (v0.2) / gru_wm_ms.pt (v0.3-MS) + meta
    ├── twm_serve.py       # 推理服务: 动作条件多步 rollout (线程安全懒加载)
    ├── backend/api.py     # FastAPI (16 端点, 线程安全 DuckDB)
    └── frontend/index.html  # 可视化工作台 (Plotly 单页 + 闭环验证表)
```

## 快速开始

```bash
uv venv .venv --python 3.12 && .venv/bin/pip install duckdb pandas pyarrow fastapi "uvicorn[standard]" plotly torch

# 1. 数据入湖 (10 episodes 真实数据 → Parquet + DuckDB)
#    ⚠️ 前置: 源数据目录为机器绝对路径 (datalake/config.py: SIM_ROOT)
#    /Users/daxu/software/6G_data_simulation
#    若源在其他位置, 修改 datalake/config.py 的 SIM_ROOT 指向你的
#    6G_data_simulation 目录 (需含 6G_episode_data/6G_episodes_10s_fullcatalog)
.venv/bin/python world6g/scripts/ingest.py

# 2. Tiny-Twin 生成仿真数据 (4 场景 × 2, 自动校准报告)
.venv/bin/python world6g/scripts/generate_twin.py --n-per-scene 2

# 3. 同根反事实动作分支数据集 (83,200 rollout 样本)
.venv/bin/python world6g/scripts/generate_actions.py

# 4. 训练 GRU-WM (v0.2 单步)
.venv/bin/python world6g/scripts/train_world_model.py
#    多步一致性 v0.3-MS (当前 serving 默认)
.venv/bin/python world6g/scripts/train_wm_ms.py

# 5. 启动可视化 (http://127.0.0.1:8811)
.venv/bin/python -m uvicorn world6g.backend.api:app --port 8811
```

## 可复现性 (已 4 次独立验证, 回执入库)

全链路在固定 seed 下**完全确定**, 详见 `docs/closedloop/reproduction_receipt.md`:

| 验证 | 结论 |
|---|---|
| ① 评测复现 | 已提交权重 + 重生成数据 → 96 窗闭环结果与已提交 JSON 逐位一致 (仅时间戳/耗时) |
| ② 重训练复现 | seed 42 从零重训 → `gru_wm_ms.pt` / `policy_net.pt` **SHA 逐位相同**, 训练即复现 |
| ③ 服务冷启动 | 新 clone 启动 uvicorn, 16 端点冒烟全通过, 权重可加载上线 |
| ④ 闭环指标 | CEM-MPC lift +0.2852 [CI +0.113, +0.458], Policy +0.2825, Random 阴性对照 −0.0099 ✓ |

复现命令 (与已提交 results.json meta 对齐): `eval_closedloop.py --n-per-scene 24 --seed 2026`。
`wm/rollout` / `wm/verify` 请求体键名为 `episode_id` (非 `ep_id`)。

## Tiny-Twin 校准方法 (拟合真实数据)

不复制原生成器代码, 而是将其物理链路作为**可校准参数结构**, 全部参数从数据湖拟合:

| 模块 | 结构 | 校准来源 (数据湖) |
|------|------|------------------|
| 路损 | 3GPP 38.901 UMa/UMi NLOS | (RSRP, 3D 距离) 回归: 结构模型残差 sd=7.1dB |
| 阴影 | AR(1), ρ=exp(−v·dt/d_cor) | 残差自相关 → d_cor≈37.7m, σ=6.0dB (城区) |
| 扇区增益 | 主瓣 17dBi 二次衰减, FB 30dB | 服务扇区方位分布拟合 |
| 切换 | A3 事件 (Off=2dB, TTT=160ms) | 真实 MRO 统计锚定 |
| 业务 | 时变吞吐 (功率余量约束) | 下行速率分布拟合 |

引擎**确定性**: 同 seed 重放与 parquet 存档逐位一致 (已验证 max|diff|=0.0000),
这是"预测-验证"闭环中"执行"产出真值的前提。

## 世界模型 (方案二 §四, GRU-WM)

- **架构**: 动作条件 GRU (hist=8, 状态=RSRP/SINR/DL/CQI, 5 动作族: baseline/pmax/prb/az/ho)
- **训练数据**: 同根反事实分支 (twin_action_dataset, 83,200 rollout 样本) — 同一 seed 下 baseline 与动作分支只差动作本身, 因果效应精确可分离
- **v0.3-MS**: 多步一致性训练 (rollout windows), 长程自回归更稳
- **四任务基准** (`wm/bench.py`): 单步预测 / 多步 rollout / 动作效应 / 场景迁移 (详见 `wm/` 与训练报告)

## 预测→执行→验证闭环 (v2 核心)

```
预测:  GRU-WM 对 baseline / 动作分支各 rollout K 步 (自回归)
执行:  Tiny-Twin 引擎同 seed 重放两条真值轨 (逐位一致 = 真值)
验证:  逐 KPI RMSE (vs Persistence 末值保持参考) + Δ效应误差 (预测Δ vs 真值Δ)
```

前端「世界模型」面板双模式切换: **反事实对照** (纯预测) / **预测→执行→验证** (闭环 + 验证表)。
冒烟: 8 twin episodes × 7 动作 = 56 组合全通过 (预测 ~15ms, 执行 ~260ms/次)。

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /api/episodes` | 18 episodes 清单 (真实+Twin) |
| `GET /api/kpi/{id}?metric=` | KPI 时序 (7 指标) |
| `GET /api/map/{id}` | UE 轨迹 + 站点 |
| `GET /api/compare` | 全 episode 对比 |
| `GET /api/fields` | 数据字典 249 字段 |
| `GET /api/twin/report` | Twin 校准报告 |
| `GET /api/scene3d/{id}` | 3D 场景 (五层渲染) |
| `GET /api/layers` | 五层定义 |
| `GET /api/capabilities` | 五项能力卡 |
| `GET /api/campus` | 园区地图 (信息港·昌平) |
| `POST /api/wm/rollout` | WM 多步 rollout (baseline vs 动作) |
| `POST /api/wm/verify` | **预测-验证闭环** (rollout + 同 seed 重放 + RMSE/Δ 验证; `action="auto"` 由动作模型决策) |
| `POST /api/wm/action` | **动作模型决策端点** (PolicyNet-Fused 一步前馈, `policy_action` 与闭环评测逐位一致) |
| `POST /api/chat` | 工作台对话问答 (规则式, 数据湖驱动) |

## 可视化工作台

- **KPI 时序**: 7 指标 (RSRP/SINR/DL/UL/时延/CQI/丢包率), 均值±P10/P90 带 + 每 UE 曲线
- **网络地图**: UE 轨迹 (颜色=RSRP Turbo 色阶) + 站点三扇区覆盖
- **小区吞吐**: 每小区 Mbps 时序
- **全 Episode 对比**: RSRP×SINR 散点 (真实圆点 vs Twin 菱形)
- **Twin 校准**: 生成值 vs 真实值域 (误差条)
- **世界模型闭环**: 双模式 (反事实对照 / 预测→执行→验证), 4 KPI × (预测/真值) × (base/动作) 曲线 + 逐 KPI 验证表 (RMSE vs Persistence, Δ效应误差)
- **五项能力卡**: Radio map / 新站预测 / KPI 预测 / 动作效应 / 干扰分析 (含模型可用性标识)

## 下一步

1. 11/13 节点: 闭环演示稳定化 (PPT 嵌入实测数据)
2. WM 迭代: 场景迁移任务 (Task D) 提升 → 接入更多真实 episode 微调;
   **融合奖励已验证并可部署**: `fuse_wm_planner.py` λ≈0.5 兼得闭环保真度与进攻性
   (lift 0.469 vs 0.269/0.404), 并已蒸馏为单网络动作模型
   `train_planner_fused.py` → `policy_fused_net.pt` —
   **闭环 lift +0.550 [CI +0.361,+0.729] P>0=1.000** (唯一真实奖励为正的策略);
   **已部署**: 叠加前馈网络 `twm_serve.policy_action` 上线, `POST /api/wm/action`
   决策端点 + `/api/wm/verify` `action="auto"` 自动决策闭环 (96 held-out 窗与已提交评测
   逐位一致)
3. 动作族扩展: 波束级动作 / ~~多小区协同动作~~ **已完成 v2** —
   `scripts/coord_actions.py` 在校准孪生 (per-cell `cell_tx_scale_db` hook) 上生成
   **3125 行多 seed 协同数据集** (5 场景 × 5 seeds × 125 逐小区功率组合,
   `coord_action_dataset.parquet`); 跨 seed 稳健性: 机器人工厂场景 5/5 seeds 协同增益>0
   (mean +0.391±0.355, 中间小区降功率解), 低空 5/5 增益恰为 0, 其余场景以全局动作为主
   (联合==全局 60-100%) — 结论: 协同增益是场景结构性属性, 非单 seed 噪声
   (`coord_seed_robustness.json` + `docs/campus/coord_action_report.json` v2)
