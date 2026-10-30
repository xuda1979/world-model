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
import sys, json
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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


# ---- 前端静态页 ----
@app.get("/")
def index():
    return FileResponse(Path(__file__).resolve().parents[1] / "frontend" / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8811)
