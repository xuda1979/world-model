#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时序协同 MDP 数据集 (Coordination Sequences, M11)
==================================================
在时序化仿真原语 (twin/calibrated.py: tick_ms + _step_tick + 时变
cell_tx_scale_db) 上生成跨决策点的时变功率决策序列:

  - 仿真 tick = 200ms (每 tick 8 UE 行), 决策间隔 = 1s (5 tick = 40 行/窗):
    每 episode (ep_s=10) → 10 个决策点, 动作在决策点施加, 自下一
    tick 生效 (因果无泄漏, 由 test_twin_temporal 锁定)。
  - 动作空间: 与 coord_actions 同格点 p_i ∈ {-6,-3,0,+3,+6} dB
    (per-cell 功率偏移, 时变: 每决策点可独立变更)。
  - 状态 x_t (决策时可知, 无泄漏): 当前窗逐小区特征
    sinr_c0..2 / n_ue_c0..2 + 全局 mean_sinr + L3 滤波边距
    l3_margin_c0..2 (服务小区 vs 最强邻区, 含切换滞后信息)。
  - 奖励 r_t: 决策窗内 obj 增量 = (mean_sinr + 0.5·min_sinr)_t −
    同根恒零功率参考窗 obj (反事实差分, 与 twin_action_dataset
    同根纪律)。
  - 静态参考解对照: 同 seed 恒定动作序列 (全程 cell_tx_scale_db
    不变) vs 时变动作序列, 验证时变动作确实改变轨迹。

输出: datalake/generated/coord_seq_dataset.parquet
      datalake/generated/coord_seq_static_ref.json
