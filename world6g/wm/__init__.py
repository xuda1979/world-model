"""动作条件世界模型基准套件 (Benchmark Suite v2)

论文配套代码: 校准反事实数字孪生 + 动作条件世界模型
- data.py     : 数据协议 (A/B/C 三任务 + 归一化 + 切分)
- models.py   : Persistence / LinearAR / GRU-WM / RSSM / Transformer / Physics-informed hybrid
- metrics.py  : RMSE / 多步 rollout / Δ-效应 / 校准
- train.py    : 统一训练循环 (1-step 训练 + 多步教师强迫可选)
- evaluate.py : 主评测入口 → docs/wm_suite/*.json + 图
"""
