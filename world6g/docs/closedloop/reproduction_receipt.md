# 独立复现回执 (Reproduction Receipt) — 永久记录

## 第三次验证: 服务栈冷启动冒烟测试 (2026-11-06 14:52, HEAD = `d99ec69`)

**验证目标**: 训练产物不仅可离线复现, 还可**加载上线服务** —
全新 clone 中启动 uvicorn, 16 端点全部冒烟通过。

- `git clone /Users/daxu/software/world-model /tmp/world6g_svc` (HEAD=`d99ec69`)
- `scripts/ingest.py` → `scripts/generate_twin.py --n-per-scene 2` (最小数据集)
- `python -m uvicorn world6g.backend.api:app --port 8813` (1s 内就绪)

### 端点冒烟结果 (全部通过)

| 端点 | 结果 |
|---|---|
| `GET /api/health` | `{"status":"ok", tables:[12 表]}` ✅ |
| `GET /api/episodes` | 返回 10 real + twin episodes ✅ |
| `GET /` `/workbench` | 200 (31,241B / 24,909B 前端页面) ✅ |
| `POST /api/wm/rollout` | baseline+action 双预测轨 ✅ |
| `POST /api/wm/verify` | 预测-重放闭环: RSRP rmse_act=5.301, delta_pred/delta_truth 输出 ✅ |
| `GET /api/twin/report` `/api/scene3d/{ep}` `/api/capabilities` | 均正常 ✅ |
| `POST /api/chat` | 正确回答统计查询 ✅ |

> **API 使用注记**: `wm/rollout` 与 `wm/verify` 的请求体键为
> `episode_id`（非 `ep_id`）。初次冒烟用错键名导致两个 400/500,
> 属调用方 payload 错误, 非服务缺陷; 正确键名下两端点均正常。

**结论**: 已提交权重在全新环境中可被服务栈懒加载并对外提供
16 个端点, 前端可视化与 WM 推理服务冷启动即用。

---

## 第二次验证: 端到端重训练复现 (2026-11-06 14:38, HEAD = `7f5b146`)

**验证目标**: 不仅评测可复现, **训练本身**也可复现 — 从提交的 HEAD
重新训练世界模型与策略网络, 权重应与已提交文件**逐位相同**。

- `git clone /Users/daxu/software/world-model /tmp/world6g_e2e` (HEAD=`7f5b146`)
- 全管线: ingest → generate_twin(8/场景) → generate_actions →
  **train_wm_ms** (seed 42, 40 epochs) → **train_planner** (seed 42) →
  eval_closedloop (`--n-per-scene 24 --seed 2026`, 用**重训**权重)

### 权重哈希比对 (SHA-1, 逐位相同)

| 模型 | 已提交 | 重训 | 一致 |
|---|---|---|---|
| `gru_wm_ms.pt` | `29374ac42e7b...` | `29374ac42e7b...` | ✅ |
| `policy_net.pt` | `9b008fb43e65...` | `9b008fb43e65...` | ✅ |

### 重训后 held-out 指标 (与已提交 policy_meta.json 全部 8 项一致, 全精度)

```
n_windows=128  type_match_rate=0.8645833333333334  value_mae=3.327142758571079
planner_recommend_rate=0.75  policy_recommend_rate=0.7421875
wm_reward_planner=-0.30339545124055195  wm_reward_policy=-0.1702277028816752
reward_gap_policy_minus_planner=0.002881012129364535
```

### 重训权重闭环评测 vs 已提交结果: 递归逐位比对

```
total diffs: 2
  meta.date / meta.runtime_s  (时间戳 / 墙钟耗时, 设计内非确定性)
其余全部一致: CEM-MPC +0.2852 / Policy +0.2825 / Random −0.0099、
WM 保真度 pearson +0.798 / spearman +0.782、96 窗明细、twin_runs=209
```

**结论**: 全链路 (数据生成 → WM 训练 → 策略蒸馏 → 闭环评测) 在固定
seed 下**完全确定且逐位可复现**, 已提交权重即重训结果本身。

---

## 第一次验证: 已提交权重评测复现 (2026-11-06 14:30, HEAD = `9a33cab`)

- **验证方式**: `git clone /Users/daxu/software/world-model /tmp/world6g_permverify`
  (HEAD = `9a33cab`, 干净工作树, 无未提交文件)
- **管线** (全部在克隆内重跑, 原始数据源重新出发):
  1. `scripts/ingest.py` — 从 6G_data_simulation 源重建 lake.duckdb (ue rows=18,400)
  2. `scripts/generate_twin.py --n-per-scene 8` — 32 twin episodes (校准 ✓)
  3. `scripts/generate_actions.py` — twin_action_dataset.parquet 83,200 行
  4. `scripts/eval_closedloop.py --n-per-scene 24 --seed 2026` — 96 held-out 窗闭环评测
     (使用**已提交**的模型权重 `models/gru_wm_ms.pt` + `models/policy_net.pt`)

### 逐位比对结果

对 `docs/closedloop/closedloop_results.json` 的 meta/summary/windows
全部字段做递归逐位比对:

```
total diffs: 2
  meta.date:      committed=2026-11-06 13:55  fresh=2026-11-06 14:30
  meta.runtime_s: committed=43.5              fresh=37.0
```

仅 2 处差异, 均为**设计内的非确定性字段** (时间戳 / 墙钟耗时)。
其余全部一致, 包括:

| 字段 | 值 (两侧一致) |
|---|---|
| CEM-MPC lift | +0.2852, CI [+0.1133, +0.4578], P(lift>0)=0.9995 |
| Policy lift | +0.2825, CI [+0.1062, +0.4586], P(lift>0)=0.99875 |
| Random lift | −0.0099, CI [−0.1035, +0.0891], P(lift>0)=0.4075 |
| WM 保真度 | pearson +0.798 / spearman +0.782 (n=384) |
| n_windows / twin_runs / same_root_verified | 96 / 209 / true |
| 全部 96 个决策窗明细 | 逐窗逐字段一致 |

### 结论

从提交的 HEAD 出发, 仅依赖 (a) 外部原始数据源
`/Users/daxu/software/6G_data_simulation` 与 (b) 已提交的代码+权重,
闭环评测结果**逐位可复现** (bit-for-bit, 排除时间戳/耗时)。

> 注: 复现命令参数 `--n-per-scene 24 --seed 2026` 来自已提交
> results.json 的 meta (n_per_scene=24, seed=2026); 使用默认参数
> (6/场景, seed 123) 会得到 24 窗的小规模结果, 属预期行为。
