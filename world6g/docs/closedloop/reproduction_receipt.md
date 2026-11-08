# 独立复现回执 (Reproduction Receipt) — 永久记录

## 第十次验证 (2026-11-07): 会话 26 校准动作模型 + 完整技术报告 v3.0 — 全链路检证

会话 26 新增三条证据链, 每条均以权威 JSON 为准 (零手抄), 并做独立探针复核:

### ① 校准策略动作模型 (train_policy_calib.py + eval_policy_calib.py)

- 训练: 在 `datalake/generated/twin_{cal,uncal}_action_dataset.parquet` 上, 用各自配对 WM
  (`models/wm_{cal,uncal}.pt`) 内做 CEM-MPC 专家采样 (n_window=2048, CEM seed=7), 蒸馏双头
  MLP 策略 (`models/policy_{cal,uncal}_net.pt`, 29,577 参数, seed 42)。
- 闭环评测: 统一规则 — 校准 twin 权威执行器 (CalTwin 同根分支), Windows A = 40 真实 held-out 窗
  (E3 协议), Windows B = 24 校园孪生窗 (E4 协议); 每窗动作真实施加, bootstrap n=2000。
- **参照一致性锁 (协议锁)**: `closedloop_calib_policy.json` 中的 CEM 参照与已提交的 E3/E4
  逐位一致 (小于显示精度性差异):
  - Windows A: cem-cal 期望真实奖励 −0.1261 vs 提交 closedloop_calib_compare.json −0.1260
    (同一源值 7 位小数格式差异); cem-uncal −0.1363 = 提交 −0.1363; P>0 0.952/0.3765 全一致
  - Windows B: cem-cal lift +0.0290 CI[0.0,+0.0665] P>0=0.96, 动作分布 {none:16, ho:5, az:3}
    = 提交 campus_action_report.json 逐位一致; cem-uncal −0.0065 P=0.411 一致
- 校准策略结果 (诚实呈现): Windows A pol-cal lift +0.0019 P>0=0.999 动作率 0.200 (极度克制);
  园区窗 pol-cal lift 0.0000 动作率 0.375 — 蒸馏层动作率显著低于 CEM 层, 校准优势在 CEM 层更强,
  报告第 4.5 节如实呈现。
- **协议锁原始级复算 (追加)**: 从 closedloop_calib_policy.json 的逐窗 `*_real` 数组独立重算
  均值/动作率 (不读 summary 字段): Windows A cem-cal mean −0.1261 = 提交、cem-uncal −0.1363
  = 提交、动作率 0.225/0.650 = 提交; Windows B cem-cal lift +0.0290 CI[0.0,0.0665] P=0.96 分布
  {none:16,ho:5,az:3} / cem-uncal −0.0065 P=0.411 分布 {ho:17,az:5,none:2} 全部 = 提交
  (campus_action_report.json) — **逐位 MATCH, 同一权威执行器同根一致**。
- **确定性重跑零差异 (追加)**: 用同一已提交权重/数据/种子复跑两个评价脚本, 与已提交 JSON
  递归逐位比对:
  - `eval_wm_calib_suite.py` (rng 2026, 480 窗) → wm_calib_suite.json **递归 diff = 0**
    (无时间戳字段; 已发布 h1/h8/h40 聚合为数据+脚本的纯函数)
  - `eval_policy_calib.py` (E3/E4 双窗口集 + bootstrap n=2000) → closedloop_calib_policy.json
    **递归 diff = 0** (含逐窗动作、CI、P 值、动作分布) — 协议锁数字端到端可位级重现。
- **报告生成器确定性 (追加)**: `make_full_report.py` 重生成 docx, 与已提交版本内容流
  (排除 docProps 时间戳) sha256 **逐位一致** (c0c731487084...), 且 31/31 溯源探针复跑
  全过 — docx 是源 JSON + 生成器的纯函数 (延续会话 22-24 的确定性承诺)。
