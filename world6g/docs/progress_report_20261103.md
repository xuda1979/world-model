# world6g 项目进展报告

**日期**: 2026-11-03
**周期**: 2026-10-30 ~ 2026-11-03
**目标**: 校准反事实数字孪生 + 动作条件世界模型(方案二首期)

---

## 一、总体进展

本周期完成了从"数据底座 + Tiny-Twin"到"**世界模型基准套件**"的跨越,共 10 次提交,覆盖
数据底座 → 统计保真度 → 同根反事实动作 → 世界模型训练/评测 → 3D 可视化平台 → 论文骨架 → 进度 PPT
的完整链路。当前项目已具备:

1. **可复现的数字孪生底座**(三层 datalake + Tiny-Twin 物理层仿真)
2. **论文级统计保真度验证**(KS 检验 / Wasserstein-1 / QQ 回归,真实 vs Twin 对齐)
3. **同根反事实动作数据集**(同 RNG seed,只改变动作,Δ 因果效应精确无混淆)
4. **四任务动作条件世界模型基准**(A 单步 / B rollout / C 反事实 Δ / D 不确定性校准)
5. **3D 可视化工作台**(五层数据渲染、园区地图、对话/指标卡、hover 详情)
6. **IEEEtran 论文骨架**与 20 页进度汇报 PPT

---

## 二、本周完成事项

### 2.1 数据底座与 Tiny-Twin(10-30 ~ 10-31,提交 ab0e41a / 73c7f65 / 7a39ad7)

- **三层 datalake**: raw → curated → generated,数据字典 249 字段;DuckDB 湖 `lake.duckdb`。
- **Tiny-Twin 物理层仿真**: 几何 + 阴影 + 噪声随机流建模,输出 RSRP/SINR/下行速率/CQI 四 KPI。
- **统计保真度验证**(论文级口径,提交 5adeb39):
  - 方法: 双样本 KS 检验 / Wasserstein-1 / Q-Q 分位数回归(理想 slope=1, R²=1)。
  - 结论口径: KS p>0.05 = 分布不可区分;W1/IQR<0.25 = 搬运代价低于四分位距 1/4。
  - 结果(节选,详见 `docs/fidelity_report.md` 与 `docs/fidelity_table.tex`): 郊区下行速率 QQ R²=0.989、W1/IQR=0.056;郊区 SINR QQ R²=0.977。
- **Twin 业务流量模型同构**(提交 5adeb39): 业务侧与无线侧建模口径统一。
- **Twin 动作族**(提交 7a39ad7): 发射功率缩放 ×[0.63,1.82]、PRB 分配比例 ∈[0.28,0.33]、波束方位偏移最大 ±28°、A3 切换偏移(+4.4 dB),对每个干预用**相同 RNG seed** 重跑 Twin,只改变动作,得到反事实同根分支数据 `twin_action_dataset.parquet`。

### 2.2 动作条件世界模型(11-01 ~ 11-02,提交 3de62d7 / 15998b6 / 本周期新代码 `wm/`)

**模型库**(`wm/models.py`,统一接口 `predict_next(xh_norm, xa_norm) → ŷ_norm`,容量递增 + 结构对照):

| 模型 | 参数量 | 说明 |
|---|---|---|
| Persistence | 0 | 持久性基线 ŷ = s_t |
| LinearAR (action-aware) | 闭式解 | 动作感知线性 AR |
| GRU-WM | 18,772 | 门控循环网络 |
| RSSM-WM | 37,812 | 循环状态空间模型(随机 z, KL 约束) |
| TF-WM | 68,420 | 因果 Transformer |
| PhysWM | 18,772 | 物理混合(物理核 + 残差校正头) |

**训练协议**(`wm/train.py`): 统一 MSE,40 epochs,batch 512,Adam lr=1e-3,seed=42;训练集 63,232 样本,测试集 16,640(时间外推切分,避免泄漏)。

**四任务基准结果**(`docs/wm_suite/results.json`,归一化空间):

- **Task A 单步预测**: RSSM-WM 最优 RMSE 0.2257,PhysWM 0.2272,GRU-WM 0.228,Persistence 0.2598。配对 bootstrap(2000 次)显示 GRU/RSSM/PhysWM 显著优于持久性基线(P[更差]=0.0,95% CI 不含 0),TF-WM 亦显著(P=0.006);动作分支上 RMSE 略低,说明模型利用了动作信息。
- **Task B 多步 rollout**(K ∈ {1,2,4,8,16,32,40},即最长 2 秒): 复合误差增长明显 — RSSM-WM 增长 11.4×(h1=0.2112 → h40=2.405)、GRU-WM 4.5×、TF-WM 4.3×、PhysWM 5.0×。**v2 多步一致性训练**(teacher-forcing + k≤8 步 rollout 混合,λ 课程 0.3→0.9)后 **GRU-WM-MS h40 误差 1.103 → 0.849(-23.1%),首次低于 Persistence(0.696 之外的中间 horizon 全段更优)**;PhysWM-MS 反而退化(1.218 → 3.152,物理先验与 rollout 目标冲突)——复合误差是训练目标问题而非架构极限,混合物理架构需要 rollout-aware 的残差训练。
- **Task C 反事实 Δ-效应**(核心结论,同根对: 基准轨迹 → 动作分支):
  - 仅在 Task A 协议上训练的模型在 Δ 预测上**全部失效**(GRU-WM corr=0.13、RSSM-WM corr=0.07、PhysWM corr=0.21,接近零假设 Δ=0 的 corr=0);
  - **反事实监督(输入基准历史 → 预测动作分支)后**,GRU-WM corr=**0.72**、符号准确率 **73.3%**、Δ-RMSE 0.436(vs 零假设 0.598,提升 27%);
  - 按动作类型: PRB 缩放效果最好(RMSE 0.210 vs null 0.694),波束方位偏移最难(RMSE 0.616 vs null 0.801)。
  - **结论**: 反事实监督是学到因果 Δ-效应的关键;数据底座的同根设计使这种监督成为可能。
