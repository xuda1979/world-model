# 启动手册 (v0.2, 2026-11-12)

- **对应交付**: 方案一 §7 "启动手册 + 10分钟演示脚本 + 备用录像" (演示脚本另见 `docs/demo_script_v02.md`, 冻结 2026-11-12)
- **适用**: 全新机器从零开始复现数据湖 → Tiny-Twin → 校准 → 时序 WM/策略 → 演示服务全链路
- **版本锁**: 依赖锁定文件 `world6g/requirements.lock` (100 个精确 pin, `pip freeze` 生成)

## 0. 目录约定

仓库根 = `/Users/daxu/software/world-model` (或克隆后任意根), 以下命令均从仓库根执行。
`world6g/` 下: `datalake/` (raw/curated/generated 三层)、`scripts/` (33 个脚本)、`models/` (权重)、`backend/api.py` (FastAPI)、`frontend/workbench.html` (演示工作台)、`tests/`。

## 1. 安装 (首次)

```bash
uv venv .venv --python 3.12
.venv/bin/pip install duckdb pandas pyarrow fastapi "uvicorn[standard]" plotly torch scikit-learn
# 完整锁文件 (可选, 精确复现): .venv/bin/pip install -r world6g/requirements.lock
```

## 2. 数据流水线 (按序)

```bash
# ① 真实数据入湖
.venv/bin/python world6g/scripts/ingest.py            # datalake/raw → curated, 源指纹写 registry.json
# ② Tiny-Twin 生成 (TinyTwin 引擎: 38.901 路损 + AR(1) 阴影 + 扇区增益, 参数由数据湖校准; 快速冒烟 2 场景/组)
.venv/bin/python world6g/scripts/generate_twin.py --n-per-scene 2
# ③ 反事实动作分支 (策略标签)
.venv/bin/python world6g/scripts/generate_actions.py # --n-actions 12 默认
# ④ 时序 tick 数据集 (WM/策略训练样本)
.venv/bin/python world6g/scripts/gen_coord_seqs.py
```

## 3. 训练 (全模型栈)

```bash
# 基线世界模型 (GRU-WM) + 多步一致性变体
.venv/bin/python world6g/scripts/train_world_model.py
.venv/bin/python world6g/scripts/train_wm_ms.py
# 校准链路 (sim_calib → train_calibrated)
.venv/bin/python world6g/scripts/sim_calib.py && .venv/bin/python world6g/scripts/train_calibrated.py
# 时序 WM ensemble + 时序协同策略 (M11 主线)
.venv/bin/python world6g/scripts/train_coord_seq_policy.py
# 策略蒸馏 (CEM-MPC / 融合)
.venv/bin/python world6g/scripts/train_planner.py && .venv/bin/python world6g/scripts/train_planner_fused.py
.venv/bin/python world6g/scripts/train_policy_calib.py && .venv/bin/python world6g/scripts/train_coord_policy.py
```

## 4. 评估与探针 (零手抄断言)

```bash
# GBM 5 折 LOSO 特征天花板探针 → wm_ceiling_probe.json
.venv/bin/python world6g/scripts/probe_wm_ceiling.py
# 闭环评测 (真实) / 校准 / 融合
.venv/bin/python world6g/scripts/eval_closedloop.py --n-per-scene 24 --seed 2026
.venv/bin/python world6g/scripts/eval_closedloop_calib.py
.venv/bin/python world6g/scripts/eval_closedloop_fused.py
# 双守护探针 (技术报告 72/72 + 周报 36/36)
.venv/bin/python world6g/scripts/probe_report_v3.py
.venv/bin/python world6g/scripts/probe_progress_1111.py
# 测试套件 (64 个护栏测试)
.venv/bin/python -m pytest world6g/tests/ -q
```

## 5. 演示服务 (演示日前置)

```bash
# 方式 A (推荐, 演示脚本载入口): 仓库根直启 (venv 解释器含 fastapi/plotly 全栈)
.venv/bin/python3 world6g/backend/api.py        # → http://127.0.0.1:8811 → /workbench
# 方式 B (uvicorn reload 可选)
.venv/bin/python -m uvicorn world6g.backend.api:app --port 8811
```

页面: `http://127.0.0.1:8811/workbench`。5 层 3D 场景 (环境/设备/传播/业务/KPI)、协同策略面板、时序 Tick 回放面板、数据湖问答、CSV/JSON 导出按钮。

## 6. 演示日依赖就绪自检 (13 日按 demo_script_v02.md 打勾前)

- [ ] `datalake/generated/coord_seq_dataset.parquet` (9,000 行) / `coord_action_dataset.parquet` (3,125) / `twin_{01..08}_*` / `wm_ceiling_probe.json` 均存在
- [ ] `models/coord_seq_*.pt` + meta (WM ensemble 3 × 142,347 / policy 34,685 / coord_policy 613,874) 就位, 无 `.pt` 缺失
- [ ] 服务启动无 500: 调 `GET /api/coord/ticks?scene=campus&seed=9000&ep_i=0` 返回 10 行; 非法入参返回 422 (非 500)
- [ ] 导出: `GET /api/coord/ticks/export?scene=campus&seed=9000&ep_i=0` 返回 `coord_ticks_campus_s9000_ep0.csv` (RFC 5987 中文文件名回退)
- [ ] 冻结数字对照: Σreward = 4.7318; tick8 reward = +3.065; win R²=0.8978 / reward R²=0.6211 (见 demo_script_v02.md)
- [ ] 离线预案: 无外网时问答走本地规则引擎, 3D 用 Plotly 本地渲染, 数据全在 parquet

## 7. 异常排查速查

| 现象 | 检查 |
|---|---|
| `ingest.py` 找不到源 | `datalake/raw/registry.json` 的 source_root 指向 6G_episode_data/6G_episodes_10s_fullcatalog |
| twin 校验失败 | `generate_twin.py --n-per-scene N` 的 N 影响行数, 先冒烟 N=2 |
| 演示页 3D 不渲染 | Plotly 版本锁 7.x; 前端无外网 CDN 依赖 (本地 plotly.min.js) |
| API 422/500 行为不符 | 对照 `tests/test_wm_api.py` 护栏语义 (切片过滤 + 导出同源) |