- **训练位级复现 (追加, 决定性)**: 用已提交参数重训两个校准策略
  (`train_policy_calib.py --n-window 2048 --epochs 60 --lam-v 5.0 --lam-action 0.02`, seed 42):
  - policy_cal_net.pt 重训 MD5 **ef7e0669...** = 已提交逐位一致 (meta 唯一差异 trained_at)
  - policy_uncal_net.pt 重训 MD5 **5de8f333...** = 已提交逐位一致 (meta 唯一差异 trained_at)
  - 重训权重跑 eval_policy_calib → closedloop_calib_policy.json **递归 diff = 0**
  → 数据→训练→蒸馏→闭环评测全链路位级可复现 (延续会话 12 的方法论)。

### ② 校准 WM 预测能力套件 (eval_wm_calib_suite.py)

- 协议: 5 场景 CalTwin 基准分支, 每 (场景 × UE × h∈{1,8,40}) 确定性采样 4 窗 (rng 2026),
  起点 t∈[160, T−h] (≥8000ms held-out), 共 480 窗; 开环 rollout 反归一化比较 RMSE。
- 结果 (`docs/wm_suite/wm_calib_suite.json`): 校准 WM mean RMSE 全时域更优 —
  h1 4.316 vs 未校准 4.601, h8 5.578 vs 7.036, h40 7.978 vs 12.597
  (增益 h1 +0.28 / h8 +1.46 / h40 +4.62)。逐 KPI 诚实呈现 (SINR 增益最大, CQI/DL 部分混合)。

### ③ 完整技术报告 v3.0 (world6g_full_technical_report.docx)

- `make_full_report.py` 插入第 4 章「仿真器多源校准」(4.1 校准数据源 / 4.2 E1 分布保真 /
  4.3 E2 预测能力 / 4.4 E3 闭环决策 / 4.5 E4 园区应用 + 校准策略动作模型 / 4.6 诚实局限),
  版本 v2.0→v3.0, 章节重排 (4→5, 5→6, 6→7, 7→8, 8→9)。
- **溯源探针 24/24 MUST-BE-PRESENT 全过**: 期望值从 6 个权威 JSON 独立重算
  (sim_calib_report / wm_calib_suite / wm_calib_eval / closedloop_calib_compare /
  campus_action_report / closedloop_calib_policy), 断言 docx 正文+表格命中 —
  0.0701/0.0834/+0.0187, h8 5.578/7.036/+1.46, h40 +4.62, −0.1261/−0.1363,
  +0.0290/−0.0065/0.96, pol-cal +0.0019/P>0=0.999/动作率 0.200, 以及全部新章节标题。
- **探针脚本固化**: 增强版探针已固化为可重复运行脚本 `scripts/probe_report_v3.py`
  (约束: 从 6 个权威 JSON 自行重算期望值, 零手抄), 独立复跑 **31/31 MUST-BE-PRESENT
  全过** — 含 9 个章节标题 (4.1-4.6/5.1/7.2/附录 B)、E1 JS 0.083/0.070/+0.0187、
  E2 套件 6 项 (h1/h8/h40 cal+uncal) + 校园自洽 5.21/7.10、E3 4 项 (−0.1261/−0.1363/
  0.225/0.650)、E4 3 项 (0.0290/−0.0065/0.96)、三个策略数字 (0.0019/0.0000/0.999)、
  v3.0 版标。运行命令: `.venv/bin/python world6g/scripts/probe_report_v3.py`。

### 回归护栏

- `pytest world6g/tests/` → 8 passed (wm_api 协议锁, 0 skipped), 3.05s。
- 全部新数字读权威 JSON 动态生成, 无手抄字面量。

### 命运审计补遗 (第十一次验证, 2026-11-07): E2-real `wm_calib_eval.json` 溯源收敛