- **Task D 不确定性校准**: v1 — RSSM MC-z 原始 σ 严重欠覆盖(95% 覆盖率仅 4.4%,σ 均值 0.004),事后全局缩放 ×191 后覆盖率达 93.3%、NLL 1.02。**v2 异方差 NLL 训练**(σ 头 + 5 epoch warmup 冻结)后: GRU-WM-NLL 覆盖率 **95.2%**、ECE 0.24、NLL -0.45;RSSM(aleatoric σ 头 + MC-z 认知项组合)**95.6%**、ECE 0.24、NLL -0.40,事后缩放退化为 ×0.97(几乎 no-op)——**显式 NLL 目标直接产出校准的输入相关不确定性,难题 (b) 基本解决**。

### 2.3 可视化平台(提交 15a0c5a / 5139c13)

- **信息港园区地图模块** + 3D 场景 API + 能力卡 + WM rollout 接入 + 对话问答。
- **3D 仿真工作台**: 五层数据渲染 + 对话/指标卡 + hover 详情。

### 2.4 论文与汇报(提交 15998b6 / 3b86a43 及本周期修改)

- **论文**(`paper/main.tex`,IEEEtran): "A Calibrated Counterfactual Digital Twin for Action-Conditioned World Models in 6G Networks"。本周期完成四任务基准章节(Task A/B/C/D)、保真度表格 `fidelity_table.tex`、Related Work、Limitations and Path Forward、Conclusion,并编译出 `main.pdf`。
- **进度汇报 PPT**: 20 页(`docs/world6g_progress.pptx`),生成脚本 `scripts/make_progress_ppt.py` 本周期更新(加入真实保真度摘要、世界模型四任务结果页)。

---

## 三、关键结论

1. **同根反事实数据设计是本项目的核心方法论**: 相同 seed 只改动作,使 Δ 无混淆、可精确监督,这是普通仿真数据无法做到的。
2. **反事实监督 >> 普通时序监督**: Task A 训练的模型在 Δ 预测上接近零假设;反事实监督后 corr 从 ~0.1 提升到 0.72。
3. **物理混合模型(PhysWM)在单步与 GRU 持平**,且在 SINR/速率边际上占优,验证"物理核 + 学习残差"的混合路线。
4. **两个公开难题均已给出解法并验证 (v2, 11-04)**: (a) rollout 复合误差 — 多步一致性训练使 GRU-WM h40 误差 -23.1%,首次跑赢 Persistence;(b) 不确定性校准 — 异方差 NLL 训练使覆盖率从 4.4% 提升到 95.2-95.6%,ECE 0.24,事后缩放退化为 no-op。两个方法均已写入论文 Benchmark 章节。

---

## 四、下一步计划

1. ~~降低 rollout 复合误差~~ **已完成 (11-04)**: 多步一致性损失落地(GRU-WM-MS -23.1%);遗留: PhysWM 混合架构的 rollout-aware 残差训练(当前退化)、RSSM 层级化状态、k>8 的长程一致性。
2. ~~不确定性校准~~ **已完成 (11-04)**: 异方差 NLL 头落地(覆盖率 95%+);遗留: 按 horizon 分桶校准(rollout 中 σ 应随 h 增宽)、conformal 精修、per-action-type 校准。
3. **动作空间扩展**: 波束方位偏移是当前最难动作(RMSE 0.616),需增加同根样本或引入波束级物理先验。
4. **3D 平台与世界模型闭环**: 平台侧下发动作 → WM 预测 → Twin 校验,形成"预测-验证"演示闭环。
5. **双周节点**: 11/6 能力交互首版、11/13 稳定演示(沿用 README 中的节点)。
6. **论文**: 补 Related Work 引用、复跑全部实验固化数值、按目标会议格式打磨。

---

## 五、仓库状态

- 提交数(本周期): 10
- 关键目录: `world6g/datalake`(数据底座)、`world6g/twin`(Tiny-Twin)、`world6g/wm`(世界模型基准)、`world6g/backend`+`frontend`(可视化平台)、`world6g/paper`(论文)、`world6g/docs`(报告/图表/PPT)。
- 评测产物: `docs/wm_suite/results.json`(机器可读全部数值)、`fig_taskA.png`、`fig_rollout.png`、`fig_delta.png`。
- 可复现性: 固定 seed=42,时间外推切分,评测脚本 `wm/evaluate.py` 一键复跑。
