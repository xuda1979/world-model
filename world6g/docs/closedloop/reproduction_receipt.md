# 独立复现回执 (Reproduction Receipt) — 永久记录

- **日期**: 2026-11-06 14:30
- **验证方式**: `git clone /Users/daxu/software/world-model /tmp/world6g_permverify`
  (HEAD = `9a33cab`, 干净工作树, 无未提交文件)
- **管线** (全部在克隆内重跑, 原始数据源重新出发):
  1. `scripts/ingest.py` — 从 6G_data_simulation 源重建 lake.duckdb (ue rows=18,400)
  2. `scripts/generate_twin.py --n-per-scene 8` — 32 twin episodes (校准 ✓)
  3. `scripts/generate_actions.py` — twin_action_dataset.parquet 83,200 行
  4. `scripts/eval_closedloop.py --n-per-scene 24 --seed 2026` — 96 held-out 窗闭环评测
     (使用**已提交**的模型权重 `models/gru_wm_ms.pt` + `models/policy_net.pt`)

## 逐位比对结果

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

## 结论

从提交的 HEAD 出发, 仅依赖 (a) 外部原始数据源
`/Users/daxu/software/6G_data_simulation` 与 (b) 已提交的代码+权重,
闭环评测结果**逐位可复现** (bit-for-bit, 排除时间戳/耗时)。

> 注: 复现命令参数 `--n-per-scene 24 --seed 2026` 来自已提交
> results.json 的 meta (n_per_scene=24, seed=2026); 使用默认参数
> (6/场景, seed 123) 会得到 24 窗的小规模结果, 属预期行为。