对 3 个会话 25 producer (sim_calib / eval_wm_calib / eval_campus_actions) 做会话 26 级
"确定性重跑零差异"审计时, 发现 `wm_calib_eval.json` (mtime 20:46) 与当前工作树
`wm_{cal,uncal}.pt` (mtime 18:10, seed 42 epochs 40) 重跑结果**不一致** (cal mean_all
14.3802 → 14.5507, 逐 KPI 漂移; uncal 8.8882 完全相同)。三次重跑相互一致 → 重跑确定性,
非 RNG/顺序问题。权证文件mtime序 (weights 18:10 < JSON 20:46) 排除了"旧权重≠JSON"解释;
`lake.duckdb` (10-30) 先于两者 → 数据源未变。结论: **该 JSON 由早前/不同的 wm_cal.pt
字节产物生成, 原字节已随本次重训覆盖, 无法逐位回溯** — 属会话 25 权证溯源缺口
(会话 26 位级复现只覆盖了 policy_cal/uncal, 未覆盖 wm_cal/uncal 本体)。

**处置 (已执行, 全链一致)**: 以当前可复现权重重跑 `eval_wm_calib.py` 就地再生成
(mean_all cal 14.5507 / uncal 8.8882), 并用同一 JSON 重生成两份报告:
- `make_full_report.py` → v3 docx 内容流 sha256 **C0C731487084 逐位不变** (docx 是 6 JSON
  纯函数; E2-real 在报告中仅定性"跨域 cal 略差", 无数值字面量, 定性结论 re-run 不变);
- `make_calib_report.py` → 会话 25 报告同步再生成, 定性表述 (跨域 cal 略高) 不变;
- 探针复跑 31/31 PASS; 全部数值消费方 (5.21/7.10 自洽 rollout, h1/h8/h40 套件, E3/E4
  闭环 = campus_action_report + 两 closedloop) 均不受影响。
- **审计价值**: 临时发现一处会话 25 未对齐的权证时序缺口并转化为"当前树可重跑=全链路
  可复现"的确定性; 报表结论与诚实表述在 re-run 下保持稳定。

### 第十一次验证续 (2026-11-07): 其余两个 producer 确定性验证 + 一处舍入边界收敛 — 3/3 全绿

对剩余两个会话 25 producer 做同款"确定性重跑"审计, 连同上节 E2-real 共 **3/3 全绿**:

1. **eval_campus_actions.py (E2 自洽 + E4)**: 同权重/种子重跑, 输出与已提交
   campus_action_report.json **递归 diff = 0** (E4 cal lift +0.0290 CI[0.0,0.0665] P>0=0.96
   动作分布 {none:16,ho:5,az:3} / uncal −0.0065 P=0.411 + E2 self_rollout 5.2135/7.1019
   全部逐位一致)。
2. **sim_calib.py (E1)**: 逐场景 JS 与两个 headline 均值全逐位一致; 唯一差异 =
   `cal_gain_js_rsrp_mean` 恰在舍入边界 0.01875 (np.mean 成对求和 → 0.018750000000000003
   → 0.0188; 纯 Python 顺序求和 → 0.01875 → 0.0187 = 已交付值)。处置: `sim_calib.py`
   聚合改用平台确定性的顺序求和 (1 行改动, 语义不变 — 同一 4 个 per-scene 增益的均值),
   重跑后**全部字段递归 diff = 0**; docx/探针数字 (+0.0187) 不受影响。另核: JSON 仅键序
   差异源于 `--only-cal` (复用已存 uncal), 数值无差异。
3. **处置后全链验证**: 探针 31/31 PASS; 三 producer 现全部可从当前树权重+数据
   位级复现 (`sim_calib.py` → `eval_wm_calib.py` → `eval_campus_actions.py` 全部 diff=0)。

**结论**: 会话 25 三个 producer 的确定性从"时序推断"升级为"重跑实测 diff=0";
E1/E2/E3/E4 协议锁数字全部可位级重现。唯一外部门 = operator commit 审批。

**结论**: 会话 26 三链条 (策略动作模型闭环 / cal-wm 预测套件 / 报告 v3.0 溯源) 全部检证通过,
参照值协议锁与之前的 E3/E4 提交结果一致; 审计补遗已把 E2-real 溯源收敛到当前可复现权重;
唯一外部门 = operator commit 审批。

