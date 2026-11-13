# 我们需要训练完世界模型和规划起（动作模型），数据通过仿真器/模拟器产生
Status: completed for operator REVIEW
Active constraints: make sure we have very good world prediction models and action models  using calibrated simulator's generated datasets and improve the full technical report(the technical report must be written in Chinese) | give a new progress report in Chinese | review again, and improve. finish the rest of the work that the two docs asked | git add, commit and push asap | continue ⎿  ◼ M11 测试/报告/账本/推送收尾    
     ◼ make_full_report.py 新增 7.6 节                         
     ✔ 跑全测试套件 + git 提交推送 (58/58 PASS + probe 72/72, fc2679c/62ad188 已推送)                       
     ✔ 更新双 goal.md 账本（会话 36, 双镜像 grep -c 均 = 1）                          
     ✔ 新中文进展报告 (2026-11-11, docs/progress_report_20261111.md + .docx)                             
     ✔ 重新生成 docx 并更新 probe (probe_report_v3.py 72/72 零手抄断言全命中)
     ✔ 复验加固: 新增 probe_progress_1111.py 周报溯源探针 36/36 (技术报告 probe 72/72 + 周报 probe 36/36 双守护)                                                                                                                                                                                                                                            
     ✔ 修复 train_coord_seq_policy.py 折内掩码/死代码 bug
     ✔ 跑通 M11 冒烟 + 完整训练闭环                                                                                                                                                                                                                                          
     ✔ v2 质量迭代: 3× 数据集 + WM early-stop/wd + 5 折重训对比                                                                                                                                                                                                              
     ✔ 新建 test_coord_seq.py 守护测试                                                                                                                                                                                                                                       
      … +1 completed

## 会话 37 (2026-11-12, 方案一 V0.2 演示面收口, 双账本镜像)
- **导出能力** (方案一 V0.2 验收点"界面与导出"): `GET /api/coord/ticks/export` —
  复用 tick 切片过滤语义, 输出 scene 中文文件名 CSV (RFC 5987 filename* + ASCII 回退)。
- **工作台双下载按钮** (workbench.html): 「⬇ 下载推荐 JSON」+「⬇ 导出切片 CSV」。
- **护栏测试 +2** → 套件 64/64 PASS (导出内容与 JSON 切片逐格一致 + 422/404 语义)。
- **演示脚本冻结** docs/demo_script_v02.md (2026-11-12 冻结, 13 日按脚本验收):
  10 分钟 6 环节, 全部数字取自 parquet + 天花板探针实测 (Σreward 4.7318 /
  tick8 +3.065 / win R²=0.898 / reward R²=0.62 上界 vs v2 0.534)。
- **Status: completed (2026-11-12)** — 收口 commit 含 api.py + workbench.html +
  test_wm_api.py + demo_script_v02.md + 双账本, 推送后复验 64/64 PASS。V0.2
  冻结演示前置全部就绪, 13 日按脚本打勾。

## 会话 38 (2026-11-12, 季度归档交付: 依赖锁 + 数据卡 + 启动手册, 双账本镜像)
- **依赖锁定文件** `world6g/requirements.lock` (96 个精确 pin, pip freeze 生成) —
  方案一 §7/方案二 §8 "固定配置和复现脚本" 缺失项。
- **数据卡** `world6g/docs/data_cards_v02.md` — 四类数据集全行数实测
  (curated 100,830 / twin 59,200 / twin_cal 104,000 / 反事实 282,525),
  模型权重注册表 (613,874 / 142,347×3 / 34,685 / 18,772 / 29,577),
  天花板数字 (win R²=0.8978, reward R²=0.6211 v3 上界 vs v2 0.5337)。
- **启动手册** `world6g/docs/startup_manual_v02.md` — 全新机器零起步全链路
  (安装/数据流水线/训练/评估探针/演示服务/演示日自检/异常排查)。
- **交叉核验修正**: twin 引擎描述纠正 (非 Sionna, 实为 38.901+AR(1) 结构引擎),
  coord_seq 结构实测 (5 场景 × 5 种子 × 36 ep = 25 场景-种对, 非 25 种子),
  WM ensemble 3 成员存单文件, API 参数 ep_i 对齐。
