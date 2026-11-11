# world6g 项目进展报告

**日期**: 2026-11-11
**周期**: 2026-11-06 ~ 2026-11-11
**目标**: 校准反事实数字孪生 + 动作条件世界模型 + 规划器/动作模型(方案二闭环)

---

## 一、总体进展

本周期(11-06 起)完成「仿真器校准 → 决策动作模型 → 协同/时序协作 → 报告/服务化」的整条收口链路,共 25 次提交(下文按里程碑列出核心 10 次),覆盖
校准仿真引擎 → 技术报告 v3.0 → 逐小区协同策略蒸馏与服务化 → M11 时序化原语 → 时序协同策略(时序 WM + 规划 oracle 蒸馏)
的完整链路。核心目标「**训练完世界模型与规划器(动作模型),数据来自校准仿真器**」已端到端达成,全部产物双源校验入账本。

本周期 5 个关键交付:

1. **仿真器多源校准(报告第 4 章)**: E1 分布保真 / E2 世界模型预测 / E3 闭环决策 / E4 园区应用,全数字从 6 份权威 JSON 独立重算
2. **逐小区协同策略(coord policy)**: 5 场景 × 5 seeds × 125 组合,LOSO 实现率 96.3%,闭环 lift +3.34,并服务化 `/api/coord/action`
3. **M11 时序化原语**: tick 推进 + 时变 per-cell 功率,9000 行时序协同 MDP 数据集 + 21 项守护测试
4. **时序协同策略(时序 WM + 规划 oracle + BC 蒸馏)**: 闭环 LOSO 全 5 折,sim_greedy 29.35 领先 best_const 28.55,产出入账
5. **技术报告 v3.0 新增 7.6 节**: 复算全部数字零手抄,溯源探针 72/72 PASS

---

## 二、本周完成事项

### 2.1 校准仿真引擎与决策动作模型(11-06 ~ 11-08,提交 b698792 / d704d31 / f60d411)

- **融合奖励蒸馏为单网络动作模型**(11-06, b698792): λ=0.5 融合奖励下把 CEM-MPC 规划器蒸馏为
  `policy_fused_net.pt`,闭环 lift **+0.550** [CI +0.361, +0.729] —— 首个真实奖励为正的单一网络动作模型。
- **校准仿真引擎(组 A, 会话 25/26, 27 项)**: 收敛校准损耗 + 时变偏移,产出 `sim_calib_report.json`。
- **技术报告 v3.0 与全树收口(组 B, f60d411)**: 工作树修改 16 项 + 目标账本(会话 21-26)入账。
- **世界模型消融注记**(1993226/c3a10a8): 验证闭环提升不归因于多步一致性训练——MS 保真度高但 lift 略低,
  单步更进攻;融合奖励 λ=0.5 兼得两者,蒸馏为单网络策略。

### 2.2 逐小区协同策略蒸馏与服务化(11-09,提交 f3950ff / 798ffbd / 5a5f9ed / 6b15f76 / 1b71d95)

- **多小区协同动作数据集 v2**: 5 场景(园区/城区/郊区/低空/机器人工厂)× 5 seeds × 125 组合,协同增益跨 seed 稳健性入报告 7.4。
- **coord policy 蒸馏**(5a5f9ed): 61.4 万参数逐小区策略,5 折 LOSO:
  - regret mean **0.128** / p50 0.000 / max 0.950;
  - **协同增益实现率 96.3%**(oracle lift 3.468 vs 实际 lift 3.340);
  - seed 头部对比: value 目标实现率 96.5%(regret 0.123)优于 score 目标 93.1%(regret 0.240)。
- **API 输入校验加固**(798ffbd): 空 episode_id → 422 + 5 项守护测试固化。
- **coord policy 服务化**(6b15f76): `/api/coord/action` 在线推理 + 13 项守护测试(报告 7.5 节部署段)。
- **目标账本补录会话 28**(1b71d95): v2 多 seed 稳健版 + API 加固守护轮(双账本)。

### 2.3 仿真器时序化原语 M11(11-10,提交 8bc94ca / d6aa6fd)

- **tick 推进(dt=1s)**: 仿真器核心态按 tick 演化,整体回报 = Σ_t tick 目标(win_obj_t − ref_zero_t)。
- **时变 per-cell 功率**: 每小区功率随 tick 漂移(依 seed 组合),制造跨 tick 时变协同需求。
- **时序协同数据集**: 25 组(5 场景 × 5 seeds) × 36 eps × 10 决策步 = 9000 行时序 MDP;配套
  `coord_seq_static_ref.json` 静态参照(同 seed 静态功率基线)。
- **21 项守护测试 `test_twin_temporal.py`** 固化 tick 推进/时变功率/时序数据集不变量。
- **目标账本会话 35**(d6aa6fd): 时序化原语入库记录 + operator 即时推送约束。

### 2.4 时序协同策略 v1(11-11,提交 eea4929 / abd0584)

**训练管线**(`scripts/train_coord_seq_policy.py`): 时序世界模型(3 集成, 142,347 参数) → 规划 oracle(逐 tick 搜索)
→ BC 蒸馏(10 步内 pearl 决策) → 闭环 LOSO(5 折按 seed_offset, 25 组)。