---

## 第七次验证 (2026-11-06 20:35): 全论文数字对齐权威 results.json — 校正 5 处漂移

对 paper/main.tex 全部四任务数字做系统溯源, 以 docs/wm_suite/results.json 为唯一权威:

| 位置 | 原述 | 权威值 (results.json) | 处置 |
|---|---|---|---|
| 摘要/引言 | −23% | task_b.ms_improvement.GRU-WM = 0.2212 → −22% | 改 |
| Fig.2 Task B | GRU 1.103, RSSM 2.405/11.4×, MS 0.849, −23.1% | GRU 1.088 / RSSM 2.395/11.2× / MS 0.8476 / −22.1% | 改 |
| Task C 表 | GRU-WM Δ-RMSE 0.436 | cf_supervised.GRU-WM.rmse = 0.4304 | 改 |

核验通过未改的: Task A 表 (0.2598/0.2257/0.2272/0.2280/0.2383 ✓)、Task C 其余
(TF 0.480/0.67/0.71, RSSM 0.449/0.69/0.73, PhysWM 0.437/0.71/0.73, null 0.598 ✓)、
Task D 全部 (MC-z 4.4%/c=191/93.3%/NLL 1.02/GRU-NLL 95.2%·ECE 0.24·NLL −0.45/RSSM 95.6% ✓)。
twm_serve.py 注释与两份 goal.md 的数字同修正 (0.848/1.088/22%)。
main.pdf 重编译成功 (5 页, 零 LaTeX 错误), 与 tex 同步。

**结论**: 闭环/消融数字上一轮已溯源 (零漂移); 本轮回溯补齐四任务基准, 校正 5 处历史漂移
(均为近似值偏差, 非结论性错误), 全论文数字现与 results.json 逐位一致。

---

## 第八次补充审计 (2026-11-06 21:00): docx 生成器数字全动态化 — 无手抄环节

对 `scripts/make_full_report.py` 做数字字面量审计并改造: 全部剩余报表数字改为从权威 JSON
动态读取 (fidelity 表 → scene_fid() 按场景聚合; 闭环/消融/摘要/结论 → closedloop_fused /
ablation_wm_planner / fuse_wm_planner / policy_meta / results.json), 并删除无持久来源的
e2e 探针数字 (0.875/0.917/91.7%, 全仓 JSON 无字段) → 改写为 policy_meta.eval 持久指标。

docx 重生成验证: 关键数字就位 (0.848/−22.1%/0.5504), 旧字面量全词缺失 (0.849/1.103/0.917/0.875);
pytest 协议锁 8 passed; main.pdf 重编译 5 页 0 错误。至此 docx 与 main.pdf 的数字均无手抄环节,
与源 JSON 逐位一致或由生成器直接派生。

### 程序化独立复核 (21:20, 探针不依赖生成器内部函数)

1. **25 项 MUST-BE-PRESENT / 5 项 MUST-BE-ABSENT 全过**: 期望值脚本从 results.json /
   closedloop_fused.json / policy_meta.json 独立重算 (如 0.848 = GRU-WM-MS.rmse_h40 fmt3,
   0.5504 = Policy-Fused.lift mean fmt4, −22.1% = ms_improvement pct1, 86.5% = type_match_rate pct1),
   断言 docx 文本中含含; 旧值 0.849/1.103/0.917/0.875/91.7 全词缺失。
2. **场景保真度表独立重算 8/8 全过**: 农历外重算 scene_fid 聚合,
   低空 0.191/0.960、城区 0.694/0.870、机器人工厂 w1="— (CQI IQR=0)" r2=0.926、
   郊区 w1="— (CQI IQR=0)" r2=0.935 — 与 docx 文本逐项一致。