- **演示日前置自检全通 (2026-11-12 实测, 启动手册 §6)**: 服务直启无 500;
  `/api/coord/ticks?scene=campus&seed=9000&ep_i=0` → 200 + 10 rows (n_ticks=10);
  非法 scene → 422 (非 500); `/api/coord/ticks/export` → RFC 5987 中文文件名 +
  ASCII 回退, CSV 表体与 JSON 切片逐格一致 (含 reward 列); 冻结数字实测复验
  Σreward = **4.7318** / tick8 = **+3.0652** 与 demo_script_v02.md 冻结值一致。
- **review 再加固 (2026-11-12)**: 工作台 URL 修正 — 实际路由为 `/workbench`
  (api.py 830 行), 文档中 `/workbench.html` 会 404; 启动手册 §5 两处与演示脚本 v0.2
  演示入口已统一改 `/workbench` (live 验证 200)。随后全量实测演示脚本余下接口:
  `POST /api/wm/rollout` (auto 动作 az_offset, 双轨预测) / `POST /api/wm/verify`
   (预测-验证闭环, 城区, Δ效应逐 KPI) / `POST /api/wm/action` (PolicyNet-Fused
  一步决策) / `GET /api/coord/info` (LOSO 0.9631) / `GET /api/campus` / `POST /api/chat`
  (「城区平均RSRP」应答) 全部 200, 无 500。
- **交付物审计收官 (2026-11-12)**: 双 docx 逐项交叉核对 — 方案一 §五 五类交付
  (数据接入质检/场景校准/仿真生成回放/数据集服务/前端部署) 与 §七 季付清单、方案二
  §八 九项季付全部映射到仓库实物: 训练推理代码 33 脚本 + api.py 模型服务 /workbench,
  权重 models/*.pt 12 个生产权重 (coord_policy 613,874 / coord_seq_wm 3×142,347 /
  coord_seq_policy / 基线 GRU-WM / 多步一致性 / 策略蒸馏 fused 等), 依赖锁
  requirements.lock (96 pin), 四类数据集 parquet + 数据卡 data_cards_v02.md, 质量与
  增强效果报告 fidelity_report.md, 中文技术报告 world6g_full_technical_report.docx
  (汉字 6,350) + 校准报告 + 周报 docx (2026-11-11), 演示脚本 demo_script_v02.md
  + 启动手册 startup_manual_v02.md; 双守护探针复验 72/72 + 36/36, 镜像 grep -c 各 = 1;
  唯一剩余项 "备用录像" 为 13 日演示日项 (非本次缺口)。
- **复现性修复 (2026-11-13, review 再加固)**: 文档化复现路径 (.venv +
  requirements.lock) 缺 scikit-learn 依赖, 冻结天花板探针 probe_wm_ceiling.py
  无法文档化复现 (系统 python 3.9 亦不支持 PEP 604)。已装 scikit-learn 1.9.1 并
  重跑探针 byte-identical 复现 (win R²=0.8978 / reward R²=0.6211, 确定性),
  锁文件 96→100 pin (sklearn 栈 +4), 启动手册 §1 安装行加 scikit-learn 与数据卡
  pin 计数同步。commit 0a94c99 已推 origin/main。
- **Status: completed (2026-11-12)** — 三交付物 + 双账本镜像一次提交推送
  (616de45 + 状态收口 1062c44 + 依赖栈修复 0a94c99, 已推 origin/main,
  13 日演示前置全部就绪)。
- **V0.2 验收证据实测 (2026-11-13, 会话 39)**: 方案一 §7 表4 验收方式落成机器测量
  探针 `scripts/probe_v02_acceptance.py` — demo_script_v02.md 全演示序列连续回放
  × 3 轮: 3/3 无阻断 (9 端点 × 3, 含 422 守卫语义 + CSV 导出结构); 稳态缓存交互
  P95 = 359.6 ms (预算 ≤ 2,000 ms, 口径: api.py 无显式缓存层, DuckDB 进程内 +
  OS 页缓存即热路径, 稳态轮常规调用, 422 错误路径不计); 长任务进度标记 OK
  ('推理中…'/按钮禁用/tick 播放与进度条 + wm/verify timing_ms 服务端时长披露)。
  证据 → `datalake/generated/v02_acceptance_evidence.json`; 启动手册 §4 探针行 +
  §6 自检项同步。双账本镜像 (会话 39 各 1 处)。