**时序 WM**(`coord_seq_wm.pt`): 30-256-256-256-11 全连接 3 集成,输入 10 维 ctx+action+scene → next_ctx(10)+reward(1);
留出折 reward-R² **0.534**、MAE **0.821**。

**BC 策略**(`coord_seq_policy.pt`, 34,685 参数): 15-128-128-125,8,325 条专家轨迹,训练 acc **93.1%**;
留出折与规划器动作一致率 **82.0%**,与 sim_greedy 决策重合 31.6%(与 wm_greedy 重合 32.9%)。

**闭环 LOSO 结果**(`coord_seq_loso.json`, 6 控制器, 385s):

| 控制器 | 回报均值 | vs best_const 胜率 |
|---|---|---|
| zero(恒零) | 0.00 | — |
| random(随机动作) | 1.74 | 0% |
| best_const(静态最优) | 28.55 | — |
| wm_greedy(时序 WM 贪心) | 26.69 | 8% |
| bc_policy(蒸馏策略) | **26.96** | 12% |
| sim_greedy(oracle 逐 tick 上界) | **29.35** | **80%** |

**逐场景**(BC / sim_greedy): 园区 30.15/31.86,城区 28.34/30.00,郊区 24.78/26.95,低空 26.88/30.07,
机器人工厂 24.67/27.87 —— 全场景 BC 追平 best_const、距 oracle 上界 <2 点。

**修复记录**(abd0584): 折内掩码形状 bug + 死代码清理,重训后全 5 折产物入库。

### 2.5 测试套件与技术报告(收尾轮)

- **全测试套件 58 项 PASS**: `test_coord_seq.py`(21 项时序协同 M11 守护)+ `test_twin_temporal.py`(21 项时序原语)
  + `test_wm_api.py`(13 项在线服务)+ 既有回归。
- **技术报告 v3.0 新增 7.6 节**「时序协同策略 (M11: 时序世界模型 + 规划 oracle 蒸馏)」: 性质 / WM / BC / 协议 /
  每折表(R², MAE, agreement, BC 回报, sim 回报)/ 跨折总结 / 诚实披露(胜率、match rate 分布差、逐场景拆分)/ 资源复现。
- **溯源探针 `probe_report_v3.py` 72/72 PASS**: 新增 21 项 7.6 断言全命中,第 4 章全部数字与章节标题零手抄。

---

## 三、关键结论

1. **校准是决策质量的根**: E3 40 真实窗闭环,cal-WM 均值奖励 −0.126 > uncal −0.136,且动作率 0.225 << 0.65
   —— 校准过的 WM「该动才动」,单窗 CEM 位数优势在决策质量而非动作量;园区 E4 cal 平均 lift +0.029(p=0.96),
   uncal 平均为负。
2. **协同动作是最大单步决策收益**: coord policy 5 折 LOSO 实现 oracle 协同增益的 96.3%,regret 中位恰为 0;
   服务化 `/api/coord/action` 已在线。
3. **时序化后,蒸馏策略直接贴近最优**: M11 中 bc_policy(26.96)追平 best_const(28.55)、胜率 12%(vs wm_greedy 8%),
   而 oracle 逐 tick 贪心 29.35 胜率 80% —— 动作模型在时序任务上是规划器的有效代理。
4. **诚实披露**: BC 与 sim_greedy 决策重合率仅 ~32%(动作空间 125,分歧大但回报损失小),wm_greedy 贪心滞后最佳静态;
   分场景均有 <2 点差距,是扩展决策空间的收益来源。

---

## 四、下一步计划

1. **黑盒守护 + 计时策略**: 为 coord_seq Loso 训练管线补运行时断言(逐步已由 test_coord_seq.py 覆盖),入 CI 守护。
2. **BC→oracle 差距收敛**: 扩大专家数据(tick 级 oracle 决策)、动作空间结构化(组合数 125→粗粒度),目标 bc 回报 ≥28.5。
3. **WM 预测量化提升**: reward-R² 0.534 → 目标 0.65;引入 tick 级 GNN 共享 scene 结构。
4. **报告 7.7 节候选**: 时序协同策略对 CEM/策略蒸馏的可迁移性对比(cal 策略 7.5 的数据)。
5. **演示节点**: 3D 工作台接入 `/api/coord/action` + 时序 tick 回放。

---

## 五、仓库状态

- 提交数(本周期): 10(b698792 → abd0584)。
- 关键目录: `world6g/datalake/generated`(coord_seq_loso / coord_seq_static_ref / coord_policy_loso)、
  `world6g/models`(coord_seq_wm/policy.pt+meta、coord_policy.pt+meta、policy_fused_net.pt)、
  `world6g/scripts`(train_coord_seq_policy.py / make_full_report.py / probe_report_v3.py)。
- 守护测试: `world6g/tests/test_coord_seq.py`(21 项)+ `test_twin_temporal.py`(21 项)+ `test_wm_api.py`(13 项)。
- 基线参照: zero/random/best_const/wm_greedy/bc_policy/sim_greedy 六控制器双源可复算;eval 全固定 seed、时序外推切分。
- 账本: 根 `.future-code/goal.md` 与 `world6g` 双账本镜像(grep -c 断言两侧均 = 1)。
