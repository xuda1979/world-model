#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETL: 10 episodes 真实仿真数据 -> Parquet (curated) + DuckDB 注册
====================================================
数据底座第一步 (方案一)。产出:
  datalake/curated/<table>.parquet   — 全 10 episodes 合并, 带 episode_id 列, AI 就绪
  datalake/lake.duckdb               — 查询层: 每表一个视图 + episodes 元数据表
  datalake/raw/registry.json         — 源数据指纹 (可追溯)

用法: python3 scripts/ingest.py [--episodes 1 2 3]
"""
import sys, json, hashlib, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import duckdb

from datalake.config import (EPISODES, EP_TABLES, CURATED, DB_PATH, RAW_DIR, STATS_JSON)


def sha256_of(path: Path, limit_mb=8) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        data = f.read(limit_mb * 1024 * 1024)
    h.update(data)
    return h.hexdigest()[:16]


def load_episode(ep_id: int):
    """读一个 episode 的全部表, 返回 {table: DataFrame}, 加 episode_id 列"""
    edir = EPISODES / f"episode_{ep_id}"
    assert edir.exists(), f"missing {edir}"
    out = {}
    for key, (zh_name, rel) in EP_TABLES.items():
        f = edir / rel
        if not f.exists():
            continue
        # 场景地图/NRM/CSI 说明是 字段-值 竖表, 统一为 key-value
        if key in ("map", "csi"):
            df = pd.read_csv(f)
            df["episode_id"] = ep_id
            out[key] = df
            continue
        if key == "nrm":
            # NRM-CM: 首行为 对象类型-> 映射行, 实际表头在第 2 行
            df = pd.read_csv(f, header=1)
            df["episode_id"] = ep_id
            out[key] = df
            continue
        df = pd.read_csv(f)
        df["episode_id"] = ep_id
        out[key] = df
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, nargs="*", default=list(range(1, 11)))
    args = ap.parse_args()

    CURATED.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 1. 收集 ----
    merged = {}
    registry = {"source_root": str(EPISODES), "episodes": {}, "tables": {}}
    for ep in args.episodes:
        tables = load_episode(ep)
        registry["episodes"][ep] = {
            t: {"rows": len(tables[t]), "sha16": sha256_of(EPISODES / f"episode_{ep}" / rel)}
            for t, (zh, rel) in EP_TABLES.items() if t in tables
        }
        for t, df in tables.items():
            merged.setdefault(t, []).append(df)
        print(f"episode_{ep}: {len(tables)} tables, "
              + ", ".join(f"{t}={len(df)}" for t, df in sorted(tables.items())))

    # ---- 2. 写 Parquet (curated 层) ----
    curated_tables = {}
    for t, frames in merged.items():
        df = pd.concat(frames, ignore_index=True)
        pq = CURATED / f"{t}.parquet"
        df.to_parquet(pq, index=False, engine="pyarrow", compression="zstd")
        curated_tables[t] = df
        registry["tables"][t] = {"rows": len(df), "cols": len(df.columns),
                                 "parquet": str(pq)}
        print(f"[curated] {t:10s} {len(df):>7,} rows x {len(df.columns):>3} cols -> {pq.name}")

    # ---- 3. DuckDB 查询层 ----
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    for t, df in curated_tables.items():
        pq = CURATED / f"{t}.parquet"
        # 直接注册 parquet 为表 (零拷贝)
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{pq}');")
    # episodes 元数据
    meta = pd.DataFrame([
        {"episode_id": ep,
         "rows": sum(v["rows"] for v in info.values()),
         **{f"has_{t}": t in info for t in EP_TABLES}}
        for ep, info in registry["episodes"].items()])
    con.execute("CREATE TABLE episodes AS SELECT * FROM meta_df",
                ) if False else con.register("_meta", meta)
    con.execute("CREATE TABLE episodes AS SELECT * FROM _meta")
    con.unregister("_meta")

    # 快速冒烟查询
    n_ue = con.execute("SELECT COUNT(*) FROM ue").fetchone()[0]
    n_cells = con.execute("SELECT COUNT(DISTINCT 服务小区ID) FROM ue").fetchone()[0]
    print(f"\n[duckdb] lake.duckdb: ue rows={n_ue:,}, distinct serving cells={n_cells}")

    # ---- 4. 源指纹 (可追溯) ----
    anchors = json.load(open(STATS_JSON))
    registry["real_anchors"] = {
        "mro_serving_rsrp_dbm": anchors["mro"]["MR.NRScSSRSRP"],
        "mro_serving_sinr_db": anchors["mro"]["MR.NRScSSSINR"],
        "mro_serving_rsrq_db": anchors["mro"]["MR.NRScSSRSRQ"],
        "n_real_cells_grid6": len(anchors["cells"]),
        "n_real_sites_grid6": anchors["n_sites"],
    }
    (RAW_DIR / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=1))
    print("[raw] registry.json written (source fingerprints + real anchors)")

    con.close()
    print("\nDONE. 数据底座 curated 层就绪。")


if __name__ == "__main__":
    main()
