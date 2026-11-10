#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Calibrated Tiny-Twin: 基于真实网络数据校准的仿真引擎 (v3)
==========================================================
校准路径与 twin/engine.py 同接口 (scene, n_cell, n_ue, seed, run()),
但 KPI 生成直接吸收多源真实分布 (calib_model.REAL 锚点) 而非纯结构化近似:

  - RSRP: 锚点均值 + 相对距离结构 (50m=0dB) + 场景阴影 → 分布贴近真实逐场景统计
  - SINR: 相对 RSRP/干扰 + 噪声 → clamp 到真实锚点附近区间
  - CQI : 真实逐场景均值锚定 (饱和映射)
  - DL  : MCS×PRB×业务因子 (真实 dl 均值起底)
  - 时延/PLR: 业务条件分布 (eMBB 高 / uRLLC 低)
  - RSRQ: 全局锚 −10.1 附近
  - campus: 园区几何 — 站点落一期/二期/6期锚点, UE 沿园区道路低速行走

服务于 sim_calib.py 校准主线; uncal 对照走原 engine.TinyTwin。
构造器不依赖基类 scene dict (campus 无 SCENE_SPEED 键), 站点/KPI 全部自建。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twin.engine import TrafficGen, HO_OFFSET_DB, TX_POWER_DBM, NOISE_FLOOR_DBM
from twin.calib_model import (cal_cqi_from_sinr, cal_dl_mbps, cal_lat_ms,
                              cal_plr, cal_rsrq, REAL, CAMPUS_REAL, MRO_GLOBAL)
from twin.campus import load_campus, lonlat_to_xy

SCENE_SPEED_CAL = {"城区": 16.7, "郊区": 27.8, "低空": 83.3,
                   "机器人工厂": 2.8, "campus": 4.0}
SCENE_PL_CAL = {"城区": 35.0, "郊区": 33.0, "低空": 38.0,
                "机器人工厂": 30.0, "campus": 34.0}   # 校准入射角结构 (dB @ 50m)


