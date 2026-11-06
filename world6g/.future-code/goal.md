# Goal: 训练完成世界模型 + 规划器（动作模型），数据由仿真器/模拟器产生

Status: in_progress
推进日志 2026-11-06 16:20 (会话 3): 消融实验已完成并提交 (c3a10a8, 1993226),
发现闭环提升不归因于多步一致性, 单步更进取 (lift 0.456 vs 0.285) 但保真度低 (0.390 vs 0.688)。
下一步: 融合奖励实验 λ·MS + (1−λ)·单步 检验能否兼得两者优势 (已在 ablation_report 明示改进路径)。

推进日志 2026-11-06 17:10 (会话 3 续): 融合奖励实验完成并验证 (fuse_wm_planner.py,
96 窗 × 5 λ, twin 90 次, docs/closedloop/fuse_wm_planner.json)。
结果: λ=0.5 → lift +0.469 [CI +0.299,+0.634] P>0=1.000 且保真度 pearson 0.651,
兼得单步进攻性与多步保真度 (纯 MS +0.269/0.706, 纯 v2 +0.404/0.471)。
三个改进路径假设都被实验证实: 消融诚实归因 + 融合奖励可操作,
融合奖励进一步蒸馏为**单网络动作模型**并闭环验证。

推进日志 2026-11-06 16:50 (会话 3 续 2): 融合蒸馏部署化完成 — train_planner_fused.py
把 λ=0.5 融合专家 (双 WM 在线 CEM) 蒸馏为单 PolicyNet (29,577 参数, 512 专家窗,
val_type_acc 0.868, WM 内 reward gap 仅 +0.005)。
闭环评测 (eval_closedloop_fused.py, 96 held-out 窗 × 5 策略, 同根 twin, 种子 2026):
**Policy-Fused lift +0.550 [CI +0.361,+0.729] P>0=1.000** — 逐一窗口超过在线
融合规划器自身 (+0.469) 与生产 Policy (+0.283)/CEM-MPC (+0.285),
动作率 0.77 保持 MS 式保守 (az 74/none 22), 是首个真实奖励均值为正的策略
(+0.119 vs NoAction −0.432)。内置一致性校验逐位复现已提交基准 (CEM +0.2852,
Policy +0.2825), 同根校验 ✓。部署形态: 单前馈网络 ~毫秒级, 不再需要双 WM。

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

端到端重训练复现 (2026-11-06 14:38, 第三次独立验证, HEAD 7f5b146):
- 新 clone → 全管线 → **train_wm_ms + train_planner 从零重训** (seed 42) →
  重训权重 SHA-1 与已提交 gru_wm_ms.pt / policy_net.pt **逐位相同**
  (29374ac42e7b... / 9b008fb43e65...); held-out 8 项指标全精度一致;
  重训权重跑 eval_closedloop 与已提交结果仍仅时间戳/耗时 2 处差异。
- 结论: 全链路 (数据生成 → WM 训练 → 策略蒸馏 → 闭环评测) 固定 seed
  下完全确定, 训练本身就是可复现产物, 已提交权重 = 重训结果。

服务栈冷启动冒烟测试 (2026-11-06 14:52, 第四次独立验证, HEAD d99ec69):
- 新 clone → ingest + twin(2/场景) → uvicorn world6g.backend.api:app
  1s 就绪; 16 端点冒烟全部通过 (health/episodes/前端 2 页/wm rollout/
  wm verify 闭环/twin report/scene3d/capabilities/chat)。
- 已提交权重可被服务栈加载上线, 前端可视化 + WM 推理冷启动即用。
- 回执: docs/closedloop/reproduction_receipt.md (含 episode_id 键名注记)。

可复现性文档化 (2026-11-06 15:05):
- README 快速开始补注: ingest 前置条件 = SIM_ROOT 绝对路径
  (/Users/daxu/software/6G_data_simulation, 见 datalake/config.py),
  换机器需改 SIM_ROOT — 此前新用户首个命令即会失败且无提示。
- README 新增"可复现性"章节: 4 次独立验证摘要表 + 复现命令参数
  (--n-per-scene 24 --seed 2026) + episode_id 键名注记, 链接回执。

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
  模型权重入库）→ 782b845（服务栈 WM v2 + /wm/verify）→ 9a33cab（报告）→
  7f5b146（复现回执①）→ d99ec69（重训练 SHA 复现②）→ 62b869d（服务冒烟③）→
  4a193ef（README 可复现性）→ 本提交（论文闭环章节④）
- 关键架构：数据由 Tiny-Twin 仿真器产生（ingest 真实数据校准 → generate_twin
  仿真 → generate_actions 同根反事实分支）→ WM v2 训练 → CEM-MPC 规划 +
  策略蒸馏 → 闭环评测（同根校验 + bootstrap CI + 阴性对照）
- WM 决策保真度：Spearman +0.78（n=384），排序能力支撑真实决策
- 动作分布：学习策略 74% 选择 az_offset（波束方位角），21-23% 选 none（不过度干预）
