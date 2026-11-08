#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多小区协同动作数据集 (Coordination Dataset, v2 多 seed 稳健版)
================================================================
背景: 全局动作 (pmax/az/ho 全站同步施加) 无法表达小区级差异。本脚本在
校准孪生 (twin/calibrated.py, per-cell hooks: cell_tx_scale_db) 上生成
逐小区功率偏移组合的同根分支数据:

  - 动作空间: 每小区功率偏移 p_i ∈ {−6, −3, 0, +3, +6} dB,
    5³=125 组合全枚举;
  - 同根分支: 同 scene+seed 下 UE 几何/运动/阴影随机流一致, 仅功率组合
    不同 → Δobj 无混淆 (与 twin_action_dataset 同一根纪律);
  - 多 seed (v2, round 176): 每场景 5 个 seed (主 seed 与
    generate_actions.py 对齐 9000+si*13, 附加 seed = 主 seed + 1000×k,
    k=1..4, 同根几何多样本), 验证 round-175 协同增益跨 seed 稳定性;
  - 逐小区特征 (v2): sinr_c0/c1/c2 (各小区服务 UE 的均值 SINR)、
    n_ue_c0/c1/c2 (各小区服务 UE 数) — 为逐小区策略网络 (coord policy)
    提供小区级观测输入;
  - 判据 (协同价值标签): obj = mean_ue_sinr + 0.5 × min_ue_sinr
    (吞吐与边缘公平帕累托加权, 与报告 §7 协同实验一致)。
  - 产出: coord_action_dataset.parquet
      scene, seed, combo (如 "+3,0,-6"), p0, p1, p2, obj, mean_sinr,
      min_sinr, mean_rsrp, mean_dl, mean_cqi, sinr_c0..2, n_ue_c0..2
    (v2: 5 场景 × 5 seed × 125 组合 = 3,125 行 × 20 列)
    兼容性: v1 单 seed 数据集 (625 行) 为主 seed 子集, 逐位一致。

判别实验设计 (grid search 证明联合 > 全局):
  等能耗预算 Σ|p_i| 下, 逐小区联合最优 vs 全局同步最优的 obj 差,
  per-seed 计算 → 跨 seed 均值±标准差 = 协同增益的稳健估计。
  参考 (round-175, v1 单 seed): 机器人工厂 +0.64 / campus +0.05;
  城区/郊区/低空最优解恰为全局动作 (联合搜索无损失)。