class TinyTwin:
    """校准版孪生 (同原接口)"""

    def __init__(self, scene, n_cell=3, n_ue=8, ep_s=10.0, seed=42,
                 source="twin_cal", tick_ms=None):
        self.scene = scene
        self.n_cell = n_cell
        self.n_ue = n_ue
        self.ep_s = ep_s
        self.ep_ms = ep_s * 1000.0
        self.seed = seed
        self.source = source
        # 时序化原语 (M11): tick_ms=None → 走冻结的 run() 路径 (逐位不变);
        # tick_ms=int → run() 切 tick 推进 (时变功率动作可用 set_cell_tx_scale_db
        # 逐 tick 变更, L3 滤波历史保持在 self._tick_l3)。
        self.tick_ms = tick_ms
        self.rng = np.random.default_rng(seed)
        self.speed = SCENE_SPEED_CAL[scene]

        # 动作钩子 (同 generate_actions 接口)
        self.tx_scale_db = 0.0
        self.prb_scale = 1.0
        self.ho_offset_db = HO_OFFSET_DB
        # 多小区协同钩子 (None = 走原全局路径, 逐位不变):
        #   cell_tx_scale_db[i] — 第 i 小区功率偏移 (dB), 单小区独立生效
        #   cell_az_offset[i]   — 第 i 小区方位角偏移 (deg)
        self.cell_tx_scale_db = None
        self.cell_az_offset = None

        # 校准锚点
        self.anchor = REAL.get(scene, CAMPUS_REAL)   # {kpi: (mean, sd)}

        # 站点 + 终端
        if scene == "campus":
            self._load_campus_site()
            self._campus_ues()
        else:
            self._load_lake_site()
            self._init_ues()

        # 业务流量
        biz = self.biz_of(scene)
        self.urllc = "超可靠" in biz or "uRLLC" in biz
        self.traffic = [TrafficGen(biz, self.rng, 2.0 if self.urllc else 20.0)
                        for _ in range(self.n_ue)]

    # ---------------- 站点 ----------------
    def _load_lake_site(self):
        """从 lake 取真实站点 (与 engine 同规则, 3 扇区)"""
        import duckdb
        from datalake.config import DB_PATH
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
        pick = int(self.rng.integers(0, len(eps)))
        cells = cells[cells.episode_id == eps[pick]]
        self.site = []
        for i in range(self.n_cell):
            c = cells.iloc[i % len(cells)]
            self.site.append({
                "idx": i, "cell_name": c.cell_name, "pci": int(c.pci), "cellid": int(c.cellid),
                "lon": float(c.longitude), "lat": float(c.latitude),
                "h": float(c.height), "az": float(c.azimuth) % 360,
                "pwr": TX_POWER_DBM, "band": c.band, "arfcn": int(c.arfcn),
                "x": 0.0, "y": 0.0})

    def _load_campus_site(self):
        campus = load_campus()
        anchors = list(campus.anchors.values())[:3]
        self.site = []
        for i, a in enumerate(anchors):
            x, y = lonlat_to_xy(a["lon"], a["lat"])
            self.site.append({
                "idx": i, "cell_name": f"campus-{i+1}", "pci": 400 + i, "cellid": i + 1,
                "lon": a["lon"], "lat": a["lat"], "h": 25.0,
                "az": [0.0, 120.0, 240.0][i] + 20,
                "pwr": TX_POWER_DBM, "band": "N41", "arfcn": 504990,
                "x": x, "y": y,
            })

    # ---------------- 终端 ----------------
    def _init_ues(self):
        rng = self.rng
        types = ["手机", "无人机", "智能车", "机器人"]
        self.ues = []
        for j in range(self.n_ue):
            ang = rng.uniform(0, 2 * np.pi)
            rr = rng.uniform(160, 212)
            hdg = rng.uniform(0, 360)
            spd = self.speed * rng.uniform(0.7, 1.3)
            if self.scene == "低空":
                spd *= 0.65
            self.ues.append({
                "type": types[j % 4], "j": j,
                "x0": rr * np.cos(ang), "y0": rr * np.sin(ang),
                "z0": (50.0 if self.scene == "低空" else 1.5) + rng.uniform(0, 10),
                "hdg": hdg, "spd": spd,
            })

    def _campus_ues(self):
        rng = self.rng
        types = ["手机", "智能车", "机器人", "手机"]
        self.ues = []
        for j in range(self.n_ue):
            self.ues.append({
                "type": types[j % 4], "j": j,
                "x0": rng.uniform(-450, 450), "y0": rng.uniform(-350, 350),
                "z0": 1.5 + rng.uniform(0, 2),
                "hdg": rng.uniform(0, 360),
                "spd": self.speed * rng.uniform(0.8, 1.2),
            })

    @staticmethod
    def biz_of(scene: str) -> str:
        if scene == "campus":
            return "eMBB:DL/UL 20/7Mbps, 时延非敏感"
        from twin.engine import TinyTwin as E
        return E.biz_of(scene)

    # ---------------- 校准 KPI ----------------
    def ant_gain_db(self, ux, uy, cell) -> float:
        """扇区天线相对衰减 (az 动作生效): 主瓣 0dB, 3dB 半宽 32.5°, 背瓣 −30dB。
        常数抬升已由 anchor 吸收; 只保留方向相对作用。"""
        az = math.degrees(math.atan2(uy - cell["y"], ux - cell["x"])) % 360
        cell_az = cell["az"]
        if self.cell_az_offset is not None:
            cell_az = (cell_az + self.cell_az_offset[cell["idx"]]) % 360
        d_az = abs(((az - cell_az + 180) % 360) - 180)
        return -min(20.0, 10.0 * (d_az / 60.0) ** 2)

    def rx_power_re_dbm(self, ux, uy, uz, cell, shadow=0.0):
        """校准 RSRP: 环形小区统计模型 + 扇区天线增益 (az 动作因果)。
        RSRP(d) = 锚点均值 + 距离相对参考点微扰 + 天线增益 + 功率动作 + 阴影。
        ref 取各场景 UE 典型服务距离, 使 UE 主体落在真实均值附近。"""
        d = math.sqrt((ux - cell["x"]) ** 2 + (uy - cell["y"]) ** 2
                      + (uz - cell["h"]) ** 2)
        ref = {"城区": 185.0, "郊区": 195.0, "低空": 190.0,
               "机器人工厂": 185.0, "campus": 260.0}[self.scene]
        slope = {"城区": 32.0, "郊区": 30.0, "低空": 40.0,
                 "机器人工厂": 30.0, "campus": 32.0}[self.scene]
        m_mean = self.anchor["rsrp"][0]
        tx_db = self.tx_scale_db
        if self.cell_tx_scale_db is not None:
            tx_db += self.cell_tx_scale_db[cell["idx"]]
        struct = (m_mean + tx_db + self.ant_gain_db(ux, uy, cell)
                  + slope * math.log10(max(d, 60.0) / ref))
        # 密集园区: 建筑街谷遮挡 (真实园区 RSRP 落在城区弱区间 −108..−120)
        if self.scene == "campus":
            struct -= 16.0
        return struct + shadow

    def _sinr(self, rsrp, pwrs_row, s, rng):
        """校准 SINR: 以服务小区 s 相对最强邻区干扰 + 噪声"""
        interf_mw = 0.4 * 0.1 * sum(10 ** (pwrs_row[ci] / 10)
                                    for ci in range(self.n_cell) if ci != s)
        noise_mw = 10 ** (NOISE_FLOOR_DBM / 10)
        sinr = (10 * math.log10(10 ** (rsrp / 10) / (interf_mw + noise_mw))
                + 5.0 + rng.normal(0, 1.5))
        m, sd = self.anchor["sinr"]
        return max(m - 5, min(m + 9, sinr))

    # ---------------- 主仿真 ----------------
    def run(self) -> dict[str, pd.DataFrame]:
        if self.tick_ms is None:
            return self._run_frozen()
        return self._run_tick()

    def _tick_sig(self) -> float:
        return {"城区": 9.0, "郊区": 9.0, "低空": 8.0, "机器人工厂": 8.5,
                "campus": 9.0}[self.scene]

    # ---- 时序化路径 (M11): tick 推进 + 时变功率 ----
    def _init_tick_state(self):
        """初始化 tick 状态 (位置/速度/阴影/L3/服务小区)。幂等: 重复调用重置。"""
        rng = self.rng
        nm, nc = self.n_ue, self.n_cell
        sig = self._tick_sig()
        self._tick_shadows = rng.normal(0, sig, (nm, nc))
        self._tick_pos = np.array([[u["x0"], u["y0"], u["z0"]] for u in self.ues])
        self._tick_vel = np.array([[u["spd"] * math.cos(math.radians(u["hdg"])),
                                    u["spd"] * math.sin(math.radians(u["hdg"])), 0.0]
                                   for u in self.ues])
        self._tick_srv = [0] * nm
        self._tick_l3 = np.zeros((nm, nc))
        self._tick_ti = 0
        self._tick_t_ms = 0.0

    def _step_tick(self, dt_ms: float) -> list[dict]:
        """推进一个 tick (dt_ms 毫秒), 返回本 tick 的 UE 记录行。

        动作时变语义: 调用方可在两次 _step_tick 之间修改
        self.cell_tx_scale_db / self.tx_scale_db / self.prb_scale /
        self.ho_offset_db / self.cell_az_offset, 变更自下一 tick 起生效。
        L3 滤波历史在 self._tick_l3 (形状 (n_ue, n_cell))。"""
        rng = self.rng
        nm, nc = self.n_ue, self.n_cell
        sig = self._tick_sig()
        biz = self.biz_of(self.scene)
        ti = self._tick_ti
        tms = self._tick_t_ms
        t = tms / 1000.0
        dt_s = dt_ms / 1000.0

        pos = self._tick_pos + self._tick_vel * dt_s
        self._pos = pos
        if self.scene == "低空":
            for j in range(nm):
                pos[j, 2] = 50.0 + 10 * math.sin(0.2 * t + j)
        # 阴影相关时间归一化到 50ms 步长 (rho 定义同 frozen 路径, dt 无关)
        rho = math.exp(-self.speed * dt_s / 50.0)
        shadows = (rho * self._tick_shadows
                   + math.sqrt(max(0.0, 1 - rho * rho))
                   * sig * rng.normal(0, 1, (nm, nc)))
        pwrs = np.zeros((nm, nc))
        for j in range(nm):
            for ci, cell in enumerate(self.site):
                pwrs[j, ci] = self.rx_power_re_dbm(pos[j, 0], pos[j, 1],
                                                   pos[j, 2], cell, shadows[j, ci])
        if ti == 0:
            for j in range(nm):
                self._tick_srv[j] = int(np.argmax(pwrs[j]))
            l3 = pwrs.copy()
        else:
            l3 = (1 - 0.25) * self._tick_l3 + 0.25 * pwrs   # L3 滤波 (同 engine)
            for j in range(nm):                             # A3 硬切换
                s = self._tick_srv[j]
                best = int(np.argmax(l3[j]))
                if (best != s
                        and l3[j, best] - l3[j, s] >= self.ho_offset_db + 1.0):
                    self._tick_srv[j] = best
        self._tick_l3 = l3
        self._tick_shadows = shadows
        self._tick_pos = pos

        rows = []
        for j in range(nm):
            s = self._tick_srv[j]
            rsrp = pwrs[j, s]
            sinr = self._sinr(rsrp, pwrs[j], s, rng)
            cqi = cal_cqi_from_sinr(sinr, rng)
            dl = cal_dl_mbps(sinr, self.prb_scale, rng,
                             anchor_mbps=self.anchor["dl"][0])
            ul = dl * (0.35 if not self.urllc else 0.5)
            lat = cal_lat_ms(biz, rng)
            plr = cal_plr(biz, rng)
            rsrq = cal_rsrq(rsrp, sinr, rng)
            ncs = [ci for ci in np.argsort(-pwrs[j]) if ci != s][:2]
            rows.append({
                "UEID": j, "类型": self.ues[j]["type"],
                "位置X": round(pos[j, 0], 2), "位置Y": round(pos[j, 1], 2),
                "位置Z": round(pos[j, 2], 2),
                "速度X": round(self._tick_vel[j, 0], 2),
                "速度Y": round(self._tick_vel[j, 1], 2),
                "速度Z": round(self._tick_vel[j, 2], 2), "业务及需求": biz,
                "时间戳(ms)": float(tms),
                "服务小区ID": self.site[s]["cell_name"],
                "CSI": f"csi/csi_slot{int(tms / 0.5)}_MS{j}.txt",
                "服务小区RSRP(dBm)": round(rsrp, 2),
                "服务小区SINR(dB)": round(sinr, 2), "CQI": cqi,
                "邻区ID": ";".join(self.site[ci]["cell_name"] for ci in ncs),
                "邻区RSRP": ";".join(f"{pwrs[j, ci]:.1f}" for ci in ncs),
                "端到端时延(ms)": round(lat, 2),
                "上行速率(Mbps)": round(ul, 2), "下行速率(Mbps)": round(dl, 2),
                "丢包率": round(float(plr), 6),
                "同步信号波束索引": int(j % 8),
                "功率余量(dB)": round(
                    min(19.0, max(0.0, 23.0 - (rsrp + 140) * 0.5 + rng.normal(0, 1.0))), 2),
                "到达角(度)": round(math.degrees(math.atan2(pos[j, 1], pos[j, 0])) % 360, 1),
            })
        self._tick_ti += 1
        self._tick_t_ms += dt_ms
        return rows

    def _run_tick(self) -> dict[str, pd.DataFrame]:
        """tick 模式主循环: 固定 tick_ms 步长走完 ep_ms。"""
        assert self.tick_ms and self.tick_ms > 0
        self._init_tick_state()
        rows_ue = []
        n_ticks = int(round(self.ep_ms / self.tick_ms))
        for _ in range(n_ticks):
            rows_ue.extend(self._step_tick(self.tick_ms))
        return {"ue": pd.DataFrame(rows_ue)}

    # ---- 冻结路径: 历史行为逐位保留 (tick_ms=None 时走此路) ----
    def _run_frozen(self) -> dict[str, pd.DataFrame]:
        rng = self.rng
        nc, nm = self.n_cell, self.n_ue
        sig = {"城区": 9.0, "郊区": 9.0, "低空": 8.0, "机器人工厂": 8.5,
               "campus": 9.0}[self.scene]
        ue_ts = np.arange(0, self.ep_ms, 50.0)
        biz = self.biz_of(self.scene)

        shadows = rng.normal(0, sig, (nm, nc))
        pos = np.array([[u["x0"], u["y0"], u["z0"]] for u in self.ues])
        vel = np.array([[u["spd"] * math.cos(math.radians(u["hdg"])),
                         u["spd"] * math.sin(math.radians(u["hdg"])), 0.0]
                        for u in self.ues])
        srv = [0] * nm
        l3 = np.zeros((nm, nc))                      # L3 滤波 (ho_offset 生效)
        rows_ue = []

        for ti, tms in enumerate(ue_ts):
            t = tms / 1000.0
            pos = pos + vel * 0.05
            self._pos = pos
            if self.scene == "低空":
                for j in range(nm):
                    pos[j, 2] = 50.0 + 10 * math.sin(0.2 * t + j)
            rho = math.exp(-self.speed * 0.05 / 50.0)
            shadows = (rho * shadows + math.sqrt(max(0.0, 1 - rho * rho))
                       * sig * rng.normal(0, 1, (nm, nc)))
            pwrs = np.zeros((nm, nc))
            for j in range(nm):
                for ci, cell in enumerate(self.site):
                    pwrs[j, ci] = self.rx_power_re_dbm(pos[j, 0], pos[j, 1],
                                                       pos[j, 2], cell, shadows[j, ci])
            if ti == 0:
                for j in range(nm):
                    srv[j] = int(np.argmax(pwrs[j]))
                l3 = pwrs.copy()
            else:
                l3 = (1 - 0.25) * l3 + 0.25 * pwrs     # L3 滤波 (同 engine)
                # A3 硬切换: 邻区滤波 ≥ 服务 + ho_offset + Hys
                for j in range(nm):
                    s = srv[j]
                    best = int(np.argmax(l3[j]))
                    if (best != s
                            and l3[j, best] - l3[j, s] >= self.ho_offset_db + 1.0):
                        srv[j] = best
            for j in range(nm):
                s = srv[j]
                rsrp = pwrs[j, s]
                sinr = self._sinr(rsrp, pwrs[j], s, rng)
                cqi = cal_cqi_from_sinr(sinr, rng)
                dl = cal_dl_mbps(sinr, self.prb_scale, rng,
                                 anchor_mbps=self.anchor["dl"][0])
                ul = dl * (0.35 if not self.urllc else 0.5)
                lat = cal_lat_ms(biz, rng)
                plr = cal_plr(biz, rng)
                rsrq = cal_rsrq(rsrp, sinr, rng)
                ncs = [ci for ci in np.argsort(-pwrs[j]) if ci != s][:2]
                rows_ue.append({
                    "UEID": j, "类型": self.ues[j]["type"],
                    "位置X": round(pos[j, 0], 2), "位置Y": round(pos[j, 1], 2),
                    "位置Z": round(pos[j, 2], 2),
                    "速度X": round(vel[j, 0], 2), "速度Y": round(vel[j, 1], 2),
                    "速度Z": round(vel[j, 2], 2), "业务及需求": biz,
                    "时间戳(ms)": float(tms),
                    "服务小区ID": self.site[s]["cell_name"],
                    "CSI": f"csi/csi_slot{int(tms / 0.5)}_MS{j}.txt",
                    "服务小区RSRP(dBm)": round(rsrp, 2),
                    "服务小区SINR(dB)": round(sinr, 2), "CQI": cqi,
                    "邻区ID": ";".join(self.site[ci]["cell_name"] for ci in ncs),
                    "邻区RSRP": ";".join(f"{pwrs[j, ci]:.1f}" for ci in ncs),
                    "端到端时延(ms)": round(lat, 2),
                    "上行速率(Mbps)": round(ul, 2), "下行速率(Mbps)": round(dl, 2),
                    "丢包率": round(float(plr), 6),
                    "同步信号波束索引": int(j % 8),
                    "功率余量(dB)": round(
                        min(19.0, max(0.0, 23.0 - (rsrp + 140) * 0.5 + rng.normal(0, 1.0))), 2),
                    "到达角(度)": round(math.degrees(math.atan2(pos[j, 1], pos[j, 0])) % 360, 1),
                })
        return {"ue": pd.DataFrame(rows_ue)}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for scene in ["城区", "campus", "郊区", "低空", "机器人工厂"]:
        tw = TinyTwin(scene, n_cell=3, n_ue=8, seed=7)
        ue = tw.run()["ue"]
        print(f"[{scene}] n={len(ue)}  RSRP {ue['服务小区RSRP(dBm)'].mean():7.1f}±"
              f"{ue['服务小区RSRP(dBm)'].std():.1f} | "
              f"SINR {ue['服务小区SINR(dB)'].mean():6.1f}±{ue['服务小区SINR(dB)'].std():.1f} | "
              f"CQI {ue['CQI'].mean():.1f} | DL {ue['下行速率(Mbps)'].mean():6.1f}")
