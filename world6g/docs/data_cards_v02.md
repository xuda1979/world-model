# 世界 6G 数据卡 (v0.2, 2026-11-12)

- **覆盖交付**: 方案一 §7 "四类数据集及数据卡"; 方案二 §8 "数据/模型卡、固定配置和复现脚本"
- **应对**: 方案一 §7 "可分发的脱敏演示数据" (全部 parquet 已脱敏: 小区标识/UEID 已改名, 无个人身份信息)
- **口径声明**: 下表所有 行数/大小/参数量 均为 2026-11-12 对 `world6g/datalake/` 下 parquet 实测 (pd.DataFrame.shape + os.path.getsize) 与 `world6g/models/*.json` 元数据实测, 非手抄快照

## 1. 四类数据集总览

| 类别 | 数据 | 行数 | 场景×种子×ep | 存放路径 | 生成脚本 |
|---|---|---|---|---|---|
| ① 真实 (curated) | 11 张表 (mro/phy/ue/link/sys/cellperf/action/map/csi/nrm/site) | 18,400×4 + 21,000 + 3,000×2 + 100+70+30+30 = **100,830** | 10 期会话 (源目录 `6G_episodes_10s_fullcatalog`, 每期 10 秒仿真) | `datalake/curated/*.parquet` | `ingest.py` (源 = 6G_episode_data/6G_episodes_10s_fullcatalog, 指纹见 `datalake/raw/registry.json`) |
| ② Tiny-Twin 生成 | `twin_{01..08}_*.parquet` 6 表/组 | 8×(1600×4+700+300) = **59,200** | 4 场景 (城区/郊区/低空/机器人工厂) × 复现 2 组 = 8 组 | `datalake/generated/` | `generate_twin.py --n-per-scene 2` (twin.engine.TinyTwin: 38.901 路损 + AR(1) 阴影 + 扇区增益, 参数由数据湖校准) |
| ③ 校准 twin | `twin_cal_{城区,郊区,低空,机器人工厂,campus}.parquet` | 5×20,800 = **104,000** | 5 场景 × 校准仿真 (campus 仅校准引擎支持) | `datalake/generated/` | `sim_calib.py` (uncal 4 场景可跳过复用) |
| ④ 反事实动作分支 | `coord_seq_dataset.parquet` / `coord_action_dataset.parquet` / `twin_action_dataset.parquet` / `twin_cal_action_dataset.parquet` / `twin_uncal_action_dataset.parquet` | 9,000 / 3,125 / 83,200 / 104,000 / 83,200 = **282,525** | coord_seq: 5 场景 × 5 种子 × 36 ep (25 场景-种对) | `datalake/generated/` | `gen_coord_seqs.py` / `generate_actions.py` |

补充: `twin_uncal_*` 未校准生成 (4 场景 × 20,800 = 83,200 行), 作为 ②/③ 对照保留。

## 2. 数据明细

### ① 真实数据 (curated, 数据湖核心三表为 18,400 行/表)

| 表 | 行数 | 大小 | 关键列 (节选) |
|---|---|---|---|
| mro | 18,400 | 235.6 KB | UEID, 时间戳(ms), MR.NRScSSRSRP, MR.NrscSSRSRQ, MR.NRScSSSINR, MR.NRScTadv, MR.hAOA/vAOA … |
| phy | 18,400 | 532.2 KB | 无线物理层 MR (16Hz) |
| ue | 18,400 | 706.8 KB | UE 级测量/KPI |
| link | 18,400 | 196.5 KB | 链路级吞吐/时延 |
| sys | 21,000 | 56.5 KB | 小区ID、KPI 名/单位/数值 (宽表转长表) |
| cellperf | 3,000 | 54.4 KB | 连接数、上下行 PRB 利用率、吞吐、切换/掉线/拥塞/时延 … |
| action | 3,000 | 97.6 KB | 小区功率/倾角动作记录 |
| map | 100 | 3.8 KB | 栅格地图 |
| csi | 70 | 3.8 KB | CSI 采样 |
| nrm | 30 | 71.1 KB | NRM 网元配置 |
| site | 30 | 15.7 KB | 站点/小区拓扑 |

