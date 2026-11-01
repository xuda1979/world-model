#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国移动信息港 (昌平) 园区地图模块
====================================
方案一 §三.2: "从已有或授权地图导入建筑轮廓、高度、地形和天线参数, 先做一个小区域"
地图选点: 中国移动国际信息港 (昌平区沙河镇/马池口镇, 未来科学城西区, 京藏高速 G6 以西)

数据来源 (每条均带 source 标注, 方案一 §四: "每张结果卡常驻标识来源"):
  - OSM Overpass API (ODbL) — 建筑轮廓/道路 (datalake/raw/campus/*.json)
  - Bing Maps POI — 一期/二期/6期 园区锚点 (经纬度)
  - 缺失建筑高度 → 按 building 类型假设 (方案一: "缺失高度、材料或方向图采用明确假设和有界扰动")

投影: WGS84 → 本地 ENU 米制 (easting=x 东, northing=y 北), 原点=园区中心
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

PKG_DIR = Path(__file__).resolve().parents[1]
RAW_CAMPUS = PKG_DIR / "datalake" / "raw" / "campus"

# ---- 园区锚点 (Bing Maps POI, 2026-10-31 查证) ----
ANCHORS = {
    "信息港一期": {"lat": 40.14987, "lon": 116.22982,
                    "note": "满白路与东坨村路交叉口西北440m (数据中心/机房/实验室, 一期6.5万m²)"},
    "信息港二期": {"lat": 40.15097, "lon": 116.22635,
                    "note": "移动信息港二期西门西南110m (研发办公)"},
    "信息港6期": {"lat": 40.15462, "lon": 116.24206,
                   "note": "能源西路2号 (科技创新产业园)"},
}
# 园区 ENU 原点 (中心)
ORIGIN_LAT = 40.1515
ORIGIN_LON = 116.2335

# 高度假设 (m): OSM 缺失 height/building:levels 时按类型假设
# 方案一: "缺失高度、材料或方向图采用明确假设和有界扰动, 记录其对误差的影响"
DEFAULT_HEIGHTS = {
    "yes": 24.0,          # 园区综合建筑 (数据中心机房群, 含 id=1290441818 56,000m² 大底座)
    "industrial": 18.0,   # 工业/机房
    "commercial": 15.0,   # 商业配套
    "university": 12.0,   # 研发/学术
    "residential": 18.0,  # 6层住宅
    "apartments": 18.0,
    "house": 6.0,
}
DEFAULT_HEIGHT = 15.0

# 大型关键建筑的显式高度 (来自 OSM 或园区公开资料; 其余走类型假设)
EXPLICIT_HEIGHTS = {901847854: 20.0}   # OSM height=20 标注


# ---------------------------------------------------------------- 投影
_KX = 111320.0 * math.cos(math.radians(ORIGIN_LAT))   # m / deg lon
_KY = 110540.0                                        # m / deg lat


def lonlat_to_xy(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 → 园区 ENU (米). x=东, y=北"""
    return (lon - ORIGIN_LON) * _KX, (lat - ORIGIN_LAT) * _KY


def xy_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return ORIGIN_LON + x / _KX, ORIGIN_LAT + y / _KY


# ---------------------------------------------------------------- 数据类
@dataclass
class CampusBuilding:
    id: int
    poly: list[tuple[float, float]]    # ENU 多边形 [(x,y), ...] 闭合
    height_m: float
    btype: str
    area_m2: float
    source_height: str                  # "osm" / "假设(类型)" / "假设(默认)"
    osm_id: int | None = None


@dataclass
class CampusRoad:
    name: str
    kind: str
    line: list[tuple[float, float]]     # ENU 折线


@dataclass
class Campus:
    name: str = "中国移动信息港 (昌平)"
    origin_lat: float = ORIGIN_LAT
    origin_lon: float = ORIGIN_LON
    buildings: list[CampusBuilding] = field(default_factory=list)
    roads: list[CampusRoad] = field(default_factory=list)
    anchors: dict = field(default_factory=lambda: dict(ANCHORS))
    sources: dict = field(default_factory=lambda: {
        "buildings": "OpenStreetMap Overpass API (ODbL), 2026-10-30 快照",
        "roads": "OpenStreetMap Overpass API (ODbL), 2026-10-30 快照",
        "anchors": "Bing Maps POI (一期/二期/6期 经纬度), 2026-10-31 查证",
        "heights": "OSM 标注优先; 缺失按建筑类型假设 (标注于每栋 building.source_height)",
        "coverage_note": "OSM 对园区建筑覆盖不全 (65 栋); 未数字化建筑不参与传播遮挡, "
                         "误差影响按方案一以假设+有界扰动处理",
    })


# ---------------------------------------------------------------- 加载
def _poly_area(poly) -> float:
    a = 0.0
    for i in range(len(poly) - 1):
        a += poly[i][0] * poly[i + 1][1] - poly[i + 1][0] * poly[i][1]
    return abs(a) / 2.0


def load_campus() -> Campus:
    """加载园区地图 (OSM raw → ENU 投影). 缓存于模块级."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    c = Campus()

    # 建筑
    bf = RAW_CAMPUS / "campus_bldgs.json"
    if bf.exists():
        data = json.loads(bf.read_text())
        for e in data.get("elements", []):
            geom = e.get("geometry", [])
            if e.get("type") != "way" or len(geom) < 3:
                continue
            tags = e.get("tags", {})
            btype = tags.get("building", "yes")
            poly = [lonlat_to_xy(g["lon"], g["lat"]) for g in geom]
            if poly[0] != poly[-1]:
                poly.append(poly[0])
            osm_h = tags.get("height")
            if osm_h:
                h = float(str(osm_h).split()[0]); src = "osm"
            elif e["id"] in EXPLICIT_HEIGHTS:
                h = EXPLICIT_HEIGHTS[e["id"]]; src = "osm"
            elif btype in DEFAULT_HEIGHTS:
                h = DEFAULT_HEIGHTS[btype]; src = f"假设(类型:{btype})"
            else:
                h = DEFAULT_HEIGHT; src = "假设(默认)"
            c.buildings.append(CampusBuilding(
                id=e["id"], poly=poly, height_m=h, btype=btype,
                area_m2=_poly_area(poly), source_height=src, osm_id=e["id"]))

    # 道路
    rf = RAW_CAMPUS / "campus_roads.json"
    if rf.exists():
        data = json.loads(rf.read_text())
        for e in data.get("elements", []):
            geom = e.get("geometry", [])
            if e.get("type") != "way" or not geom:
                continue
            tags = e.get("tags", {})
            c.roads.append(CampusRoad(
                name=tags.get("name", tags.get("highway", "路")),
                kind=tags.get("highway", "?"),
                line=[lonlat_to_xy(g["lon"], g["lat"]) for g in geom]))

    _CACHE = c
    return c


_CACHE: Campus | None = None


# ---------------------------------------------------------------- 输出
def campus_payload() -> dict:
    """/api/campus 载荷: 建筑多边形/道路/锚点 (ENU 米制, 前端直接绘制)"""
    c = load_campus()
    return {
        "name": c.name,
        "origin": {"lat": c.origin_lat, "lon": c.origin_lon},
        "anchors": [{"name": k, "lat": v["lat"], "lon": v["lon"],
                     "xy": list(lonlat_to_xy(v["lon"], v["lat"])), "note": v["note"]}
                    for k, v in c.anchors.items()],
        "buildings": [{
            "id": b.id, "btype": b.btype, "h": b.height_m,
            "area": round(b.area_m2, 1), "source_height": b.source_height,
            "poly": [[round(p[0], 1), round(p[1], 1)] for p in b.poly],
        } for b in c.buildings],
        "roads": [{
            "name": r.name, "kind": r.kind,
            "line": [[round(p[0], 1), round(p[1], 1)] for p in r.line],
        } for r in c.roads],
        "sources": c.sources,
        "n_buildings": len(c.buildings),
        "n_roads": len(c.roads),
    }


def buildings_df() -> pd.DataFrame:
    """建筑表 (调试/训练特征用): id, btype, h, area, cx, cy, source_height"""
    c = load_campus()
    rows = []
    for b in c.buildings:
        cx = sum(p[0] for p in b.poly) / len(b.poly)
        cy = sum(p[1] for p in b.poly) / len(b.poly)
        rows.append({"id": b.id, "btype": b.btype, "h": b.height_m,
                     "area": round(b.area_m2, 1), "cx": round(cx, 1),
                     "cy": round(cy, 1), "source_height": b.source_height})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    p = campus_payload()
    print(f"园区: {p['name']} | 原点 ({p['origin']['lat']}, {p['origin']['lon']})")
    print(f"建筑 {p['n_buildings']} 栋, 道路 {p['n_roads']} 条, 锚点 {len(p['anchors'])}")
    df = buildings_df()
    print(df.sort_values("area", ascending=False).head(8).to_string(index=False))
    # 校验: 锚点投影
    for a in p["anchors"]:
        print(f"  锚点 {a['name']}: ENU=({a['xy'][0]:.0f}, {a['xy'][1]:.0f}) m")
