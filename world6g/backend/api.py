#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
6G 数据底座可视化 API (FastAPI)
================================
数据源: datalake (curated 真实 + generated twin) DuckDB/Parquet
端点:
  GET /api/episodes                    — episode 清单 (真实 + twin, 含场景/KPI 摘要)
  GET /api/kpi/{episode_id}            — 单 episode KPI 时序 (RSRP/SINR/吞吐/时延)
  GET /api/map/{episode_id}            — 地图数据: 站点 + UE 轨迹 + 服务小区
  GET /api/compare                     — 全 episode KPI 对比 (真实 vs twin)
  GET /api/fields                      — 数据字典 (249 字段)
  GET /api/twin/report                 — twin 校准报告 (真实值域 vs 生成值)
用法: .venv/bin/python world6g/backend/api.py  (127.0.0.1:8811)
"""
from __future__ import annotations
import sys, json, importlib
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from datalake.config import DB_PATH, GEN_DIR, DICT_CSV, EPISODES

app = FastAPI(title="6G 数据底座 API", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CON = duckdb.connect(str(DB_PATH), read_only=True)
CON_LOCK = __import__("threading").Lock()
def q(sql: str) -> pd.DataFrame:
    """线程安全的 duckdb 查询 (uvicorn 线程池并发时避免 connection busy)"""
    with CON_LOCK:
        return CON.execute(sql).fetchdf()

SCENE_ZH = {1: "城区", 2: "城区", 3: "郊区", 4: "低空", 5: "机器人工厂",
            6: "城区", 7: "郊区", 8: "低空", 9: "机器人工厂", 10: "城区"}


def _ep_ue_df(episode_id: str) -> pd.DataFrame:
    """统一读 episode 的 UE 表: 真实 (数字) 或 twin (twin_NN)"""
    if episode_id.startswith("twin"):
        return pd.read_parquet(GEN_DIR / f"{episode_id}_ue.parquet")
    return q(f'SELECT * FROM ue WHERE episode_id = {int(episode_id)}')


def _ep_site_df(episode_id: str) -> pd.DataFrame:
    if episode_id.startswith("twin"):
        # twin 用对应真实场景的站点工参 (engine 从湖中抽取)
        return pd.DataFrame()  # twin 站点在生成时记录, 前端用 (0,0) 三扇区呈现
    return q(f'SELECT * FROM site WHERE episode_id = {int(episode_id)}')


@app.get("/api/episodes")
def episodes():
    """全部 episode 清单: 真实 10 + twin N, 含场景与 KPI 摘要"""
    real = q("""
        SELECT u.episode_id::VARCHAR AS id,
               MAX(CASE WHEN m.字段='类型' THEN m.描述 END) AS scene,
               COUNT(DISTINCT u.UEID) AS n_ue,
               ROUND(AVG(u."服务小区RSRP(dBm)"),1) AS rsrp,
               ROUND(AVG(u."服务小区SINR(dB)"),1) AS sinr,
               ROUND(AVG(u."下行速率(Mbps)"),1) AS dl,
               'real' AS source
        FROM ue u LEFT JOIN map m ON m.episode_id=u.episode_id AND m.字段='类型'
        GROUP BY u.episode_id ORDER BY u.episode_id
    """)
    rows = real.to_dict("records")
    # twin
    for ep in sorted({p.stem.split("_")[0] + "_" + p.stem.split("_")[1]
                      for p in GEN_DIR.glob("twin_*_ue.parquet")}):
        df = pd.read_parquet(GEN_DIR / f"{ep}_ue.parquet")
        rep = json.loads((GEN_DIR / "twin_report.json").read_text()) if (GEN_DIR / "twin_report.json").exists() else []
        scene = next((r["scene"] for r in rep if r["gen_episode"] == ep), "?")
        rows.append({"id": ep, "scene": scene, "n_ue": int(df.UEID.nunique()),
                     "rsrp": round(float(df["服务小区RSRP(dBm)"].mean()), 1),
                     "sinr": round(float(df["服务小区SINR(dB)"].mean()), 1),
                     "dl": round(float(df["下行速率(Mbps)"].mean()), 1),
                     "source": "twin"})
    return rows


@app.get("/api/kpi/{episode_id}")
def kpi(episode_id: str, metric: str = Query("rsrp", description="rsrp|sinr|dl|ul|latency|plr|cqi")):
    df = _ep_ue_df(episode_id)
    if df.empty:
        raise HTTPException(404, f"episode {episode_id} not found")
    col = {"rsrp": "服务小区RSRP(dBm)", "sinr": "服务小区SINR(dB)", "dl": "下行速率(Mbps)",
           "ul": "上行速率(Mbps)", "latency": "端到端时延(ms)", "plr": "丢包率",
           "cqi": "CQI"}[metric]
    # 系统级时间序列 (每 50ms 平均)
    g = df.groupby("时间戳(ms)")[col]
    ts = pd.DataFrame({"t": g.mean().index.astype(float), "mean": g.mean().values,
                       "p10": g.quantile(0.1).values, "p90": g.quantile(0.9).values})
    # 每 UE 曲线 (前 12 条)
    per_ue = {}
    for ueid, gg in df.groupby("UEID"):
        if len(per_ue) >= 12:
            break
        per_ue[str(ueid)] = {"t": gg["时间戳(ms)"].tolist(), "v": gg[col].tolist()}
    # 小区级吞吐 (真实: duckdb; twin: parquet; 列名统一)
    if episode_id.startswith("twin"):
        cp = pd.read_parquet(GEN_DIR / f"{episode_id}_cellperf.parquet")
        cp = cp.rename(columns={"小区标识": "cell", "时间戳": "t", "吞吐量(Mbps)": "thr"})
    else:
        cp = q(f"""
            SELECT "小区标识" cell, 时间戳 t, "吞吐量(Mbps)" thr
            FROM cellperf WHERE episode_id::VARCHAR = '{episode_id}'
            ORDER BY t""")
    cell_thr = {c: {"t": g2.t.tolist(), "v": g2.thr.tolist()}
                for c, g2 in cp.groupby("cell")} if not cp.empty else {}
    return {"metric": col, "ts": ts.round(3).to_dict("list"), "per_ue": per_ue,
            "cell_throughput": cell_thr}


@app.get("/api/map/{episode_id}")
def map_data(episode_id: str):
    """地图: UE 轨迹 + 服务小区切换 + 站点位置"""
    df = _ep_ue_df(episode_id)
    if df.empty:
        raise HTTPException(404)
    site = _ep_site_df(episode_id)
    traces = []
    for ueid, g in df.groupby("UEID"):
        g = g.sort_values("时间戳(ms)")
        traces.append({
            "ue": int(ueid), "type": g["类型"].iloc[0],
            "x": g["位置X"].round(2).tolist(), "y": g["位置Y"].round(2).tolist(),
            "z": g["位置Z"].round(2).tolist(),
            "rsrp": g["服务小区RSRP(dBm)"].round(1).tolist(),
            "sinr": g["服务小区SINR(dB)"].round(1).tolist(),
            "cell": g["服务小区ID"].tolist(), "t": g["时间戳(ms)"].tolist()})
    sites = []
    if not site.empty:
        for _, s in site.iterrows():
            sites.append({"cell": s["cell_name"], "az": float(s["azimuth"]),
                          "h": float(s["height"]), "pci": int(s["pci"]),
                          "lon": float(s["longitude"]), "lat": float(s["latitude"])})
    return {"traces": traces, "sites": sites}


@app.get("/api/compare")
def compare():
    """全 episode 对比: 真实 vs twin (RSRP/SINR/DL 散点)"""
    rows = episodes()
    return rows


@app.get("/api/fields")
def fields():
    """数据字典 249 字段"""
    df = pd.read_csv(DICT_CSV)
    return JSONResponse(df.fillna("").to_dict("records"))


@app.get("/api/twin/report")
def twin_report():
    p = GEN_DIR / "twin_report.json"
    if not p.exists():
        raise HTTPException(404, "twin report not generated")
    return json.loads(p.read_text())


@app.get("/api/health")
def health():
    return {"status": "ok", "tables": [r[0] for r in CON.execute("SHOW TABLES").fetchall()]}


# ================= 3D 五层数据工作台 =================
# 五层: 环境特征 / 设备配置 / 电磁传播特征 / 业务特征 / 性能KPI (方案一)

@app.get("/api/scene3d/{episode_id}")
def scene3d(episode_id: str):
    """3D 场景全部数据 (五层渲染, 单次请求).
    第1层 环境特征: 园区建筑/道路 (中国移动信息港·昌平, OSM)
    第2层 设备配置: 站点工参 (真实 episode 工参; twin 用对应场景工参)
    第3层 电磁传播: 地面 RSRP radio map 栅格
    第4层 业务特征: UE 逐帧位置/类型/业务 + 轨迹
    第5层 性能KPI: UE hover (RSRP/SINR/CQI/速率/时延/丢包) + 小区 hover KPI"""
    df = _ep_ue_df(episode_id)
    if df.empty:
        raise HTTPException(404)
    import numpy as _np

    # ---- 第1层 环境特征: 园区建筑 + 道路 (信息港 ENU 米制) ----
    buildings, roads = [], []
    try:
        from twin.campus import campus_payload as _cp
        cp = _cp()
        buildings = cp["buildings"]
        roads = cp["roads"]
    except Exception:
        pass

    # ---- 第2层 设备配置: 站点 (episode 工参 + 园区部署位置) ----
    # UE 坐标以站点簇为原点 (真实网格布局); 站点在园区 ENU 中的部署位置由
    # UE 包络平移到园区核心区 (演示部署, 来源标识 "仿真部署")
    site = _ep_site_df(episode_id)
    if site.empty and episode_id.startswith("twin"):
        # twin 无 site 表: 用对应场景真实 episode 的工参 (与 twin 生成同源)
        _SCENE_EP = {"城区": 1, "郊区": 3, "低空": 4, "机器人工厂": 5}
        try:
            rep = json.loads((GEN_DIR / "twin_report.json").read_text())
            scene = next((r["scene"] for r in rep if r["gen_episode"] == episode_id), "城区")
            site = _ep_site_df(str(_SCENE_EP.get(scene, 1)))
        except Exception:
            site = pd.DataFrame()
    sites = []
    ue_x = df["位置X"].astype(float); ue_y = df["位置Y"].astype(float)
    if not site.empty:
        for _, s in site.iterrows():
            sites.append({"cell": s["cell_name"], "az": float(s["azimuth"]),
                          "h": float(s["height"]), "pci": int(s["pci"]),
                          "lon": float(s["longitude"]), "lat": float(s["latitude"]),
                          "x": 0.0, "y": 0.0,
                          "tx_pwr": str(s["SSB transmit power"])})
    # UE 包络 -> 园区核心区 (锚点信息港6期东南开阔区) 平移
    off_x, off_y = 728.0, 345.0
    if sites and not df.empty:
        off_x = 728.0 - float((ue_x.min() + ue_x.max()) / 2)
        off_y = 345.0 - float((ue_y.min() + ue_y.max()) / 2)
    df = df.copy()
    df["位置X"] = ue_x + off_x
    df["位置Y"] = ue_y + off_y

    # ---- 第2层 设备配置: NRM 配置 (真实 episode) ----
    cfg = []
    try:
        if not episode_id.startswith("twin"):
            nrm = q("SELECT * FROM nrm LIMIT 40")
            cfg = nrm.fillna("").astype(str).to_dict("records")
    except Exception:
        cfg = []

    # ---- 第3层 电磁传播: 地面 radio map (RSRP, 最新帧 UE 测量内插) ----
    t_max = float(df["时间戳(ms)"].max())
    latest = df[df["时间戳(ms)"] == t_max]
    from scipy.interpolate import griddata as _gd
    gx, gy = _np.mgrid[off_x - 450:off_x + 450:31j, off_y - 450:off_y + 450:31j]
    pts = latest[["位置X", "位置Y"]].values
    vals = latest["服务小区RSRP(dBm)"].values
    grid_rsrp = _gd(pts, vals, (gx, gy), method="linear")
    radio_grid = {"x": gx.tolist(), "y": gy.tolist(),
                  "rsrp": _np.round(_np.nan_to_num(grid_rsrp, nan=-140.0), 1).tolist()}

    # ---- 第4层 业务特征 + 第5层 性能KPI: UE 逐帧 ----
    ues = []
    for _, r in latest.iterrows():
        ues.append({
            "ue": int(r["UEID"]), "type": r["类型"], "x": float(r["位置X"]),
            "y": float(r["位置Y"]), "z": float(r["位置Z"]),
            "rsrp": float(r["服务小区RSRP(dBm)"]), "sinr": float(r["服务小区SINR(dB)"]),
            "cqi": int(r["CQI"]), "dl": float(r["下行速率(Mbps)"]), "ul": float(r["上行速率(Mbps)"]),
            "lat": float(r["端到端时延(ms)"]), "plr": float(r["丢包率"]),
            "cell": r["服务小区ID"], "svc": r["业务及需求"],
            "spd": float(_np.hypot(r["速度X"], r["速度Y"]))})
    # 轨迹 (细线)
    traces = []
    for ueid, g in df.groupby("UEID"):
        g = g.sort_values("时间戳(ms)")
        traces.append({"ue": int(ueid), "x": g["位置X"].round(1).tolist(),
                       "y": g["位置Y"].round(1).tolist(), "z": g["位置Z"].round(1).tolist()})

    # ---- 第5层 性能KPI: 小区级 (cellperf 最新, 基站 hover) ----
    cellkpi = []
    if episode_id.startswith("twin"):
        cp = pd.read_parquet(GEN_DIR / f"{episode_id}_cellperf.parquet")
    else:
        cp = q(f'SELECT * FROM cellperf WHERE episode_id::VARCHAR=\'{episode_id}\'')
    if not cp.empty:
        lt = cp[cp["时间戳"] == cp["时间戳"].max()]
        for _, r in lt.iterrows():
            cellkpi.append({"cell": r["小区标识"], "conn": int(r["连接数"]),
                            "thr": float(r["吞吐量(Mbps)"]),
                            "prb_dl": float(r["下行PRB利用率"]),
                            "prb_ul": float(r["上行PRB利用率"]),
                            "cong": float(r["拥塞率"]), "drop": float(r["掉线率"])})
    return {"sites": sites, "ues": ues, "traces": traces, "radio_grid": radio_grid,
            "cellkpi": cellkpi, "nrm": cfg, "t_max": t_max,
            "buildings": buildings, "roads": roads,
            "map_name": "中国移动信息港 (昌平) · 北京移动信息港/城区, OSM 建筑布局"}



LAYER_DOCS = {
    "环境特征": {"能力": "新站预测", "内容": "地图、建筑、候选站址、站高和周边测量",
                 "渲染": "地面栅格 + 站点扇区 + 建筑体块"},
    "设备配置": {"能力": "参数调整后的信号预测", "内容": "配置有效期、动作前后参数、实际生效时间",
                 "渲染": "基站天线属性面板 + NRM 配置"},
    "电磁传播特征": {"能力": "Radio map 重构", "内容": "MR/MDT、位置与精度、信道和信号栅格",
                 "渲染": "地面 RSRP 热力栅格"},
    "业务特征": {"能力": "用户行为预测", "内容": "可关联轨迹、驻留/转移、业务到达或群体分布",
                 "渲染": "UE 球 (类型/业务/速度) + 轨迹"},
    "性能KPI": {"能力": "KPI 预测", "内容": "PM 及协议记录、吞吐/连接数/负载、时延与队列",
                 "渲染": "UE hover 卡 (RSRP/SINR/CQI/速率/时延) + 基站 hover KPI"},
}


@app.get("/api/layers")
def layers():
    """五层数据结构说明"""
    return LAYER_DOCS


# ================= 能力演示: 五项能力 + 世界模型动作对照 =================

@app.get("/api/capabilities")
def capabilities():
    """方案二 §四 能力演示: 每项能力一张结果卡 (常驻数据来源标识)"""
    cards = []
    try:
        wm = importlib.import_module("twm_serve")
        info = wm.model_info()
    except Exception:
        info = {"available": False}
    cards.append({
        "id": "wm", "name": "KPI 预测 (世界模型)",
        "desc": "动作条件 GRU 世界模型, 训练自同根反事实分支数据; 多步自回归 rollout",
        "model": info, "source": "GRU-WM v0.2 · twin_action_dataset.parquet (83,200 样本)",
    })
    cards.append({
        "id": "action", "name": "动作效应对照",
        "desc": "反事实同根分支: az/pmax/prb/ho 四类动作对 RSRP/SINR/DL/CQI 的实测效应",
        "source": "action_report.json (twin 动作族 48 组)",
    })
    try:
        p_info = wm.policy_info()
    except Exception:
        p_info = {"available": False}
    if p_info.get("available"):
        cl = p_info.get("closedloop") or {}
        cards.append({
            "id": "planner", "name": "动作模型 (规划器推荐)",
            "desc": "融合奖励蒸馏动作模型: 一步前馈决策 (az/pmax/prb/ho), 闭环实测 "
                    f"lift +{cl.get('lift_vs_noaction', '?')} (96 held-out 窗, "
                    f"P>0={cl.get('p_lift_gt_0', '?')}); 首个真实奖励为正的策略",
            "model": {"version": p_info["version"], "params": p_info["params"],
                      "hist": p_info["hist"], "available": True},
            "source": cl.get("result_file", "docs/closedloop/closedloop_fused.json"),
        })
    cards.append({
        "id": "fidelity", "name": "统计保真度",
        "desc": "真实 338 万 MRO 样本 vs Twin 生成: 分布对齐检验",
        "source": "docs/fidelity_report.json",
    })
    cards.append({
        "id": "chat", "name": "数据湖问答",
        "desc": "基于 DuckDB/Parquet 的规则问答, 支持 KPI 查询与五层解释",
        "source": "lake.duckdb (12 表) + generated/",
    })
    return cards


@app.post("/api/wm/rollout")
async def wm_rollout(req: dict):
    """世界模型多步 rollout: {episode_id, ue?, action: {type, value}, steps}
    返回 baseline 与 action 两条预测轨, 供前端对照"""
    ep_id = str(req.get("episode_id", "")).strip()
    if not ep_id:
        raise HTTPException(422, "episode_id 必填 (twin_NN 或数据湖 episode 编号)")
    action = req.get("action") or {"type": "baseline", "value": 0}
    steps = int(min(max(int(req.get("steps", 40)), 1), 200))
    try:
        wm = importlib.import_module("twm_serve")
        if wm.load_model() is None:
            raise HTTPException(503, "GRU-WM model not available")
        df = _ep_ue_df(ep_id)
        if df.empty:
            raise HTTPException(404, f"episode {ep_id} not found")
        cols = wm._META["state_cols"]; H = wm._META["hist"]
        ue = int(req.get("ue", df.UEID.iloc[0]))
        g = df[df.UEID == ue].sort_values("时间戳(ms)")
        hist = g[cols].values[:H].astype(float)
        # action="auto" → 动作模型一步决策 (与 verify 同构, 使"策略推荐"在两种模式可用)
        auto_action = None
        auto_action_raw = None
        if isinstance(action, str) and action == "auto":
            auto_action_raw = wm.policy_action(hist)
            if not auto_action_raw["available"] or not auto_action_raw["recommend"]:
                action = {"type": "baseline", "value": 0}
                auto_action = {"type": "none", "value": 0.0, "recommend": False}
            else:
                action = {"type": auto_action_raw["type"], "value": float(auto_action_raw["value"])}
                auto_action = {"type": action["type"], "value": action["value"], "recommend": True}
        base = wm.predict_rollout(hist, {"type": "baseline"}, steps=steps)
        acted = wm.predict_rollout(hist, action, steps=steps)
        t0 = float(g["时间戳(ms)"].iloc[H - 1]) if len(g) > H - 1 else 0.0
        dt_ms = 50.0
        return {
            "episode_id": ep_id, "ue": ue, "action": action, "steps": steps,
            "state_cols": cols, "dt_ms": dt_ms, "t0": t0,
            "baseline": base.round(3).tolist(), "acted": acted.round(3).tolist(),
            "auto_action": auto_action,
            "policy": wm.policy_info() if auto_action is not None else None,
            "model": wm.model_info(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"rollout failed: {e}")


# ================= 预测-验证闭环 (11/6 能力交互核心) =================
# 协议: 预测 (GRU-WM rollout) → 执行 (Twin 引擎同 seed 重放, 施加动作) → 验证 (逐 KPI 误差)
# 同根保证: twin_NN episode 由 generate_twin.py 以 seed=1000+ep_i*101 生成,
#           引擎确定性重放与 parquet 存档逐位一致 (已验证), 因此"执行"产出的是真值而非新随机轨迹。

_VERIFY_LOCK = __import__("threading").Lock()
_TWIN_EP = {  # twin episode → (场景, n_ue); 与 scripts/generate_twin.py 的 seed 方案一致
    "twin_01": ("城区", 8), "twin_02": ("城区", 8), "twin_03": ("郊区", 8), "twin_04": ("郊区", 8),
    "twin_05": ("低空", 8), "twin_06": ("低空", 8), "twin_07": ("机器人工厂", 8), "twin_08": ("机器人工厂", 8),
}
_KPI_META = [  # state_cols 顺序: RSRP / SINR / DL / CQI
    {"name": "RSRP", "unit": "dBm"}, {"name": "SINR", "unit": "dB"},
    {"name": "下行速率", "unit": "Mbps"}, {"name": "CQI", "unit": ""},
]


def _replay_twin_branch(ep_id: str, action: dict | None):
    """Twin 引擎同 seed 重放一条分支 (action=None 为基准).
    返回 (ue 表 DataFrame, 仿真器对象). 与 generate_twin.py 完全同构."""
    engine = importlib.import_module("twin.engine")
    scene, n_ue = _TWIN_EP[ep_id]
    ep_i = int(ep_id.split("_")[1])
    tw = engine.TinyTwin(scene, n_cell=3, n_ue=n_ue, seed=1000 + ep_i * 101)
    if action:
        t, v = str(action.get("type", "baseline")), float(action.get("value", 0.0))
        if t == "pmax_scale":
            tw.tx_scale_db = 10.0 * np.log10(max(v, 1e-3))
        elif t == "prb_scale":
            tw.prb_scale = min(max(v, 0.05), 1.0)
        elif t == "az_offset":
            for c in tw.site:
                c["az"] = (c["az"] + v) % 360
        elif t == "ho_offset":
            tw.ho_offset_db = v
        # baseline / 未知类型: 不干预
    return tw.run()["ue"], tw


@app.post("/api/wm/verify")
async def wm_verify(req: dict):
    """预测-验证闭环: {episode_id (twin_NN), ue, action: {type, value}|"auto", steps<=160}
    1. 预测: GRU-WM 对 baseline / action 各 rollout steps 步
    2. 执行: Twin 引擎同 seed 重放两条真值轨 (同根, 动作因果效应精确可分离)
    3. 验证: 逐 KPI 的 rollout RMSE + Δ效应误差 (预测Δ vs 真值Δ)

    action="auto" → 动作模型 (PolicyNet-Fused) 一步决策该时刻动作 (闭环决策演示).
    """
    import time as _time
    ep_id = str(req.get("episode_id", "")).strip()
    if not ep_id:
        raise HTTPException(422, "episode_id 必填 (twin_NN 或数据湖 episode 编号)")
    action = req.get("action") or {"type": "baseline", "value": 0}
    steps = int(min(max(int(req.get("steps", 80)), 1), 160))
    t_w = int(req.get("window_t", -1))          # twin_ep 的时间索引窗 (t+1≥hist 才可预测)
    if ep_id not in _TWIN_EP:
        raise HTTPException(400, f"verify 仅支持 twin episodes: {sorted(_TWIN_EP)} (引擎可确定性重放)")
    try:
        wm = importlib.import_module("twm_serve")
        if wm.load_model() is None:
            raise HTTPException(503, "GRU-WM model not available")
        cols = wm._META["state_cols"]; H = wm._META["hist"]
        df = _ep_ue_df(ep_id)
        ue = int(req.get("ue", df.UEID.iloc[0]))
        g = df[df.UEID == ue].sort_values("时间戳(ms)")
        if len(g) < H + steps:
            raise HTTPException(400, f"episode 长度不足: 需 {H + steps} 步, 实际 {len(g)}")
        if t_w == -1:
            t_w = H - 1   # 默认取首个可预测窗
        if not (H - 1 <= t_w <= len(g) - steps - 1):
            raise HTTPException(400, f"window_t={t_w} 越界: 需 [{H-1}, {len(g)-steps-1}]")
        hist = g[cols].values[t_w - H + 1: t_w + 1].astype(float)

        if isinstance(action, str) and action == "auto":
            act = wm.policy_action(hist)
            if not act["available"] or not act["recommend"]:
                action = {"type": "baseline", "value": 0}
                auto_action = {"type": "none", "value": 0.0, "recommend": False}
            else:
                action = {"type": act["type"], "value": float(act["value"])}
                auto_action = {"type": act["type"], "value": float(act["value"]), "recommend": True}
        else:
            auto_action = None

        t_pred0 = _time.time()
        pred_base = wm.predict_rollout(hist, {"type": "baseline"}, steps=steps)
        pred_act = wm.predict_rollout(hist, action, steps=steps)
        t_pred = (_time.time() - t_pred0) * 1000

        t_exec0 = _time.time()
        with _VERIFY_LOCK:                     # 引擎重放 CPU 密集, 防并发重入
            truth_base_df, _ = _replay_twin_branch(ep_id, None)
            truth_act_df, _ = _replay_twin_branch(ep_id, action)
        t_exec = (_time.time() - t_exec0) * 1000

        gb = truth_base_df[truth_base_df.UEID == ue].sort_values("时间戳(ms)")
        ga = truth_act_df[truth_act_df.UEID == ue].sort_values("时间戳(ms)")
        truth_base = gb[cols].values[t_w + 1: t_w + 1 + steps].astype(float)
        truth_act = ga[cols].values[t_w + 1: t_w + 1 + steps].astype(float)

        kpis = []
        for i, km in enumerate(_KPI_META):
            pb, pa = pred_base[:, i], pred_act[:, i]
            tb, ta = truth_base[:, i], truth_act[:, i]
            pp = np.full(steps, float(hist[-1, i]))   # Persistence 参考: 末值保持 (无动作感知, Δ=0)
            d_pred = float((pa - pb).mean()); d_truth = float((ta - tb).mean())
            kpis.append({
                "name": km["name"], "unit": km["unit"],
                "rmse_base": round(float(np.sqrt(((pb - tb) ** 2).mean())), 3),
                "rmse_act": round(float(np.sqrt(((pa - ta) ** 2).mean())), 3),
                "rmse_persist": round(float(np.sqrt(((pp - tb) ** 2).mean())), 3),
                "delta_pred": round(d_pred, 3),      # 预测的动作平均效应
                "delta_truth": round(d_truth, 3),     # 真值动作平均效应
                "delta_err": round(float(abs(d_pred - d_truth)), 3),
                "pred_base": np.round(pb, 3).tolist(),
                "pred_act": np.round(pa, 3).tolist(),
                "truth_base": np.round(tb, 3).tolist(),
                "truth_act": np.round(ta, 3).tolist(),
            })

        t0 = float(g["时间戳(ms)"].iloc[t_w])
        return {
            "episode_id": ep_id, "ue": ue, "action": action, "steps": steps,
            "window_t": t_w, "scene": _TWIN_EP[ep_id][0], "dt_ms": 50.0, "t0": t0,
            "auto_action": auto_action,
            "kpis": kpis,
            "timing_ms": {"predict": round(t_pred, 1), "execute": round(t_exec, 1)},
            "model": wm.model_info(),
            "policy": wm.policy_info() if auto_action else None,
            "protocol": ("预测 (GRU-WM 多步自回归 rollout) → 执行 (Tiny-Twin 引擎同 seed 重放, "
                         "同根分支真值) → 验证 (逐 KPI RMSE + Δ效应误差)"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"verify failed: {e}")


@app.post("/api/wm/action")
async def wm_action(req: dict):
    """动作模型 (PolicyNet-Fused) 一步决策: {episode_id, ue?, window_t?}
    返回推荐的干预动作 (type/value) + 该时刻监督信号 (auto-shadow, 真实 Twin 仿真对齐)
    """
    wm = importlib.import_module("twm_serve")
    if wm.load_policy() is None:
        raise HTTPException(503, "动作模型不可用 (policy_fused_net.pt 未找到或加载失败)")
    ep_id = str(req.get("episode_id", "")).strip()
    if not ep_id:
        raise HTTPException(422, "episode_id 必填 (twin_NN 或数据湖 episode 编号)")
    df = _ep_ue_df(ep_id)
    if df.empty:
        raise HTTPException(404, f"episode {ep_id} not found")
    cols = wm._POLICY_META["state_cols"]; H = wm._POLICY_META["hist"]
    ue = int(req.get("ue", df.UEID.iloc[0]))
    g = df[df.UEID == ue].sort_values("时间戳(ms)")
    t_w = int(req.get("window_t", len(g) - 1))
    if not (H - 1 <= t_w < len(g)):
        raise HTTPException(400, f"window_t={t_w} 越界: 需 [{H-1}, {len(g)-1}]")
    hist = g[cols].values[t_w - H + 1: t_w + 1].astype(float)
    a = wm.policy_action(hist)
    return {
        "episode_id": ep_id, "ue": ue, "window_t": t_w,
        "t_ms": float(g["时间戳(ms)"].iloc[t_w]),
        "action": {"type": a["type"], "value": float(a["value"] or 0.0)},
        "recommend": a["recommend"], "hist": hist.round(2).tolist(),
        "policy": wm.policy_info(),
    }


# ================= 逐小区协同策略 (Coord Policy, 技术报告 §7.5) =================

_COORD_SCENES = ("城区", "郊区", "低空", "机器人工厂", "campus")


@app.post("/api/coord/action")
async def coord_action(req: dict):
    """逐小区协同策略一步决策: {scene, sinr_c: [3], n_ue_c: [3]}
    值头 argmax 部署 (与 train_coord_policy.py / LOSO 评测一致),
    返回推荐功率偏移 (p0,p1,p2) + 125 组合值序 top-k + 模型元信息.
    """
    if req.get("action") == "auto":
        raise HTTPException(422, "coord 策略不支持 action='auto' (该入口属于 /api/wm/rollout);"
                                 "请提供 {scene, sinr_c, n_ue_c}")
    cs = importlib.import_module("coord_serve")
    if cs.load_coord_policy() is None:
        raise HTTPException(503, "协同策略不可用 (coord_policy.pt 未找到或加载失败)")
    scene = str(req.get("scene", "")).strip()
    if scene not in _COORD_SCENES:
        raise HTTPException(422, f"scene 必须为 {_COORD_SCENES} 之一, 收到: {scene!r}")
    try:
        sinr_c = [float(x) for x in req["sinr_c"]]
        n_ue_c = [int(x) for x in req["n_ue_c"]]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "sinr_c / n_ue_c 必填且为各 3 个数值的列表")
    if len(sinr_c) != 3 or len(n_ue_c) != 3:
        raise HTTPException(422, "sinr_c / n_ue_c 必须各为 3 个小区的值")
    if any(not np.isfinite(v) for v in sinr_c):
        raise HTTPException(422, "sinr_c 含非有限值 (NaN/Inf)")
    if any(n < 0 for n in n_ue_c):
        raise HTTPException(422, "n_ue_c 不能为负")
    if not all(-30.0 <= v <= 50.0 for v in sinr_c):
        raise HTTPException(422, "sinr_c 超出合理量程 [-30, 50] dB (校准数据范围)")
    if any(n > 500 for n in n_ue_c):
        raise HTTPException(422, "n_ue_c 超出合理量程 [0, 500]")
    try:
        rec = cs.coord_policy_action(sinr_c, n_ue_c, scene)
    except Exception as e:
        raise HTTPException(500, f"coord 推理失败: {e}")
    rec.update({"policy": cs.coord_policy_info(), "scene": scene})
    return rec


@app.get("/api/coord/info")
def coord_info():
    """协同策略模型元信息 (参数量/动作空间/LOSO 摘要), 不可用时 available=false"""
    cs = importlib.import_module("coord_serve")
    return cs.coord_policy_info()


# ================= 园区地图 (中国移动信息港·昌平) =================

@app.get("/api/campus")
def campus():
    """园区 OSM 数据: 建筑/道路/锚点 (ENU 米制, 原点=园区西南角)"""
    try:
        cp = importlib.import_module("twin.campus")
        return cp.campus_payload()
    except Exception as e:
        raise HTTPException(500, f"campus payload failed: {e}")


@app.post("/api/chat")
async def chat(req: dict):
    """工作台对话: 基于数据湖的规则问答 (无需外部 LLM 也可演示).
    支持: 指标查询 / episode 对比 / 五层解释 / KPI 极值"""
    q_text = str(req.get("q", "")).strip()
    if not q_text:
        return {"reply": "请输入问题。可问: 「城区平均RSRP」「低空的SINR对比」「五层是什么」「twin_01 吞吐」"}
    low = q_text.lower()
    try:
        # 五层解释
        if "五层" in q_text or "5层" in q_text or "层" in q_text and any(k in q_text for k in LAYER_DOCS):
            hits = [k for k in LAYER_DOCS if k in q_text]
            if hits:
                d = LAYER_DOCS[hits[0]]
                return {"reply": f"「{hits[0]}」层 → 对应能力: {d['能力']}。底座内容: {d['内容']}。本工作台渲染: {d['渲染']}。"}
            lines = [f"{i+1}. {k} → {v['能力']}: {v['内容'][:30]}…" for i, (k, v) in enumerate(LAYER_DOCS.items())]
            return {"reply": "五层数据结构:\n" + "\n".join(lines)}
        # episode 定位 (数字或 twin_xx)
        import re as _re
        ep_m = _re.search(r"(twin_\d+|(?<!\d)\d{1,2}(?!\d))", low)
        ep_id = ep_m.group(1) if ep_m else None
        # KPI 关键词
        kpi_map = {"rsrp": ("服务小区RSRP(dBm)", "RSRP", "dBm"),
                   "sinr": ("服务小区SINR(dB)", "SINR", "dB"),
                   "吞吐": ("下行速率(Mbps)", "下行速率", "Mbps"),
                   "速率": ("下行速率(Mbps)", "下行速率", "Mbps"),
                   "时延": ("端到端时延(ms)", "端到端时延", "ms"),
                   "cqi": ("CQI", "CQI", "")}
        kpi_hit = next((v for k, v in kpi_map.items() if k in low), None)
        if kpi_hit:
            col, name, unit = kpi_hit
            if ep_id:
                dfe = _ep_ue_df(ep_id)
                if dfe.empty:
                    return {"reply": f"未找到 episode {ep_id}"}
                import numpy as _np2
                v = dfe[col].astype(float)
                return {"reply": (f"episode {ep_id} 的{name}: 均值 {v.mean():.1f}{unit}, "
                                  f"P10 {_np2.percentile(v,10):.1f}, 中位 {v.median():.1f}, "
                                  f"P90 {_np2.percentile(v,90):.1f}, 最大 {v.max():.1f} (n={len(v):,})")}
            # 未指明 episode → 用当前选中 episode, 否则回退 real/首个
            ep_cur = str(req.get("ep", "")).strip()
            eps = episodes()
            if not ep_cur:
                ep_cur = next((e["id"] for e in eps if e["source"] == "real"), eps[0]["id"] if eps else None)
            if ep_cur is None:
                return {"reply": f"数据湖中还没有 episode, 无法查询{name}"}
            dfe = _ep_ue_df(ep_cur)
            if dfe.empty:
                return {"reply": f"请指明 episode, 如「1 的{name}」或「twin_01 的{name}」"}
            import numpy as _np2
            v = dfe[col].astype(float)
            return {"reply": (f"episode {ep_cur} 的{name}: 均值 {v.mean():.1f}{unit}, "
                              f"P10 {_np2.percentile(v,10):.1f}, 中位 {v.median():.1f}, "
                              f"P90 {_np2.percentile(v,90):.1f}, 最大 {v.max():.1f} (n={len(v):,})")}
        # 默认: episode 概览
        if ep_id:
            eps = episodes()
            hit = next((e for e in eps if e["id"] == ep_id), None)
            if hit:
                return {"reply": (f"episode {ep_id}: 场景 {hit['scene']}, {hit['n_ue']} 终端, "
                                  f"平均 RSRP {hit['rsrp']}dBm, SINR {hit['sinr']}dB, "
                                  f"下行 {hit['dl']}Mbps (来源: {hit['source']})")}
        eps = episodes()
        real = [e for e in eps if e["source"] == "real"]
        return {"reply": (f"共 {len(eps)} 个 episode (真实 {len(real)} + Twin {len(eps)-len(real)})。"
                          "可问: 「城区平均RSRP」「twin_01 吞吐」「五层是什么」。")}
    except Exception as e:
        return {"reply": f"查询出错: {e}"}


# ---- 前端静态页 ----
@app.get("/")
def index():
    return FileResponse(Path(__file__).resolve().parents[1] / "frontend" / "index.html")


@app.get("/workbench")
def workbench():
    """3D 仿真工作台 (信息港): 五层渲染 + 右侧对话/指标 + hover 详情"""
    return FileResponse(Path(__file__).resolve().parents[1] / "frontend" / "workbench.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8811)
