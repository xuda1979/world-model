# 6G 数据底座与可视化 (Q4 方案一) + Tiny-Twin 生成仿真数据

基于两份 Q4 执行方案 (数据底座与可视化 / 世界模型训练) 搭建:
**优先数据底座和可视化展示, Tiny-Twin 拟合真实数据生成仿真数据**。

## 架构

```
world-model/
├── Q4执行方案一_数据底座与可视化_20261030.docx   # 源方案
├── Q4执行方案二_世界模型训练与能力演示_20261030.docx
└── world6g/
    ├── datalake/          # 数据底座 (三层: raw → curated → generated)
    │   ├── config.py      # 路径/物理常量/真实锚定统计 (单一事实来源)
    │   ├── raw/registry.json       # 源数据指纹 + 真实 MRO 锚定 (可追溯)
    │   ├── curated/*.parquet       # 10 episodes 真实数据 (11 表, 249 字段)
    │   ├── generated/              # Tiny-Twin 生成数据 (与 curated 同 schema)
    │   └── lake.duckdb             # DuckDB 查询层
    ├── twin/engine.py     # Tiny-Twin 数字孪生引擎 (参数从数据湖拟合校准)
    ├── scripts/
    │   ├── ingest.py      # ETL: 10 episodes 真实数据 → Parquet + DuckDB
    │   └── generate_twin.py  # 批量生成 + 校准报告 (生成值 vs 真实值域)
    ├── backend/api.py     # FastAPI (8 端点, 线程安全 DuckDB)
    └── frontend/index.html  # 可视化工作台 (Plotly 单页)
```

## 快速开始

```bash
uv venv .venv --python 3.12 && .venv/bin/pip install duckdb pandas pyarrow fastapi "uvicorn[standard]" plotly

# 1. 数据入湖 (10 episodes 真实数据 → Parquet + DuckDB)
.venv/bin/python world6g/scripts/ingest.py

# 2. Tiny-Twin 生成仿真数据 (4 场景 × 2, 自动校准报告)
.venv/bin/python world6g/scripts/generate_twin.py --n-per-scene 2

# 3. 启动可视化 (http://127.0.0.1:8811)
.venv/bin/python -m uvicorn world6g.backend.api:app --port 8811
```

## Tiny-Twin 校准方法 (拟合真实数据)

不复制原生成器代码, 而是将其物理链路作为**可校准参数结构**, 全部参数从数据湖拟合:

| 模块 | 结构 | 校准来源 (数据湖) |
|------|------|------------------|
| 路损 | 3GPP 38.901 UMa/UMi NLOS | (RSRP, 3D 距离) 回归: 结构模型残差 sd=7.1dB |
| 阴影 | AR(1), ρ=exp(−v·dt/d_cor) | 残差自相关 → d_cor≈37.7m, σ=6.0dB (城区) |
| 扇区增益 | 主瓣 17dBi 二次衰减, FB 30dB | 服务扇区方位差分布 (主瓣命中 53.2%) |
| SSB-SINR | S/(0.04·I+N)+BF5+噪声 | 完整公式验证: 残差 sd=1.50dB (=高斯噪声项) |
| CQI | SINR 边界查表 | 湖中 CQI-SINR 边界 (15 级) |
| MRO 编码 | RSRP=值×0.5−156 等 | 数据字典 25 字段编码规则 |
| 运动 | 恒速直线+场景速度 | 每场景速度分布 (城区 16.7 / 低空 83.3 m/s) |
| UE 几何 | 初距 U(160,212)m | 湖中真实初距分布 (均值 180-194m) |
| 站点工参 | 真实网格6 站点 | site 表: 高度/方位/PCI/ARFCN 按场景匹配 |

**全局真实锚定** (96 文件 338 万 MRO 样本): RSRP −118.0dBm / SINR 12.07dB / RSRQ −10.10dB。

### 校准结果 (8/8 通过)

| 场景 | Twin RSRP | 真实值域 |
|------|-----------|----------|
| 城区 | −107.9 / −100.5 | [−121.8, −105.9] |
| 郊区 | −113.0 / −116.4 | [−117.7, −106.8] |
| 低空 | −128.8 / −131.1 | [−124.0, −122.0] |
| 机器人工厂 | −100.9 / −113.4 | [−110.7, −107.6] |

## 可视化功能 (http://127.0.0.1:8811)

- **Episode 侧栏**: 18 个 (10 真实 + 8 Twin), 按来源着色
- **KPI 卡片**: RSRP / SINR / 下行速率 / 场景
- **KPI 时序**: 7 指标切换 (RSRP/SINR/DL/UL/时延/CQI/丢包率), 均值±P10/P90 带 + 每 UE 曲线
- **网络地图**: UE 轨迹 (颜色=RSRP Turbo 色阶) + 站点三扇区覆盖
- **小区吞吐**: 每小区 Mbps 时序
- **全 Episode 对比**: RSRP×SINR 散点 (真实圆点 vs Twin 菱形)
- **Twin 校准**: 生成值 vs 真实值域 (误差条)

## API 端点

| 端点 | 说明 |
|------|------|
| `GET /api/episodes` | 18 episodes 清单 (真实+Twin) |
| `GET /api/kpi/{id}?metric=` | KPI 时序 (7 指标) |
| `GET /api/map/{id}` | UE 轨迹 + 站点 |
| `GET /api/compare` | 全 episode 对比 |
| `GET /api/fields` | 数据字典 249 字段 |
| `GET /api/twin/report` | Twin 校准报告 |

## 下一步 (对接方案二 世界模型)

1. 双周节点: 11/6 能力交互首版, 11/13 稳定演示
2. Radio map 重构 / 新站预测 / KPI 预测等 5 项能力接入本平台
3. Twin 扩展: 多小区干扰场景、动作族 (功率/PRB/波束) 执行与前后状态对比
