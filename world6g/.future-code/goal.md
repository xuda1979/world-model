# Goal: 训练完成世界模型 + 规划器（动作模型），数据由仿真器/模拟器产生

Status: completed

完成时间: 2026-11-06
独立验证: 干净 clone 全管线重跑（ingest → generate_twin → generate_actions →
eval_closedloop），结果与主仓**逐位一致**（CEM-MPC lift +0.285172 = +0.285172，
WM 保真度 pearson 0.797795 = 0.797795）。

复现回执永久化 (2026-11-06 14:30, 第二次独立复现):
- 从 HEAD 9a33cab 新 clone → ingest → twin(8/场景) → actions(83,200 行) →
  eval(--n-per-scene 24 --seed 2026, 已提交权重), 与已提交
  closedloop_results.json 全字段递归逐位比对: 仅 2 处差异且均为设计内
  非确定性字段 (meta.date 时间戳 / meta.runtime_s 墙钟耗时);
  其余全部一致 (含 96 窗明细、twin_runs=209、same_root_verified=true)。
- 回执文件: docs/closedloop/reproduction_receipt.md (已入库)

## Checklist — 全部完成，逐项证据
- [x] 盘点现状：WM v2（models/gru_wm_ms.pt + meta）、训练脚本（train_wm_ms.py /
      train_world_model.py）、仿真器管线（twin/engine.py + generate_twin.py +
      generate_actions.py 同根反事实分支）、基准套件（wm/ 四任务 A/B/C/D）
- [x] 验证世界模型 v2 评测结果：h40 rollout RMSE_norm 0.849（单步基线 1.103，
      复合误差↓23%）；异方差 NLL 校准 PICP 覆盖达标（docs/wm_suite/results.json）
- [x] 审计数据管线覆盖度：4 场景 × 8 UE × 5 动作类型，83,200 行反事实分支，
      同根设计保证 Δs 因果无混淆；held-out 段 t≥8000ms 时间外推切分
- [x] 训练世界模型 v2：多步一致性训练（λ·rollout_mse + (1−λ)·teacher_forcing，
      λ 0.3→0.9），18,772 参数，seed 42（models/gru_wm_ms.pt）
- [x] 训练规划器/动作模型：CEM-MPC 在线规划器（wm/planner.py）+ 策略蒸馏
      （models/policy_net.pt，60 epochs，type_match 86.5%，val_type_acc 0.908，
      WM 内奖励 gap 仅 +0.003 vs 规划器）
- [x] 仿真器闭环评测（96 held-out 窗，动作真实施加到 Tiny-Twin 同根分支）：
      CEM-MPC lift +0.285 [95%CI +0.113,+0.458] P>0=0.9995；
      Policy +0.283 [+0.106,+0.459] P>0=0.9988；
      Random 阴性对照 −0.010（CI 跨 0，P>0=0.407）→ 显著弱于规划器 ✓
      （docs/closedloop/closedloop_results.json + closedloop_report.md）
- [x] 保存模型工件与评测契约：models/（WM+policy+meta）、scripts/eval_closedloop.py
      （固定种子可复现，同根校验 assert）、docs/closedloop/ 完整结果
- [x] 独立复核：干净 clone 端到端重跑逐位一致；wm_suite 基准复跑生产模型指标一致
      （GRU-WM-MS rmse_h40 0.848 vs 0.849，第 3 位小数内）；负控制显著弱于规划器 ✓
- [x] Status: completed（所有独立检查通过）

## Notes
- 仓库：/Users/daxu/software/world-model，工作目录 world6g/
- 提交链：c21102e（闭环评测）→ bd8a712（基准复跑）→ 34d440d（规划器/训练器/
  模型权重入库）→ 782b845（服务栈 WM v2 + /wm/verify）→ 9a33cab（报告）
- 关键架构：数据由 Tiny-Twin 仿真器产生（ingest 真实数据校准 → generate_twin
  仿真 → generate_actions 同根反事实分支）→ WM v2 训练 → CEM-MPC 规划 +
  策略蒸馏 → 闭环评测（同根校验 + bootstrap CI + 阴性对照）
- WM 决策保真度：Spearman +0.78（n=384），排序能力支撑真实决策
- 动作分布：学习策略 74% 选择 az_offset（波束方位角），21-23% 选 none（不过度干预）
