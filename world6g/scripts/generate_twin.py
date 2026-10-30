#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tiny-Twin 批量生成: 4 场景 × N episodes 仿真数据 -> datalake/generated/
与真实 10 episodes 同 schema, 可在可视化中叠加对比。
每 episode 生成后立即校准报告 (RSRP/SINR/DL vs 真实同场景 episode 值域)。

用法: python3 scripts/generate_twin.py [--n-per-scene 1] [--seed 1000]
"""
import sys, json, argparse
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb

from datalake.config import DB_PATH, GEN_DIR
from twin.engine import TinyTwin

SCENES = ["城区", "郊区", "低空", "机器人工厂"]
UE_COUNTS = {"城区": 8, "郊区": 8, "低空": 8, "机器人工厂": 8}


def real_scene_stats(con) -> dict:
    """从湖中取每场景真实 episode 的 KPI 值域 (校准目标)"""
    df = con.execute("""
        SELECT (SELECT 描述 FROM map WHERE 字段='类型' AND episode_id=u.episode_id) scene,
               u.episode_id eid,
               AVG(u."服务小区RSRP(dBm)") rsrp, AVG(u."服务小区SINR(dB)") sinr,
               AVG(u."下行速率(Mbps)") dl
        FROM ue u GROUP BY u.episode_id
    """).fetchdf()
    out = {}
    for sc, g in df.groupby("scene"):
        key = "城区" if "城区" in sc else ("郊区" if "郊区" in sc else ("低空" if "低空" in sc else "机器人工厂"))
        for _, row in g.iterrows():
            out.setdefault(key, []).append(
                {"episode": int(row.eid), "rsrp": float(row.rsrp),
                 "sinr": float(row.sinr), "dl": float(row.dl)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-scene", type=int, default=1)
    ap.add_argument("--seed", type=int, default=1000)
    args = ap.parse_args()

    GEN_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    real_stats = real_scene_stats(con)
    con.close()

    report = []
    ep_i = 0
    for scene in SCENES:
        for k in range(args.n_per_scene):
            ep_i += 1
            seed = args.seed + ep_i * 101
            tw = TinyTwin(scene, n_cell=3, n_ue=UE_COUNTS[scene], seed=seed)
            tabs = tw.run()
            for t, df in tabs.items():
                df["episode_id"] = f"twin_{ep_i:02d}"
                pq = GEN_DIR / f"twin_{ep_i:02d}_{t}.parquet"
                df.to_parquet(pq, index=False)
            ue = tabs["ue"]
            rr = [r for r in real_stats.get(scene, [])]
            row = {
                "gen_episode": f"twin_{ep_i:02d}", "scene": scene,
                "rsrp_mean": round(float(ue["服务小区RSRP(dBm)"].mean()), 1),
                "sinr_mean": round(float(ue["服务小区SINR(dB)"].mean()), 1),
                "dl_mean": round(float(ue["下行速率(Mbps)"].mean()), 1),
                "real_ref": rr,
            }
            report.append(row)
            ok_rsrp = any(r["rsrp"] - 8 <= row["rsrp_mean"] <= r["rsrp"] + 8 for r in rr)
            print(f"[twin_{ep_i:02d}] {scene:6s} RSRP={row['rsrp_mean']:7.1f} "
                  f"SINR={row['sinr_mean']:5.1f} DL={row['dl_mean']:6.1f} "
                  f"(真实值域 RSRP [{min(r['rsrp'] for r in rr):.1f},{max(r['rsrp'] for r in rr):.1f}] "
                  f"SINR [{min(r['sinr'] for r in rr):.1f},{max(r['sinr'] for r in rr):.1f}]) "
                  f"{'✓' if ok_rsrp else '⚠'}")

    (GEN_DIR / "twin_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n{ep_i} 个 twin episode 生成完毕 -> {GEN_DIR}")


if __name__ == "__main__":
    main()