"""
import sys
import argparse
import itertools
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twin.calibrated import TinyTwin           # noqa: E402
from datalake.config import GEN_DIR            # noqa: E402

SCENES = ["campus", "城区", "郊区", "低空", "机器人工厂"]
GRID = [-6.0, -3.0, 0.0, 3.0, 6.0]

# 主 seed 与 generate_actions.py / round-175 v1 数据集对齐 (9000 + i*13);
# 附加 seed 用 +1000×k 间隔, 与主 seed 随机流无重叠风险 (default_rng 流
# 由种子唯一决定, 间隔 1000 保证与场景间 13 间隔主 seed 族不相交)。
N_SEEDS = 5
SEED_OFFSETS = [0, 1000, 2000, 3000, 4000]


def seeds_of(scene: str) -> list[int]:
    """场景的多 seed 列表: 主 seed 在前 (v1 兼容), 后接 4 个附加 seed。"""
    i = SCENES.index(scene)
    base = 9000 + i * 13
    return [base + off for off in SEED_OFFSETS[:N_SEEDS]]


def run_combo(scene: str, seed: int, combo: tuple[float, float, float]):
    """运行一条逐小区功率组合分支 (同根: 同 seed)。

    返回全局 KPI + 逐小区特征 (v2): 各小区服务 UE 的均值 SINR 与 UE 数。
    """
    tw = TinyTwin(scene, n_cell=3, n_ue=8, seed=seed)
    if any(x != 0.0 for x in combo):
        tw.cell_tx_scale_db = list(combo)
    ue = tw.run()["ue"]
    per_ue = ue.groupby("UEID")["服务小区SINR(dB)"].mean()
    # 逐小区特征: 按 服务小区ID 分组 (campus-N / 城区-N ...), 取 UE 级均值后再聚合
    cell_of = {c["cell_name"]: c["idx"] for c in tw.site}
    ue_per_cell = ue.groupby("UEID").agg(
        sinr=("服务小区SINR(dB)", "mean"),
        cell=("服务小区ID", "first"),
    )
    feats = {}
    for ci in range(3):
        sub = ue_per_cell[ue_per_cell["cell"].map(cell_of) == ci]
        feats[f"sinr_c{ci}"] = float(sub["sinr"].mean()) if len(sub) else 0.0
        feats[f"n_ue_c{ci}"] = int(len(sub))
    return {
        "obj": float(per_ue.mean() + 0.5 * per_ue.min()),
        "mean_sinr": float(ue["服务小区SINR(dB)"].mean()),
        "min_sinr": float(per_ue.min()),
        "mean_rsrp": float(ue["服务小区RSRP(dBm)"].mean()),
        "mean_dl": float(ue["下行速率(Mbps)"].mean()),
        "mean_cqi": float(ue["CQI"].mean()),
        **feats,
    }


def summarize_scene(df: pd.DataFrame) -> dict:
    """单 seed 分支组内: 基线 / 全局最优 / 联合最优 / 协同增益。"""
    b = df[df.combo == "+0,+0,+0"].iloc[0]
    j = df.loc[df.obj.idxmax()]
    gdf = df[(df.p0 == df.p1) & (df.p1 == df.p2)]
    g = gdf.loc[gdf.obj.idxmax()]
    # 平铺字段 (便于 DataFrame/json 直读, 勿嵌套 dict)
    return {
        "baseline_obj": float(b.obj),
        "global_combo": str(g.combo),
        "global_obj": float(g.obj),
        "joint_combo": str(j.combo),
        "joint_obj": float(j.obj),
        "coordination_gain": float(j.obj - g.obj),
    }


def main():
    global N_SEEDS
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", nargs="*", default=SCENES)
    ap.add_argument("--n-seeds", type=int, default=N_SEEDS,
                    help="每场景 seed 数 (1=v1 单 seed 兼容, 默认 5)")
    args = ap.parse_args()

    N_SEEDS = max(1, args.n_seeds)

    t0 = time.time()
    combos = list(itertools.product(GRID, repeat=3))
    rows = []
    for scene in args.scenes:
        for seed in seeds_of(scene):
            for combo in combos:
                m = run_combo(scene, seed, combo)
                rows.append({
                    "scene": scene, "seed": seed,
                    "combo": ",".join(f"{x:+g}" for x in combo),
                    "p0": combo[0], "p1": combo[1], "p2": combo[2],
                    **m,
                })
            df = pd.DataFrame([r for r in rows
                               if r["scene"] == scene and r["seed"] == seed])
            s = summarize_scene(df)
            print(f"[{scene} seed={seed}] 125 组合 {time.time()-t0:.0f}s | "
                  f"baseline {s['baseline_obj']:.3f} | "
                  f"全局最优 {s['global_combo']} {s['global_obj']:.3f} | "
                  f"联合最优 {s['joint_combo']} {s['joint_obj']:.3f} | "
                  f"协同增益 {s['coordination_gain']:+.3f}")

    out = pd.DataFrame(rows)
    path = GEN_DIR / "coord_action_dataset.parquet"
    out.to_parquet(path, index=False, compression="zstd")
    print(f"\n[coord_actions] {len(out)} 行 ({out['seed'].nunique()} seeds/场景 "
          f"× {len(combos)} 组合 × {len(args.scenes)} 场景) → {path}")

    # ---- 跨 seed 稳健性汇总 (v2) ----
    per_seed = []
    for (scene, seed), df in out.groupby(["scene", "seed"]):
        per_seed.append({"scene": scene, "seed": seed,
                         **summarize_scene(df)})
    ps = pd.DataFrame(per_seed)
    print("\n[coord_actions] 跨 seed 协同增益稳健性 (mean ± std, per-seed):")
    for scene, g in ps.groupby("scene"):
        gains = g["coordination_gain"]
        win = (g["joint_combo"] == g["global_combo"]).mean()
        print(f"  {scene:8s} 增益 {gains.mean():+.3f} ± {gains.std():.3f} "
              f"(min {gains.min():+.3f}, max {gains.max():+.3f}) | "
              f"联合==全局 比例 {win:.0%}")
    ps.to_json(GEN_DIR / "coord_seed_robustness.json",
               orient="records", force_ascii=False, indent=1)


if __name__ == "__main__":
    main()