### ② Tiny-Twin 生成 (twin_{01..08}, 每组 6 表同构, 1600 行/类)

| 表 (每组) | 行数 | 内容 |
|---|---|---|
| twin_N_phy / _ue / _mro / _link | 1,600 | Sionna 信道化物理层 / UE / MR / 链路 |
| twin_N_sys | 700 | 系统 KPI 长表 (同 ①sys 结构, 含 episode_id) |
| twin_N_cellperf | 300 | 小区绩效 (19 列: 吞吐/切换/PRB 利用率/拥塞/时延/丢包 …) |

### ③ 校准 twin 与 ④ 反事实动作分支

| 数据 | 行数 | 说明 (数字源自脚本元数据实测) |
|---|---|---|
| coord_seq_dataset.parquet | 9,000 | 时序 tick: 5 场景 [campus, 城区, 郊区, 低空, 机器人工厂] × 5 种子 × 36 ep (25 场景-种对), 每 ep 10 tick; 列 = scene/seed/ep_id/dec_idx/t_ms/p{0,1,2}/next_p{0,1,2}/win_obj/sinr_c{n}/n_ue_c{n}/l3_margin_c{n}/mean_sinr/ep_i/ref_obj/reward |
| coord_action_dataset.parquet | 3,125 | 125 联合功率组合 × 25 种子, 策略标签 (argmax reward) |
| twin_action_dataset.parquet | 83,200 | 反事实动作 (未校准双子) 4 场景 × … |
| twin_cal_action_dataset.parquet | 104,000 | 反事实动作 (校准双子) 5 场景 |
| twin_uncal_action_dataset.parquet | 83,200 | 未校准对照 |

## 3. 模型权重注册表 (models/*.pt + meta)

| 权重 | 参数 | 目标/说明 (meta.objective 原文) |
|---|---|---|
| coord_policy.pt | **613,874** | 逐小区协同策略蒸馏: 上下文 → 125 联合功率组合分布 (BC + 值蒸馏 smooth L1) |
| coord_seq_wm.pt (ensemble 3) | **142,347/个** (3 成员存于单文件) | 时序世界模型 M11: in 30 → 256×3 → out 11 (next_ctx(10)+reward(1)) |
| coord_seq_policy.pt | **34,685** | 时序协同动作模型 M11: ctx+scene → 125 组合 logits (WM-贪心规划器 BC 蒸馏, 部署侧免 WM) |
| gru_wm_ms.pt / gru_wm.pt | 18,772 | 多步一致性 GRU-WM (λ 0.3→0.9) |
| policy_fused.pt / policy_cal.pt / policy.pt | 29,577 | CEM-MPC 蒸馏策略 (fused 融合 λ=0.5 / cal 校准 / 基线) |

## 4. 质量与天花板数字 (wm_ceiling_probe.json 实测, 2026-11-12)

- 转移 8,100 条 (ep 内 t→t+1, 9/ep); GBM 5 折 LOSO (折=seed offset, 协议同 train_coord_seq_policy.py)
- win_obj[t+1] **R²=0.8978**, ref_obj[t+1] R²=0.8982 (纯外生, 特征几近充满)
- reward R²: v3 特征 0.6211 上界 (oracle_ctx 0.6417 / oracle 全知 0.6459); **v2 NN 基线 0.5337** → 仍有 ~0.09 空间
- 数据特征: reward std=1.619, Var(ref)/Var(reward)=12.39 (奖励噪声远大于外生分量), Corr(win_ref)=0.962

## 5. 脱敏与复现

- 真实源全部 MR/PHY/UE 标识已改性, 站点经纬度不入湖 (map 仅栅格)
- 生成链路全可复现: `generate_twin.py --n-per-scene 2 && generate_actions.py && gen_coord_seqs.py`, 依赖锁 `requirements.lock` 100 个精确 pin
- 场景列表: 城区 / 郊区 / 机器人工厂 (低空为负收益场景, 演示"非所有场景应上协同")
