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
- **Status: completed (2026-11-12)** — 三交付物 + 双账本一次提交推送 (待验证)。