3. **生成器确定性 (同一数据源两邻再生成)**: 内容流 (除 docProps/*.xml 时间戳) sha256
   逐位一致 (3cca8a470466), 逐成员对比 diff 数 = 0 — docx 是源 JSON + 生成器的纯函数,
   仅 zip 元数据时间截点不同 (设计内)。

**结论**: 技术报告 docx 的数字链路 (源 JSON → 生成器 → 内容) 三层均由独立探针证实无手抄、
无漂移、确定性可复现。

---

## 第九次验证 (2026-11-07): 仿真器多源校准 — 校准后数据的世界模型/动作优化全面更强

**验证目标**: 用户要求用物理数据校准仿真器 (Sionna/Tiny-Twin)，校准后孪生数据训练的
WM + 动作优化必须比不校准更强，并应用到昌平信息港。

**方法 (公平对照)**: `twin/calibrated.py` (多源真实数据确定性校准引擎) vs `twin/engine.py`
(未校准基线)，同种子同架构对比；唯一差异 = 校准参数。多源数据: 10 episode 真实终端表
(逐场景 KPI 分布) + 360K MRO 锚点 (RSRP −118.0/SINR 12.1) + 小区工参 + OSM 园区几何。

**四连评测 (全部 cal > uncal, 数值可溯源)**:

| 评测 | 指标 | cal | uncal | 结论 |
|---|---|---|---|---|
| E1 分布保真 | RSRP JS 距离 | 0.0701 | 0.0834 | cal 增益 +0.0187 |
| E2 自洽 rollout | 校园窗 8 步 mean RMSE | 5.21 | 7.10 | cal 低 ~36% |
| E3 闭环决策 (40 真实窗) | 期望真实奖励 | −0.126 | −0.136 | cal +0.0102 |
| E3 动作率 | | 0.225 | 0.65 | cal 更克制 |
| E4 园区动作优化 (24 窗) | lift vs NoAction | +0.0290 P>0=0.96 | −0.0065 P=0.41 | cal 正增益, uncal 无效 |

**诚实局限** (报告第 7 节明示): ① 真实 (无锡) 跨域预测 cal RMSE 略高 (训练分布域差);
② Sionna 射线信道本机无法安装 (macOS arm64 依赖解析冲突)，校园建筑遮挡用数值近似
(−16dB 街谷) 替代，未来 Linux/GPU 接入射线增强; ③ 园区无独立真实测量数据，其目标
分布为从城区类比构造，闭环验证为孪生内自洽。

**溯源验证**: 技术报告 `world6g_calibration_report.docx` (8 节 7 表) 数字全部读
4 个权威 JSON (sim_calib_report / wm_calib_eval / closedloop_calib_compare /
campus_action_report)，python-docx 探针 MUST-BE-PRESENT **12/12 全过** (0.0701/
0.0834/+0.0187/5.21/7.10/+0.0290/0.96/−0.0065/0.225/0.65)，零手抄。

**结论**: 校准后仿真器生成的数据在所有决策相关评测上系统性更强 (分布保真 + 预测自洽 +
闭环决策 + 园区动作优化)，技术报告全面对比落地。唯一外部门 = operator commit 审批。

---

## 第六次验证 (2026-11-06 20:20): 论文/报告数字溯源 — 全部论文数值可逐位回溯到两个 closedloop 源文件

(本节含 docx 技术报告 v2.0 审计, 摘要与闭环表数字与源 JSON 一致, 零手抄漂移。)

### docx 技术报告 v2.0 审计 (2026-11-06 20:25, 独立抽取)

解包 `world6g_full_technical_report.docx` (会话 7 由 make_full_report.py 生成):

| 报告数值 | 源 JSON | 一致 |
|---|---|---|
| Policy-Fused lift +0.5504 CI [+0.361,+0.729] P=1.000, 真实奖励 +0.119 | closedloop_fused.json | ✅ |
| 闭环表 5 策略行 (NoAction −0.432 / CEM +0.285 [0.113,0.458] / Policy +0.282 [0.106,0.459] / Random −0.010 [−0.103,0.089]) | closedloop_fused.json summary.strategy | ✅ |
| 摘要 "+0.5504" vs 表 "+0.550" = 同一字段 (meta 值 0.550416599… 的 4位/4位近似), 非两处不同数字 | policy_fused_meta.json closedloop_verification.lift_vs_noaction | ✅ |

> `progress_report_20261105.docx` (11-05) 为会话 3 前产物, 无闭环数字, 时间标签正确。

**结论**: 编译产物 (docx) 的闭环数字与源 JSON 完全一致, 生成器 make_full_report.py
直接读源, 无手抄环节。与 main.tex 的溯源审计互补, 闭环数字证据链闭合。

**验证目标**: paper/main.tex Closed-Loop Planning 全节的每个字面数字, 都能从
`docs/closedloop/` 的 JSON 溯源, 不存在手抄/漂移。

| 论文表述 | 源文件字段 | 数值 | 一致 |
|---|---|---|---|
| CEM-MPC $+0.285$ CI $[+0.113,+0.458]$ P=0.9995 | results.json strategy.CEM-MPC.lift_vs_noaction | 0.285171539… / [0.1133,0.4578] / 0.9995 | ✅ |
| Policy $+0.283$ CI $[+0.106,+0.459]$ P=0.99875 | results.json strategy.Policy | 0.282456189… / [0.1062,0.4586] / 0.99875 | ✅ |
| Random $-0.010$ CI $[-0.104,+0.089]$ | results.json strategy.Random | −0.0099366131… / [−0.1035,0.0891] | ✅ |
| 决策保真度 Pearson $+0.80$ / Spearman $+0.78$ n=384 | results.json summary.wm_fidelity | 0.79779476… / 0.78244065… / n=384 | ✅ |
| 熔合策略 $+0.550$ CI $[+0.361,+0.729]$ | closedloop_fused.json strategy.Policy-Fused | 0.550416599… / [0.3614,0.7286] / P=1.0 | ✅ |
| 动作率 74% az / 21% none | results.json summary.actions.Policy | az 76/none 20 (of 96) | ✅ |
| 消融: MS $+0.456$ P=0.9998 (原文) | ablation_wm_planner.json | (见 ablation_report.md, 全窗重放一致) | ✅ |
| 消融配对 MS−v0.2 代价 −0.17 CI $[-0.40,+0.06]$ | ablation_wm_planner.json ablation_paired_ms_minus_v02 | {mean:−0.17096, ci95:[−0.39616,+0.05547]} | ✅ |

> 注: `closedloop_fused.json` 的 `summary.wm_fidelity` (pearson 0.741 / n=480) 是
> 96 窗 × 5 策略的融合全集保真度, 论文引用的是 results.json (96 窗 × 4 策略,
> n=384) — 两个源各按自己的窗口集统计, 论文未混用。

**结论**: 论文全节数字零手抄漂移; 每项均可逐位回源。此审计固化为永久回执条目。

---

## 第四次验证 (更正版): 重训 fused 策略位级复现判定 (2026-11-06 19:30, 工作树 HEAD = `930df44`)

> 本节取代此前版本的"第四次验证" (基于手写 harness `validate_retraining.py`,
> 结论已被本节推判定推翻)。旧版结论见 `.future-code/goal.md` 会话 11 更正文。

**验证目标**: 判定**已提交 fused 策略工件**是否端到端逐位可复现 —
公共提交脚本 main() 在隔离环境重训 `gru_wm_ms` + `policy_fused_net`,
与已提交权重逐位对拍。

- 隔离环境: `/tmp/wm_retrain_cfg/world6g` (独立 world6g 源码拷贝, 含
  bit-identical datalake/twin_action_dataset.parquet 8567c34b...; 不含旧模型)
- 重训脚本 = **已提交公共路径**: `scripts/train_wm_ms.py` 与
  `scripts/train_planner_fused.py` 的 `main()` (未改动, 未手写 harness)
- 输入契约: 同根分支数据 twin_action_dataset.parquet (对接 md5 8567c34b... =
  主仓, 已接对); wm_ms 为同一沙盒内刚重训的位级一致权重; v0.2 基线
  `gru_wm.pt` = 已提交工件

### 结果 1: GRU-WM-MS 重训 = 已提交 (逐位一致)

| 哈希 | 已提交 | 重训 (沙盒 main()) | 一致 |
|---|---|---|---|
| MD5 | `9dda1555569dd299d87a27f87799de5c` | `9dda1555569dd299d87a27f87799de5c` | ✅ |
| SHA-1 | `29374ac42e7b323161bc0a9a889f86d4a883f01a` | `29374ac42e7b323161bc0a9a889f86d4a883f01a` | ✅ |
| meta (非时间字段) | ... | ... | NONE 差异 |

0.35 墙钟重训 (40 epochs), 沙盒 norm.mu/sd/a_mu/a_sd 与已提交 meta 全同。

### 结果 2: Policy-Fused 重训 = 已提交 (逐位一致)

| 哈希 | 已提交 | 重训 (沙盒 main()) | 一致 |
|---|---|---|---|
| MD5 | `91482b421ffefe944ce1de68532c790c` | `91482b421ffefe944ce1de68532c790c` | ✅ |
| SHA-1 | `4e0426d098bc77623c98c75d04f01c61ff769564` | `4e0426d098bc77623c98c75d04f01c61ff769564` | ✅ |
| meta (非时间字段) | params 29577 / val_type_acc 0.8815789222717285 / n_window 512 | 同 | ✅ (唯一差异 `closedloop_verification` 为训练完成后追加的服务验证字段, 非训练输出) |

### 结果 3: gen_expert 跨进程位级确定 (probe, 2 个新进程)

同一已提交权重 + 同一 parquet, 两次独立进程跑 gen_expert (SEED=42,
n_windows=512):
- 两跑 **recommend 计数均为 316** (非会话 11 观察的 316 vs 317)
- t_lab / v_lab / v_lab(round4) md5 三组**全部门一致** (t_lab 8475d663...,
  v_lab e98a4921..., v_lab_round4 a3dcc478...)

→ gen_expert 在固定权重 + 固定数据下是确定性函数; CEMPlanner RNG 为
实例固定 (default_rng(seed=7) 每 fresh 实例重置), 不消耗全局 RNG 流。

### 结论: 会话 11 "fused 不可位级" 判断被推翻 — 根源是 harness 缺种子, 非管线缺陷

第四届验证 (`validate_retraining.py`, /tmp/wm_windtunnel) 的 74/96 失配
**不是** fused 管线固有不确定性, 而是该 harness 自身缺陷:
`validate_retraining.py` 第 54 行 `M.build_model` 之前**未调用**
`torch.manual_seed(42)` / `np.random.seed(42)` → GRU-WM init 消耗未播种
torch RNG → 产生与已提交不同的 WM 基线 (tunnel gru_wm_ms md5 `c0726252`
≠ 提交/重训 `9dda1555`) → 不同基线 → eval_closedloop 标签漂移 → 74/96 失配
(值 MAE≈3.77 恰为两版 WM 差异幅度的传导)。

已提交 `train_wm_ms.py` main() 的播种顺序是正确的 (39-40 行 seeding
→ 57 行 build_model), 这正是沙盒重训与已提交逐位一致的原因。

**最终判定**:
- **全 fused 链路在公共脚本 main() 下逐位可复现** (GRU-WM-MS 与
  Policy-Fused 权重 + meta 全部逐位一致, 非时间字段 zero diff)
- 旧版"第四次验证"回执的 "fused 路径不可位级重训 (固有设计性质)" 及两处
  goal.md 推进日志中相应表述 **均已撤回**, 代之以 harness 缺失播种的根源判定
- 协议无裂缝结论不变: 服务端 policy_action vs 已提交闭环 96 窗
  mismatch=0 (pytest 护栏, 会话 10 起持续复检)

---

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
