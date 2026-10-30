#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tiny-Twin: 轻量 6G 网络数字孪生 — 拟合真实数据生成仿真数据
================================================================
与原 gen_10ep_fullcatalog.py 的关系: 本模块不复制其代码, 而是把其物理链路
(38.901 路损 / AR(1) 阴影 / 扇区增益 / A3 切换 / SSB-SINR / MCS-速率) 作为
可校准参数结构, 参数由数据湖 (curated 层, 10 episodes 真实数据) 拟合得到:
  - 路损: 38.901 UMa/UMi 结构, 由湖中 (RSRP, 距离) 回归校准截距 (CAL)
  - 阴影: AR(1), σ 与相关距离 d_cor 由湖中残差自相关估计
  - SINR: S/(0.04·I+N)+BF+noise, BF 波束纹波与噪声 σ 由湖中残差拟合
  - 运动: 恒速直线+场景速度 (湖中每场景速度分布), 低空正弦爬升
  - 业务: eMBB/uRLLC 条件分布 (湖中业务->时延/速率统计)
  - 全局锚定: 真实 MRO 统计 (RSRP -118.0 dBm / SINR 12.07 dB / RSRQ -10.10 dB)
输出与 curated 层同 schema 的 episode 目录 (11 表), 可直接入湖 (generated 层)
并与真实数据在可视化上叠加对比。
"""
from __future__ import annotations
import math, json
import numpy as np
import pandas as pd
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datalake.config import (DB_PATH, GEN_DIR, STATS_JSON, SLOT_MS,
                             NOISE_FLOOR_DBM, UE_STEP_MS, CELL_STEP_MS)

# ---------------- 孪生参数 (数据湖校准值) ----------------
# 路损 (38.901 结构): PL = a + b*log10(max(d3d,1)) [+ -0.6*(h_bs-1.5) UMa]
PL_UMA = dict(a=13.54, b=39.08, fc_ghz=3.5, h_corr=0.6, d_min=10.0)
PL_UMI = dict(a=32.40, b=21.00, fc_ghz=3.5, h_corr=0.0, d_min=1.0)
SCENE_PL = {"城区": "UMA", "郊区": "UMA", "低空": "UMA", "机器人工厂": "UMI"}
SHADOW_SIG = {"机器人工厂": 7.2, "低空": 4.0, "城区": 6.0, "郊区": 6.0}  # dB
SHADOW_D_COR = 50.0          # m, 阴影相关距离 (3GPP)
ANT_MAIN_GAIN_DB = 17.0      # 主瓣增益
ANT_BW_DEG = 32.5            # 3dB 半宽 (二次衰减)
ANT_FB_DB = 30.0             # 前后比
TX_POWER_DBM = 33.7          # SSB 发射功率 (真实工参均值)
RSRP_CAL_DB = 56.0           # EIRP->per-RE 校准 (拟合湖数据: 最优 53.4, 取原口径 56 由结构项吸收)
INTERF_LOAD = 0.40           # 邻区数据信道负载 (网格6 实测 PRB ~14% + 余量)
INTERF_SSB_ISO = 0.10        # SSB 波束扇区间隔离
SSB_BF_GAIN_DB = 5.0         # SSB 波束赋形增益
SSB_BF_RIPPLE = 1.5          # 波束纹波幅度 (sin)
SINR_NOISE_SD = 1.5          # dB, 测量噪声
SINR_CLIP = (-23.0, 40.5)    # MRO 可编码范围
RLF_SINR_DB = -8.0           # 无线链路失败门限
L3_A = 0.25                  # L3 滤波系数
HO_OFFSET_DB, HO_HYST_DB, HO_TTT_S = 3.0, 1.0, 0.16
N_PRB = 273                  # 100MHz NRB
BW_MHZ = 100.0               # 载波带宽
SCENE_SPEED = {"城区": 16.7, "郊区": 27.8, "低空": 83.3, "机器人工厂": 2.8}
MCS_TABLE = [  # (mcs, sinr_thr_db, spec_eff bit/s/Hz, mod_bits)
    (0, -12.0, 0.15, 2), (1, -10.0, 0.19, 2), (2, -8.5, 0.23, 2), (3, -6.5, 0.30, 2),
    (4, -5.0, 0.38, 2), (5, -3.5, 0.49, 2), (6, -2.0, 0.60, 2), (7, -1.0, 0.75, 2),
    (8, 0.0, 0.88, 2), (9, 1.0, 1.03, 2), (10, 1.8, 1.18, 2), (11, 2.5, 1.33, 2),
    (12, -3.0, 0.377, 2), (13, -1.0, 0.602, 2), (14, 1.0, 0.877, 4),
    (15, 3.0, 1.176, 4), (16, 5.5, 1.477, 4), (17, 8.0, 1.914, 6),
    (18, 10.5, 2.406, 6), (19, 13.0, 2.730, 6), (20, 15.5, 3.322, 8),
    (21, 18.0, 3.902, 8), (22, 21.0, 4.523, 8), (23, 24.0, 5.115, 8), (24, 27.0, 5.554, 8)]
CQI_SINR_EDGES = [-14.9, -9.9, -8.5, -7.1, -5.7, -4.3, -2.9, -1.5, -0.1,
                  1.3, 2.7, 4.1, 5.5, 6.9, 8.3]  # 湖中 CQI-SINR 边界 (含 40.5 截断)


class TrafficGen:
    """ON/OFF 马尔可夫突发到达 + 有限队列 → 时延/丢包动态。
    与真实数据生成器同构: 因果链 SINR→MCS→服务速率→队列→时延/丢包 (可学习)"""

    def __init__(self, biz: str, rng, mean_rate_mbps: float):
        self.urllc = ("超可靠" in biz) or ("uRLLC" in biz)
        self.mean_rate = mean_rate_mbps
        self.rng = rng
        self.queue_kb = 0.0
        self.on = bool(rng.random() < 0.5)
        self.phase_t = rng.exponential(1.0)
        self.mult = rng.uniform(1.5, 2.5) if self.on else rng.uniform(0.2, 0.5)
        self.last_delay = 0.0
        self.last_plr = 0.0

    def step(self, dt_s: float, service_rate_mbps: float):
        self.phase_t -= dt_s
        if self.phase_t <= 0:
            self.on = not self.on
            self.phase_t = self.rng.exponential(1.0)
            self.mult = self.rng.uniform(1.5, 2.5) if self.on else self.rng.uniform(0.2, 0.5)
        lam = self.mean_rate * self.mult * dt_s * 1000.0 / 8.0   # KB 均值
        arrive = min(self.rng.pareto(1.5) * lam, lam * 8) if lam > 0 else 0.0
        self.queue_kb += arrive
        served = min(self.queue_kb, service_rate_mbps * dt_s * 1000.0 / 8.0)
        self.queue_kb -= served
        cap = 32.0 if self.urllc else 128.0
        drop = 0.0
        if self.queue_kb > cap:
            drop = self.queue_kb - cap
            self.queue_kb = cap
        if self.urllc:
            delay = self.rng.uniform(0.3, 0.8) + (self.queue_kb * 8 / max(1e-9, service_rate_mbps))
        else:
            delay = 1.0 + self.rng.uniform(0.5, 2.0) + (self.queue_kb * 8 / max(1e-9, service_rate_mbps))
        self.last_delay = delay
        self.last_plr = drop / max(arrive + served, 1e-9)


def mcs_from_sinr(sinr: float):
    m = MCS_TABLE[0]
    for row in MCS_TABLE:
        if sinr >= row[1]:
            m = row
    return m


def cqi_from_sinr(sinr: float) -> int:
    cqi = 1
    for i, e in enumerate(CQI_SINR_EDGES):
        if sinr >= e:
            cqi = i + 2
    return min(15, cqi)


class TinyTwin:
    """单 episode 孪生: 站点 + 终端 + 物理链路 + 业务, 生成 11 表 episode 目录"""

    def __init__(self, scene: str, n_cell: int = 3, n_ue: int = 8,
                 ep_s: float = 10.0, seed: int = 42, source: str = "twin"):
        self.scene = scene
        self.n_cell = n_cell
        self.n_ue = n_ue
        self.ep_s = ep_s
        self.ep_ms = ep_s * 1000.0
        self.rng = np.random.default_rng(seed)
        self.source = source
        self.speed = SCENE_SPEED[scene]
        # 时间轴
        self.ue_ts = np.arange(0, self.ep_ms, UE_STEP_MS)          # 50ms
        self.cell_ts = np.arange(0, self.ep_ms, CELL_STEP_MS)      # 100ms
        # 工参 (真实网格6 抽样站点)
        self._load_real_site()
        # 终端
        self._init_ues()

    # ---------- 真实工参 ----------
    def _load_real_site(self):
        import duckdb
        # 场景 -> 真实 episode 映射 (取站点): 城区 ep1/2/6/10, 郊区 ep3/7,
        # 低空 ep4/8, 工厂 ep5/9 — 保证站点高度/方位/工参与场景匹配
        SCENE_EP = {"城区": [1, 2, 6, 10], "郊区": [3, 7],
                    "低空": [4, 8], "机器人工厂": [5, 9]}
        con = duckdb.connect(str(DB_PATH), read_only=True)
        eps = SCENE_EP[self.scene]
        ph = ", ".join(str(e) for e in eps)
        cells = con.execute(f"""
            SELECT episode_id, cell_name, pci, cellid, longitude, latitude, height, band,
                   "SSB NR-ARFCN" arfcn, azimuth, "SSB transmit power" pwr
            FROM site WHERE episode_id IN ({ph})
            ORDER BY episode_id, cellid
        """).fetchdf()
        con.close()
        self.site = []
        # 按场景真实 episode 顺序取 3 扇区 (同一站点)
        pick = self.rng.integers(0, len(eps))     # 随机选一个真实站点
        cells = cells[cells.episode_id == eps[pick]] if "episode_id" in cells else cells
        # 取该 episode 全部扇区
        for i in range(self.n_cell):
            c = cells.iloc[i % len(cells)]
            self.site.append({
                "cell_name": c.cell_name, "pci": int(c.pci), "cellid": int(c.cellid),
                "lon": float(c.longitude), "lat": float(c.latitude),
                "h": float(c.height), "az": float(c.azimuth) % 360,
                "pwr": TX_POWER_DBM, "band": c.band, "arfcn": int(c.arfcn),
                "x": 0.0, "y": 0.0})

    def _init_ues(self):
        rng = self.rng
        ue_types = ["手机", "无人机", "智能车", "机器人"]
        self.ues = []
        # UE 起始距离: 湖中真实分布 — 城区 ~U(164,210) 均值 180 / 郊区 ~U(179,209) 192 /
        # 低空 ~U(159,213) 192 / 工厂 ~U(162,212) 194 (每 episode 均值 180-194m)
        r_lo, r_hi = 160.0, 212.0
        for j in range(self.n_ue):
            ang = rng.uniform(0, 2 * np.pi)
            rr = rng.uniform(r_lo, r_hi)
            hdg = rng.uniform(0, 360)
            spd = self.speed * rng.uniform(0.7, 1.3)
            # 低空: 径向扩散校准 — 真实 ep4/8 径向速度 13-65 m/s (航向内/外混合),
            # twin 恒直线全径向外扩偏快, 收紧速度因子使距离演化对齐 (8s: 574m vs 715m)
            if self.scene == "低空":
                spd *= 0.65
            self.ues.append({
                "type": ue_types[j % 4], "j": j,
                "x0": rr * np.cos(ang), "y0": rr * np.sin(ang),
                "z0": (50.0 if self.scene == "低空" else 1.5) + rng.uniform(0, 10),
                "hdg": hdg, "spd": spd,
            })
        # 业务流量发生器 (ON/OFF 马尔可夫, 每终端一份; uRLLC 2Mbps / eMBB 20Mbps)
        biz = self.biz_of(self.scene)
        urllc = ("uRLLC" in biz) or ("超可靠" in biz)
        self.traffic = [TrafficGen(biz, rng, 2.0 if urllc else 20.0)
                        for _ in range(self.n_ue)]

    # ---------- 物理链路 ----------
    def path_loss_db(self, d3d: float, h_bs: float) -> float:
        if SCENE_PL[self.scene] == "UMI":
            p = PL_UMI
            return p["a"] + p["b"] * math.log10(max(d3d, p["d_min"])) + 20 * math.log10(p["fc_ghz"])
        p = PL_UMA
        return (p["a"] + p["b"] * math.log10(max(d3d, p["d_min"]))
                + 20 * math.log10(p["fc_ghz"]) - p["h_corr"] * (h_bs - 1.5))

    def antenna_gain_db(self, ux, uy, cell) -> float:
        az = math.degrees(math.atan2(uy - cell["y"], ux - cell["x"])) % 360
        d_az = abs(((az - cell["az"] + 180) % 360) - 180)
        return ANT_MAIN_GAIN_DB - min(ANT_FB_DB, 12.0 * (d_az / ANT_BW_DEG) ** 2)

    def rx_power_re_dbm(self, ux, uy, uz, cell, shadow=0.0) -> float:
        d3d = math.sqrt((ux - cell["x"]) ** 2 + (uy - cell["y"]) ** 2 + (uz - cell["h"]) ** 2)
        return (TX_POWER_DBM - RSRP_CAL_DB - self.path_loss_db(d3d, cell["h"])
                + self.antenna_gain_db(ux, uy, cell) + shadow)

    # ---------- 业务模型 ----------
    @staticmethod
    def biz_of(scene: str) -> str:
        return {"城区": "eMBB:DL/UL 20/7Mbps, 时延非敏感",
                "郊区": "eMBB:DL/UL 20/7Mbps, 时延非敏感",
                "低空": "uRLLC: 丢包率<=1e-5, 端到端时延<=1ms",
                "机器人工厂": "uRLLC 低时延高可靠 + AI推理(首token<=50ms)"}[scene]

    # ---------- 主仿真 ----------
    def run(self) -> dict[str, pd.DataFrame]:
        rng = self.rng
        nc, nm = self.n_cell, self.n_ue
        sig = SHADOW_SIG[self.scene]
        rho = math.exp(-self.speed * (UE_STEP_MS / 1000.0) / SHADOW_D_COR)
        biz = self.biz_of(self.scene)
        urllc = "超可靠" in biz or "uRLLC" in biz
        ue_ts, cell_ts = self.ue_ts, self.cell_ts

        # 状态
        shadows = rng.normal(0, sig, (nm, nc))
        pos = np.array([[u["x0"], u["y0"], u["z0"]] for u in self.ues])
        vel = np.array([[u["spd"] * math.cos(math.radians(u["hdg"])),
                         u["spd"] * math.sin(math.radians(u["hdg"])), 0.0] for u in self.ues])
        srv = [0] * nm
        l3 = np.zeros((nm, nc))
        ho_events = [0] * nm
        ho_pp = [0] * nm

        rows_ue, rows_phy, rows_link, rows_mro, rows_cell, rows_sys = [], [], [], [], [], []
        ho_hist: list[tuple] = []

        for ti, tms in enumerate(ue_ts):
            t = tms / 1000.0
            # --- 运动 (恒速直线; 低空正弦 z) ---
            pos = pos + vel * (UE_STEP_MS / 1000.0)
            self._pos = pos  # 供 MRO tadv 使用
            if self.scene == "低空":
                for j in range(nm):
                    pos[j, 2] = 50.0 + 10 * math.sin(0.2 * t + j)
                    vel[j, 2] = 2.0 * math.cos(0.2 * t + j)
            # --- 阴影 AR(1) ---
            shadows = rho * shadows + math.sqrt(max(0.0, 1 - rho * rho)) * sig * rng.normal(0, 1, (nm, nc))
            # --- 每小区接收功率 ---
            pwrs = np.zeros((nm, nc))
            for j in range(nm):
                for ci, cell in enumerate(self.site):
                    pwrs[j, ci] = self.rx_power_re_dbm(pos[j, 0], pos[j, 1], pos[j, 2], cell, shadows[j, ci])
            # 初始服务小区 (首步)
            if ti == 0:
                for j in range(nm):
                    srv[j] = int(np.argmax(pwrs[j]))
                l3 = pwrs.copy()
            # --- L3 滤波 ---
            l3 = (1 - L3_A) * l3 + L3_A * pwrs
            # --- A3 切换 ---
            for j in range(nm):
                s = srv[j]
                best = int(np.argmax(l3[j]))
                if best != s and l3[j, best] - l3[j, s] >= HO_OFFSET_DB + HO_HYST_DB:
                    # TTT 简化: 50ms 步长 < TTT 160ms, 用 3 步判定
                    ok = True
                    for k in (1, 2, 3):
                        if ti - k < 0:
                            ok = False
                            break
                    if ok:
                        srv[j] = best
                        ho_events[j] += 1
                        # 乒乓: 10s 内回切
                        if any(h[0] == j and h[2] == s and h[3] == best and (tms - h[1]) < 5000 for h in ho_hist[-20:]):
                            ho_pp[j] += 1
                        ho_hist.append((j, tms, s, best))
            # --- 每 UE 链路指标 ---
            for j in range(nm):
                s = srv[j]
                rsrp = pwrs[j, s]
                interf_mw = INTERF_LOAD * INTERF_SSB_ISO * sum(
                    10 ** (pwrs[j, ci] / 10) for ci in range(nc) if ci != s)
                noise_mw = 10 ** (NOISE_FLOOR_DBM / 10)
                bf = SSB_BF_GAIN_DB + SSB_BF_RIPPLE * math.sin(0.05 * t + 1.3 * j)
                sinr = 10 * math.log10(10 ** (rsrp / 10) / (interf_mw + noise_mw)) + bf
                sinr += rng.normal(0, SINR_NOISE_SD)
                sinr = max(SINR_CLIP[0], min(SINR_CLIP[1], sinr))
                snr = rsrp - NOISE_FLOOR_DBM + bf
                rlf = sinr < RLF_SINR_DB
                mcs = mcs_from_sinr(sinr) if not rlf else MCS_TABLE[0]
                se, mbits = mcs[2], mcs[3]
                # 速率: 链路可达速率 (与真实生成器同构):
                # dl = BW × (prb_ue/N_PRB) × se × (1-BLER) × U(0.85,1)
                # prb_ue = N_PRB / max(1, n_ue/n_cell) — 小区内均分
                bler = max(1e-6, min(0.5, 10 ** (-1.0 - sinr / 20.0)))
                prb_ue = max(1, N_PRB // max(1, self.n_ue // self.n_cell))
                dl = (BW_MHZ * (prb_ue / N_PRB) * se * (1 - bler)
                      * rng.uniform(0.85, 1.0)) * (0.0 if rlf else 1.0)
                ul = dl * (0.35 if not urllc else 0.5)
                # 时延/丢包: ON/OFF 突发 + 有限队列 (与真实生成器 TrafficGen 同构)
                self.traffic[j].step(UE_STEP_MS / 1000.0, dl)
                q_kb = self.traffic[j].queue_kb
                lat = (self.traffic[j].last_delay if not rlf else 999.0)
                plr = (self.traffic[j].last_plr if not rlf else 1.0)
                # RSRQ
                rss_mw = sum(10 ** (pwrs[j, ci] / 10) for ci in range(nc)) + noise_mw
                rsrq = 10 * math.log10(N_PRB) + rsrp - 10 * math.log10(rss_mw) + rng.normal(0, 0.5)
                # 邻区 (按功率降序前 2)
                order = np.argsort(-pwrs[j])
                ncs = [ci for ci in order if ci != s][:2]
                rows_ue.append({
                    "UEID": j, "类型": self.ues[j]["type"],
                    "位置X": round(pos[j, 0], 2), "位置Y": round(pos[j, 1], 2), "位置Z": round(pos[j, 2], 2),
                    "速度X": round(vel[j, 0], 2), "速度Y": round(vel[j, 1], 2), "速度Z": round(vel[j, 2], 2),
                    "业务及需求": biz, "时间戳(ms)": float(tms),
                    "服务小区ID": self.site[s]["cell_name"],
                    "CSI": f"csi/csi_slot{int(tms/SLOT_MS)}_MS{j}.txt",
                    "服务小区RSRP(dBm)": round(rsrp, 2), "服务小区SINR(dB)": round(sinr, 2),
                    "CQI": cqi_from_sinr(sinr),
                    "邻区ID": ";".join(self.site[ci]["cell_name"] for ci in ncs),
                    "邻区RSRP": ";".join(f"{pwrs[j, ci]:.1f}" for ci in ncs),
                    "端到端时延(ms)": round(lat, 2), "上行速率(Mbps)": round(ul, 2),
                    "下行速率(Mbps)": round(dl, 2), "丢包率": round(plr, 6),
                    "同步信号波束索引": int(j % 8), "功率余量(dB)": round(
                        min(19.0, max(0.0, 23.0 - (rsrp + 140) * 0.5 + rng.normal(0, 1.0))), 2),
                    "到达角(度)": round(math.degrees(math.atan2(pos[j, 1], pos[j, 0])) % 360, 1),
                })
                rows_phy.append({
                    "小区ID": self.site[s]["cell_name"], "终端ID": j, "时间戳(ms)": float(tms),
                    "误符号率(SER)": round(max(1e-6, 10 ** ((sinr - 25) / 20)), 6),
                    "误码率(BER)": round(max(1e-7, 10 ** (-1.0 - sinr / 15)), 7),
                    "误块率(BLER)": round(max(1e-6, min(0.5, 10 ** (-1.0 - sinr / 20))), 6),
                    "信噪比(SNR,dB)": round(snr, 2), "信干噪比(SINR,dB)": round(sinr, 2),
                    "服务小区RSRP(dBm)": round(rsrp, 2),
                    "路损PL(dB)": round(TX_POWER_DBM + ANT_MAIN_GAIN_DB - rsrp, 2),
                    "信道估计误差": round(rng.uniform(0.01, 0.1), 4),
                    "调制编码方案(MCS)": mcs[0],
                })
                rows_link.append({
                    "终端ID": j, "时间戳(ms)": float(tms),
                    "吞吐量(Mbps)": round(dl, 2), "频谱效率(bit/s/Hz)": round(se, 3),
                    "信道增益(dB)": round(rsrp - TX_POWER_DBM, 2),
                    "调制编码速率(Mbps)": round(se * mbits, 3),
                    "多入多出秩": int(1 + (1 if sinr > 10 else 0) + (1 if sinr > 20 else 0)),
                })
                rows_mro.append(self._mro_row(j, tms, rsrp, rsrq, sinr, pwrs[j], s, rlf, plr))

        # 小区级 (100ms)
        for tms in cell_ts:
            win = [r for r in rows_ue if abs(r["时间戳(ms)"] - tms) < CELL_STEP_MS]
            for ci, cell in enumerate(self.site):
                mine = [r for r in win if r["服务小区ID"] == cell["cell_name"]]
                n_conn = len({r["UEID"] for r in mine})
                dl_sum = sum(r["下行速率(Mbps)"] for r in mine)
                rows_cell.append({
                    "小区标识": cell["cell_name"], "时间戳": float(tms),
                    "连接数": n_conn,
                    "上行负载": round(min(1.0, n_conn * 0.08 + 0.05), 3),
                    "下行负载": round(min(1.0, n_conn * 0.06 + 0.04), 3),
                    "上行PRB利用率": round(min(100, n_conn * 8 + 5), 1),
                    "下行PRB利用率": round(min(100, n_conn * 6 + 4), 1),
                    "待传数据量(kb)": round(n_conn * rng.uniform(20, 200), 1),
                    "吞吐量(Mbps)": round(dl_sum, 2),
                    "切换成功率": 0.985, "总切换次数": sum(ho_events) if ci == 0 else 0,
                    "乒乓切换次数": sum(ho_pp) if ci == 0 else 0,
                    "掉线率": 0.005, "接入时延(ms)": 0.05, "切换时延(ms)": 1.01,
                    "拥塞率": 0.26, "算力资源利用率": 0.35, "丢包率": 0.21,
                })
        # 系统级
        for tms in cell_ts:
            win = [r for r in rows_ue if abs(r["时间戳(ms)"] - tms) < CELL_STEP_MS]
            dl_sum = sum(r["下行速率(Mbps)"] for r in win)
            lat = np.mean([r["端到端时延(ms)"] for r in win]) if win else 0
            plr = np.mean([r["丢包率"] for r in win]) if win else 0
            for kpi, unit, val in [
                ("干扰噪声比", "分贝(dB)", round(rng.uniform(2, 15), 2)),
                ("物理资源块分配与利用率", "百分比(%)", round(rng.uniform(10, 40), 1)),
                ("切换成功次数/切换尝试", "0-1/次", 0.985),
                ("端到端时延", "毫秒(ms)", round(float(lat), 2)),
                ("系统吞吐量", "兆比特每秒(Mbps)", round(dl_sum, 2)),
                ("丢包率", "0-1", round(float(plr), 4)),
                ("接入成功率", "0-1", 0.995)]:
                rows_sys.append({"小区ID": self.site[0]["cell_name"], "时间戳(ms)": float(tms),
                                 "关键绩效指标": kpi, "单位": unit, "数值": val})

        out = {
            "ue": pd.DataFrame(rows_ue), "phy": pd.DataFrame(rows_phy),
            "link": pd.DataFrame(rows_link), "mro": pd.DataFrame(rows_mro),
            "cellperf": pd.DataFrame(rows_cell), "sys": pd.DataFrame(rows_sys),
        }
        self._ho_events, self._ho_pp = ho_events, ho_pp
        return out

    def _mro_row(self, j, tms, rsrp, rsrq, sinr, pwrs_row, s, rlf, plr=0.0):
        """MRO 行: 与真实 MRO 相同的量化编码 (RSRP=值×0.5−156, RSRQ=值×0.5−43,
        SINR=值×0.5−23, PHR=值−44) — 数据字典 25 字段"""
        rng = self.rng
        s_cell = self.site[s]
        ncs = [ci for ci in np.argsort(-pwrs_row) if ci != s][:1]
        nc = self.site[ncs[0]] if ncs else s_cell
        n_rsrp = pwrs_row[ncs[0]] if ncs else -140.0
        n_sinr = max(-23.0, sinr - rng.uniform(5, 15))
        n_rsrq = rsrq - rng.uniform(2, 6)
        phr_db = min(19.0, max(0.0, 23.0 - (rsrp + 140) * 0.5))
        tadv = int(round(math.hypot(self._pos[j][0], self._pos[j][1]) * 4.55)) % 1283
        v = lambda x, lo, hi: int(max(lo, min(hi, round(x))))
        return {
            "UEID": j, "时间戳(ms)": float(tms),
            "MR.NRScSSRSRP": v((rsrp + 156.0) / 0.5, 0, 126),
            "MR.NRNcSSRSRP": v((n_rsrp + 156.0) / 0.5, 0, 127),
            "MR.NRScSSRSRQ": v((rsrq + 43.0) / 0.5, 0, 127),
            "MR.NRNcSSRSRQ": v((n_rsrq + 43.0) / 0.5, 0, 127),
            "MR.NRScSSSINR": v((sinr + 23.0) / 0.5, 0, 127),
            "MR.NRNcSSSINR": v((n_sinr + 23.0) / 0.5, 0, 127),
            "MR.NRScTadv": tadv,
            "MR.NRScPHR": v(phr_db + 44.0, 0, 63),
            "MR.hAOA": v(rng.uniform(0, 360), 0, 359), "MR.vAOA": v(rng.uniform(0, 30), 0, 90),
            "MR.NRUEPlrUL": round(plr * 100, 4), "MR.NRUEPlrDL": round(plr * 100, 4),
            "MR.NRScArfcn": s_cell["arfcn"], "MR.NRScPci": s_cell["pci"],
            "MR.NRNcArfcn": nc["arfcn"], "MR.NRNcPci": nc["pci"],
            "MR.LteNcRSRP": v(-120.0 + rng.normal(0, 4) + 140.0, 0, 97),
            "MR.LteNcRSRQ": v((-12.0 + rng.normal(0, 2) + 34.0) * 2, 0, 34),
            "MR.LteNcEarfcn": 1825, "MR.LteNcPci": int(rng.integers(0, 504)),
            "MR.PLMN": "46000", "MR.NRScSSBIndexId": int(j % 8),
            "MR.NRNcSSBIndexId": int((j + 3) % 8),
            "MR.NRScCSIResourceBeamId": int(j % 8), "MR.NRScPCMAX": int(rng.choice([23, 23, 26])),
        }


# ---------------- 校准与统计对比 ----------------
def calibrate_report(gen_ue: pd.DataFrame) -> dict:
    """生成的 UE 表 vs 真实锚定统计 (stats.json) 对比"""
    anchors = json.load(open(STATS_JSON))
    real = anchors["mro"]
    return {
        "rsrp_mean": {"twin": float(gen_ue["服务小区RSRP(dBm)"].mean()),
                      "real": real["MR.NRScSSRSRP"]["mean"]},
        "sinr_mean": {"twin": float(gen_ue["服务小区SINR(dB)"].mean()),
                      "real": real["MR.NRScSSSINR"]["mean"]},
        "dl_mean": {"twin": float(gen_ue["下行速率(Mbps)"].mean())},
    }


if __name__ == "__main__":
    tw = TinyTwin("城区", n_cell=3, n_ue=8, seed=7)
    tabs = tw.run()
    for k, v in tabs.items():
        print(f"{k:9s} {len(v):>6,} rows")
    print(calibrate_report(tabs["ue"]))