"""

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twin.calibrated import TinyTwin           # noqa: E402
from datalake.config import GEN_DIR            # noqa: E402

SCENES = ["campus", "城区", "郊区", "低空", "机器人工厂"]
GRID = [-6.0, -3.0, 0.0, 3.0, 6.0]

# seed 族与 coord_actions.py 一致 (主 seed 在前, 与场景间 13 间隔族
# 不相交; 附加 seed offset 1000 间隔)
N_SEEDS = 5
_N_SEEDS = N_SEEDS          # 可被 main() 覆盖 (smoke 模式)
SEED_OFFSETS = [0, 1000, 2000, 3000, 4000]

# ---- 时序 MDP 几何 ----
TICK_MS = 200.0          # 仿真 tick (行粒度 200ms)
TICKS_PER_DEC = 5        # 每 5 tick (1s) 一次决策
EP_S = 10.0              # episode 时长 → 10 个决策窗


def seeds_of(scene: str) -> list[int]:
    """场景多 seed 列表 (与 coord_actions.seeds_of 完全一致)。"""
    i = SCENES.index(scene)
    base = 9000 + i * 13
    return [base + off for off in SEED_OFFSETS[:_N_SEEDS]]


# ---------------------------------------------------------------- 核心 rollout
def _win_obj(ue_win: pd.DataFrame) -> float:
    """窗目标: mean_ue_sinr + 0.5·min_ue_sinr (UE 级均值后再聚合, 同 coord_actions)。"""
    per_ue = ue_win.groupby("UEID")["服务小区SINR(dB)"].mean()
    return float(per_ue.mean() + 0.5 * per_ue.min())


def _win_feats(ue_win: pd.DataFrame, tw: TinyTwin) -> dict:
    """决策窗特征 (决策时可知): 逐小区 SINR/负载 + L3 边距。

    L3 边距取窗末 tw._tick_l3 (n_ue × n_cell), 按服务小区归属聚合成
    逐小区 mean(serving_l3 − best_neighbor_l3)。
    """
    cell_of = {c["cell_name"]: c["idx"] for c in tw.site}
    ue_per_cell = ue_win.groupby("UEID").agg(
        sinr=("服务小区SINR(dB)", "mean"),
        cell=("服务小区ID", "first"),
    )
    feats = {}
    for ci in range(tw.n_cell):
        sub = ue_per_cell[ue_per_cell["cell"].map(cell_of) == ci]
        feats[f"sinr_c{ci}"] = float(sub["sinr"].mean()) if len(sub) else 0.0
        feats[f"n_ue_c{ci}"] = int(len(sub))

    l3 = tw._tick_l3                          # (n_ue, n_cell)
    srv = np.array(tw._tick_srv)              # (n_ue,)
    margins = np.zeros((tw.n_ue,))
    for j in range(tw.n_ue):
        row = l3[j]
        s = srv[j]
        nb = [v for ci, v in enumerate(row) if ci != s]
        margins[j] = row[s] - max(nb) if nb else 0.0
    for ci in range(tw.n_cell):
        sel = srv == ci
        feats[f"l3_margin_c{ci}"] = float(margins[sel].mean()) if sel.any() else 0.0

    feats["mean_sinr"] = float(ue_win["服务小区SINR(dB)"].mean())
    return feats


def run_seq_episode(scene: str, seed: int, action_seq: list[tuple[float, ...]],
                    collect: bool = True) -> tuple[list[dict], pd.DataFrame | None]:
    """跑一条时序 episode, 返回 (逐决策点记录, 全轨迹 ue 表或 None)。

    action_seq 长度 = 决策点数 = EP_S / (TICKS_PER_DEC·TICK_MS/1000)。
    决策点 t 的动作在读取窗 t 特征后施加 (窗 t 行已生成), 自下一窗起
    全程生效直至被后续动作覆盖 — 与"恒定参考"可比。
    """
    tw = TinyTwin(scene, n_cell=3, n_ue=8, ep_s=EP_S, seed=seed,
                  tick_ms=TICK_MS)
    tw._init_tick_state()
    n_ticks = int(round(tw.ep_ms / tw.tick_ms))
    rows_ep = []
    ue_all = [] if collect else None
    n_dec = len(action_seq)
    win_buf: list[dict] = []   # 当前决策窗累计行 (TICKS_PER_DEC tick × 8 UE)
    for ti in range(n_ticks):
        rows = tw._step_tick(tw.tick_ms)
        if collect:
            ue_all.extend(rows)
        win_buf.extend(rows)
        dec_idx = ti // TICKS_PER_DEC
        at_window_end = (ti + 1) % TICKS_PER_DEC == 0
        if not at_window_end or dec_idx >= n_dec:
            continue
        # 窗末: 读全窗特征 (5 tick × 8 UE = 40 行) → 施加动作 (下一 tick 起生效)
        ue_win = pd.DataFrame(win_buf)
        win_buf = []
        feats = _win_feats(ue_win, tw)
        act = action_seq[dec_idx]
        if any(x != 0.0 for x in act):
            tw.cell_tx_scale_db = list(act)
        else:
            tw.cell_tx_scale_db = None
        rows_ep.append({
            "scene": scene, "seed": seed, "ep_id": f"{scene}|{seed}",
            "dec_idx": dec_idx, "t_ms": float(tw._tick_t_ms),
            "p0": act[0], "p1": act[1], "p2": act[2],
            "next_p0": action_seq[dec_idx + 1][0] if dec_idx + 1 < n_dec else 0.0,
            "next_p1": action_seq[dec_idx + 1][1] if dec_idx + 1 < n_dec else 0.0,
            "next_p2": action_seq[dec_idx + 1][2] if dec_idx + 1 < n_dec else 0.0,
            "win_obj": _win_obj(ue_win),
            **feats,
        })
    return rows_ep, (pd.DataFrame(ue_all) if collect else None)


def ref_zero_obj(scene: str, seed: int) -> list[float]:
    """同根恒零参考: 全程 cell_tx_scale_db=None 的逐窗 obj (反事实基线)。"""
    tw = TinyTwin(scene, n_cell=3, n_ue=8, ep_s=EP_S, seed=seed,
                  tick_ms=TICK_MS)
    tw._init_tick_state()
    n_ticks = int(round(tw.ep_ms / tw.tick_ms))
    objs = []
    buf = []
    for ti in range(n_ticks):
        buf.extend(tw._step_tick(tw.tick_ms))
        if (ti + 1) % TICKS_PER_DEC == 0:
            objs.append(_win_obj(pd.DataFrame(buf)))
            buf = []
    return objs


def sample_action_seq(rng: np.random.Generator,
                      n_dec: int) -> list[tuple[float, ...]]:
    """随机时变动作序列: 每决策点独立采样 per-cell 偏移。"""
    return [tuple(float(rng.choice(GRID)) for _ in range(3))
            for _ in range(n_dec)]


def static_best_seqs(scene: str, seed: int,
                     rng: np.random.Generator) -> list[list[tuple[float, ...]]]:
    """静态参考候选: 恒定组合 + 少量随机恒定组合 (供对照分析)。"""
    n_dec = n_dec_points()
    cands = [[(0.0, 0.0, 0.0)] * n_dec]
    for combo in [(-6.0, 0.0, +6.0), (+6.0, 0.0, -6.0), (+3.0, +3.0, +3.0)]:
        cands.append([combo] * n_dec)
    for _ in range(3):
        c = tuple(float(rng.choice(GRID)) for _ in range(3))
        cands.append([c] * n_dec)
    return cands


def n_dec_points() -> int:
    return int(EP_S * 1000.0 / (TICKS_PER_DEC * TICK_MS))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", nargs="*", default=SCENES)
    ap.add_argument("--n-seeds", type=int, default=N_SEEDS)
    ap.add_argument("--n-episodes", type=int, default=12,
                    help="每 (scene, seed) 的随机时变 episode 数")
    ap.add_argument("--out", default="coord_seq_dataset.parquet")
    args = ap.parse_args()

    global _N_SEEDS
    _N_SEEDS = max(1, args.n_seeds)

    t0 = time.time()
    nd = n_dec_points()
    print(f"[gen_coord_seqs] 决策点数/episode = {nd} "
          f"(tick {TICK_MS:.0f}ms × {TICKS_PER_DEC}, ep {EP_S}s)")
    print(f"[gen_coord_seqs] scenes={args.scenes} seeds/scene={_N_SEEDS} "
          f"episodes/(scene,seed)={args.n_episodes}")

    rows_all = []
    static_ref = {}
    for scene in args.scenes:
        for seed in seeds_of(scene):
            rng = np.random.default_rng(seed)
            # 1) 恒零参考逐窗 obj (奖励基线, 同根)
            zero_objs = ref_zero_obj(scene, seed)
            assert len(zero_objs) == nd, f"参考窗数 {len(zero_objs)} != {nd}"

            # 2) 随机时变 episodes
            for ep in range(args.n_episodes):
                aseq = sample_action_seq(rng, nd)
                rows_ep, _ = run_seq_episode(scene, seed, aseq)
                assert len(rows_ep) == nd
                for r, zobj in zip(rows_ep, zero_objs):
                    r["ep_i"] = ep
                    r["ref_obj"] = zobj
                    r["reward"] = r["win_obj"] - zobj
                rows_all.extend(rows_ep)

            # 3) 静态参考候选 (恒定组合) — 记录总 obj 供对照
            best = None
            for si, seq in enumerate(static_best_seqs(scene, seed, rng)):
                rows_s, _ = run_seq_episode(scene, seed, seq)
                total = sum(r["win_obj"] for r in rows_s)
                key = f"{scene}|{seed}|static{si}|{seq[0]}"
                static_ref[key] = {
                    "combo": ",".join(f"{v:+g}" for v in seq[0]),
                    "total_obj": total,
                    "zero_total": sum(zero_objs),
                }
                if best is None or total > best[1]:
                    best = (seq[0], total)
            print(f"  {scene:8s} seed {seed}: 静态最优 {best[0]} "
                  f"总 obj {best[1]:.3f} (零参考 {sum(zero_objs):.3f}, "
                  f"Δ {best[1] - sum(zero_objs):+.3f})")

    df = pd.DataFrame(rows_all)
    out = GEN_DIR / args.out
    df.to_parquet(out, index=False)
    print(f"\n[gen_coord_seqs] 数据集 {df.shape} → {out}")
    print(f"[gen_coord_seqs] 列: {list(df.columns)}")
    n_ep = df.groupby(['scene', 'seed', 'ep_i']).ngroups
    print(f"[gen_coord_seqs] episodes = {n_ep}, 决策点 = {len(df)}")

    # 完整性断言: 每 episode 决策点数完整 + 关键列无缺失
    assert df.notna().all().all(), "数据集存在缺失值"
    cnt = df.groupby(['scene', 'seed', 'ep_i']).size()
    assert (cnt == nd).all(), f"episode 决策点数不完整: {cnt.unique()}"
    assert (df.dec_idx.to_numpy() == np.tile(np.arange(nd), n_ep)).all(), \
        "dec_idx 序列不完整"
    print("[gen_coord_seqs] 完整性断言通过 ✓")

    # 静态参考: 至少一个非零恒定组合的总 obj 与零参考不同 (动作有效性)
    per_seed = {}
    for key, v in static_ref.items():
        scene, seed = key.split("|")[:2]
        per_seed.setdefault(f"{scene}|{seed}", []).append(
            (v["combo"], v["total_obj"], v["zero_total"]))
    n_valid = 0
    for k, lst in per_seed.items():
        zt = lst[0][2]
        assert all(abs(x[2] - zt) < 1e-9 for x in lst), "零参考不稳定"
        nonzero = [x for x in lst if x[0] != "+0,+0,+0"]
        if any(abs(x[1] - zt) > 1e-6 for x in nonzero):
            n_valid += 1
    assert n_valid == len(per_seed), \
        f"静态参考动作未改变总 obj 的 seed: {len(per_seed) - n_valid}"
    print(f"[gen_coord_seqs] 静态参考对照通过 ✓ ({len(per_seed)} seed 全部 "
          f"非零动作显著改变轨迹)")

    with open(GEN_DIR / "coord_seq_static_ref.json", "w",
              encoding="utf-8") as f:
        json.dump(static_ref, f, ensure_ascii=False, indent=1)
    print(f"[gen_coord_seqs] 静态参考 → coord_seq_static_ref.json")
    print(f"[gen_coord_seqs] 用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
