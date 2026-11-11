#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时序协同策略 (Coordination Sequence Policy, M11)
=================================================
在时序 MDP 数据集 (coord_seq_dataset.parquet, 3000 行 = 25 组 × 12 eps × 10 dec)
上完成 "时序世界模型 → 规划 oracle → BC 蒸馏" 全链路, 并做 5 折闭环 LOSO:

  1. 时序世界模型 (temporal WM): (ctx_t, a_t, scene) → (ctx_{t+1}, r_{t+1})
     - ctx: 10 维决策窗特征 (逐小区 SINR/负载/L3 边距 + 全局 mean_sinr);
     - a: per-cell one-hot (3×5=15); scene one-hot (5); 输入 30 维;
     - 输出 11 维 (next ctx 10 + reward 1), 全 z-score (one-hot 除外);
     - 每折 3 成员 ensemble (不同 torch seed), 规划用均值预测。
  2. 规划 oracle (WM-greedy): 决策时对 125 组合批量前向, 取预测 reward
     argmax — 与仿真真值贪心 (deepcopy 分支回放) 同为 myopic 结构, 严格可比。
  3. BC 蒸馏 (seq policy): 规划器标签 (离线数据集状态 + 在线规划轨迹状态)
     → 直接策略网 ctx+scene → 125 logits (部署侧免 WM 推理)。
  4. 闭环 LOSO (fold = seed offset, 每场景留 1 seed × 5 场景 = 5 组/折):
     控制器对比 恒零 / 随机 / 最优恒定(125 全枚举) / WM-贪心 / BC 策略 /
     仿真真值贪心; 指标 = episode 总回报 Σ_t (win_obj_t − ref_zero_t)。

LOSO 折协议说明: 按 seed offset 折 (非按场景折) — 训练集始终含全部 5 场景,
场景 one-hot 列在训练集内非退化 (按场景折会导致 one-hot 列 std=0 归一化爆炸)。

输出: models/coord_seq_wm.pt + coord_seq_wm_meta.json        (全数据部署版 WM)
      models/coord_seq_policy.pt + coord_seq_policy_meta.json (部署版动作模型)
      models/coord_seq_folds.pt                               (LOSO 折模型, 复现)
      datalake/generated/coord_seq_loso.json                  (闭环 LOSO 结果)
"""

import argparse
import copy
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.gen_coord_seqs import (                       # noqa: E402
    SCENES, GRID, TICK_MS, TICKS_PER_DEC, EP_S, SEED_OFFSETS,
    seeds_of, ref_zero_obj, _win_obj, _win_feats, n_dec_points)
from twin.calibrated import TinyTwin                       # noqa: E402
from datalake.config import GEN_DIR                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"

COMBOS = list(itertools.product(GRID, repeat=3))
COMBO_IDX = {c: i for i, c in enumerate(COMBOS)}
G5 = len(GRID)                                # 5
N_DEC = n_dec_points()                        # 10
N_TICKS = int(round(EP_S * 1000.0 / TICK_MS))  # 50
FX = ["sinr_c0", "n_ue_c0", "sinr_c1", "n_ue_c1", "sinr_c2", "n_ue_c2",
      "l3_margin_c0", "l3_margin_c1", "l3_margin_c2", "mean_sinr"]
ACT_DIM = G5 * 3                              # 15
IN_DIM = len(FX) + ACT_DIM + len(SCENES)      # 30
OUT_DIM = len(FX) + 1                         # 11
CONTROLLERS = ["zero", "random", "best_const", "wm_greedy", "bc_policy",
               "sim_greedy"]


# ---------------------------------------------------------------- 模型
class TemporalWM(nn.Module):
    """(ctx, action, scene) → (next_ctx, reward)。"""

    def __init__(self, hidden=256):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(IN_DIM, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, OUT_DIM))

    def forward(self, x):
        return self.net(x)


class SeqPolicy(nn.Module):
    """(ctx, scene) → 125 组合 logits (BC 蒸馏的部署侧动作模型)。"""

    def __init__(self, hidden=128):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(len(FX) + len(SCENES), hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, len(COMBOS)))

    def forward(self, x):
        return self.net(x)


# ---------------------------------------------------------------- 工具
def act_ohMat(a_idx: np.ndarray) -> np.ndarray:
    """组合索引 (N,) → per-cell one-hot (N, 15)。"""
    a_idx = np.asarray(a_idx, dtype=np.int64)
    n = len(a_idx)
    oh = np.zeros((n, ACT_DIM), dtype=np.float32)
    d0 = (a_idx // (G5 * G5)) % G5
    d1 = (a_idx // G5) % G5
    d2 = a_idx % G5
    oh[np.arange(n), d0] = 1.0
    oh[np.arange(n), G5 + d1] = 1.0
    oh[np.arange(n), 2 * G5 + d2] = 1.0
    return oh


def scene_ohMat(s_idx: np.ndarray) -> np.ndarray:
    return np.eye(len(SCENES), dtype=np.float32)[np.asarray(s_idx, dtype=np.int64)]


def combo_str(c) -> str:
    return ",".join(f"{v:+g}" for v in c)


def load_transitions(df: pd.DataFrame):
    """数据集 → 转移数组 (t → t+1, 每 episode 9 条)。"""
    X, A, R, XN, S, FOLD = [], [], [], [], [], []
    s_map = {s: i for i, s in enumerate(SCENES)}
    for (sc, sd, _ei), g in df.groupby(["scene", "seed", "ep_i"], sort=True):
        g = g.sort_values("dec_idx")
        x = g[FX].to_numpy(np.float32)
        a = g[["p0", "p1", "p2"]].to_numpy(float)
        r = g["reward"].to_numpy(np.float32)
        si = s_map[sc]
        fold = SEED_OFFSETS.index(int(sd) - (9000 + 13 * si))
        for t in range(len(g) - 1):
            X.append(x[t])
            A.append(COMBO_IDX[tuple(a[t])])
            R.append(r[t + 1])
            XN.append(x[t + 1])
            S.append(si)
            FOLD.append(fold)
    return (np.array(X), np.array(A), np.array(R), np.array(XN),
            np.array(S), np.array(FOLD))


# ---------------------------------------------------------------- WM 训练/推理
def train_wm_ensemble(X, A, R, XN, S, n_members, epochs, device, seed0,
                      hidden=256, lr=2e-3):
    """训练 WM ensemble; 返回 (members, stats, final_losses)。"""
    ctx_mu = X.mean(0)
    ctx_sd = X.std(0) + 1e-6
    y = np.hstack([XN, R[:, None]]).astype(np.float32)
    y_mu = y.mean(0)
    y_sd = y.std(0) + 1e-6
    P = np.hstack([(X - ctx_mu) / ctx_sd, act_ohMat(A), scene_ohMat(S)])
    P = P.astype(np.float32)
    Y = ((y - y_mu) / y_sd).astype(np.float32)
    Pt = torch.tensor(P, device=device)
    Yt = torch.tensor(Y, device=device)
    members, losses = [], []
    for m in range(n_members):
        torch.manual_seed(seed0 + m)
        net = TemporalWM(hidden).to(device)
        opt = torch.optim.Adam(net.parameters(), lr=lr)
        loss = None
        for _ in range(epochs):
            opt.zero_grad()
            loss = ((net(Pt) - Yt) ** 2).mean()
            loss.backward()
            opt.step()
        members.append(net.eval())
        losses.append(float(loss.detach()))
    stats = (ctx_mu, ctx_sd, y_mu, y_sd)
    return members, stats, losses


@torch.no_grad()
def wm_predict(members, X, A_idx, S_idx, stats, device, chunk=256):
    """ensemble 均值预测, 返回原始单位 (N, 11)。X: (N,10) raw ctx。"""
    ctx_mu, ctx_sd, y_mu, y_sd = stats
    n = len(X)
    out = np.zeros((n, OUT_DIM), dtype=np.float64)
    for lo in range(0, n, chunk):
        hi = min(lo + chunk, n)
        cz = ((X[lo:hi] - ctx_mu) / ctx_sd).astype(np.float32)
        P = np.hstack([cz, act_ohMat(A_idx[lo:hi]),
                       scene_ohMat(S_idx[lo:hi])]).astype(np.float32)
        Pt = torch.tensor(P, device=device)
        preds = [m(Pt).cpu().numpy() for m in members]
        out[lo:hi] = np.mean(preds, 0) * y_sd + y_mu
    return out


def planner_labels(members, stats, X, S_idx, device, chunk=256):
    """对 (N,10) ctx 批量打规划器标签 (argmax_a 预测 reward) → (N,) combo idx。"""
    n = len(X)
    labels = np.zeros(n, dtype=np.int64)
    nc = len(COMBOS)
    all_a = np.arange(nc)
    for lo in range(0, n, chunk):
        hi = min(lo + chunk, n)
        rows = hi - lo
        Xt = np.repeat(X[lo:hi], nc, axis=0)
        At = np.tile(all_a, rows)
        St = np.repeat(np.asarray(S_idx[lo:hi]), nc)
        r = wm_predict(members, Xt, At, St, stats, device)[:, -1]
        r = r.reshape(rows, nc)
        labels[lo:hi] = np.argmax(r, axis=1)
    return labels


# ---------------------------------------------------------------- BC 训练/推理
def train_bc(Xb, yb, Sb, epochs, device, seed_torch, hidden=128,
             lr=1e-3, wd=1e-4):
    ctx_mu = Xb.mean(0)
    ctx_sd = Xb.std(0) + 1e-6
    P = np.hstack([((Xb - ctx_mu) / ctx_sd).astype(np.float32),
                   scene_ohMat(Sb)]).astype(np.float32)
    Pt = torch.tensor(P, device=device)
    yt = torch.tensor(np.asarray(yb, dtype=np.int64), device=device)
    torch.manual_seed(seed_torch)
    pol = SeqPolicy(hidden).to(device)
    opt = torch.optim.Adam(pol.parameters(), lr=lr, weight_decay=wd)
    loss = None
    for _ in range(epochs):
        opt.zero_grad()
        loss = nn.functional.cross_entropy(pol(Pt), yt)
        loss.backward()
        opt.step()
    pol = pol.eval()
    with torch.no_grad():
        acc = float((pol(Pt).argmax(1) == yt).float().mean())
    return pol, (ctx_mu, ctx_sd), acc, float(loss.detach())


@torch.no_grad()
def bc_argmax(pol, ctx_stats, X, S_idx, device):
    ctx_mu, ctx_sd = ctx_stats
    P = np.hstack([((X - ctx_mu) / ctx_sd).astype(np.float32),
                   scene_ohMat(S_idx)]).astype(np.float32)
    Pt = torch.tensor(P, device=device)
    return pol(Pt).argmax(1).cpu().numpy().astype(np.int64)


# ---------------------------------------------------------------- 闭环仿真
def closed_loop(scene, seed, act_fn):
    """tick 级闭环: 每决策窗读特征 → act_fn(dec, ctx, s_idx, tw) → 施加动作。

    与 gen_coord_seqs.run_seq_episode 完全同语义 (动作窗后施加, 自下窗生效)。
    返回 (objs 逐窗 obj, acts 逐决策动作, ctxs 逐窗特征)。
    """
    tw = TinyTwin(scene, n_cell=3, n_ue=8, ep_s=EP_S, seed=seed,
                  tick_ms=TICK_MS)
    tw._init_tick_state()
    s_idx = SCENES.index(scene)
    objs, acts, ctxs = [], [], []
    buf = []
    for ti in range(N_TICKS):
        buf.extend(tw._step_tick(tw.tick_ms))
        if (ti + 1) % TICKS_PER_DEC == 0:
            ue_win = pd.DataFrame(buf)
            buf = []
            feats = _win_feats(ue_win, tw)
            ctx = np.array([feats[f] for f in FX], dtype=np.float32)
            dec = len(objs)
            a = act_fn(dec, ctx, s_idx, tw)
            if any(v != 0.0 for v in a):
                tw.cell_tx_scale_db = [float(v) for v in a]
            else:
                tw.cell_tx_scale_db = None
            objs.append(_win_obj(ue_win))
            acts.append(tuple(float(v) for v in a))
            ctxs.append(ctx)
    return objs, acts, ctxs


def zero_fn(dec, ctx, s_idx, tw):
    return (0.0, 0.0, 0.0)


def const_fn(combo):
    return lambda dec, ctx, s_idx, tw: combo


def random_fn(seed, r_idx):
    rng = np.random.default_rng([int(seed), int(r_idx), 20261109])
    return lambda dec, ctx, s_idx, tw: tuple(
        float(rng.choice(GRID)) for _ in range(3))


def sim_greedy_fn(dec, ctx, s_idx, tw):
    """仿真真值贪心 (oracle 上界): deepcopy 分支 125 组合各滚一窗取 argmax。"""
    if dec >= N_DEC - 1:            # 最后一个决策无后续窗, 动作无效
        return (0.0, 0.0, 0.0)
    best_a, best_o = (0.0, 0.0, 0.0), -1e18
    for combo in COMBOS:
        b = copy.deepcopy(tw)
        if any(v != 0.0 for v in combo):
            b.cell_tx_scale_db = list(combo)
        else:
            b.cell_tx_scale_db = None
        rows = []
        for _ in range(TICKS_PER_DEC):
            rows.extend(b._step_tick(TICK_MS))
        o = _win_obj(pd.DataFrame(rows))
        if o > best_o:
            best_o, best_a = o, combo
    return best_a


def wm_greedy_fn(members, stats, device):
    def fn(dec, ctx, s_idx, tw):
        lab = planner_labels(members, stats, ctx[None, :],
                             np.array([s_idx]), device)[0]
        return COMBOS[int(lab)]
    return fn


def bc_greedy_fn(pol, ctx_stats, device):
    def fn(dec, ctx, s_idx, tw):
        lab = bc_argmax(pol, ctx_stats, ctx[None, :],
                        np.array([s_idx]), device)[0]
        return COMBOS[int(lab)]
    return fn


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="快检: 1 折 + 单成员 + 少 epoch + 2 随机参考")
    ap.add_argument("--folds", type=int, nargs="*", default=None,
                    help="指定折 (默认全部 5 折)")
    ap.add_argument("--wm-epochs", type=int, default=1200)
    ap.add_argument("--bc-epochs", type=int, default=600)
    ap.add_argument("--wm-members", type=int, default=3)
    ap.add_argument("--n-random", type=int, default=8)
    ap.add_argument("--wm-seed0", type=int, default=4100)
    ap.add_argument("--bc-seed", type=int, default=4900)
    ap.add_argument("--final-wm-seed0", type=int, default=7100)
    ap.add_argument("--final-bc-seed", type=int, default=7900)
    ap.add_argument("--device", default=None)
    ap.add_argument("--tag", default="",
                    help="产物文件名后缀 (smoke 用 '_smoke')")
    args = ap.parse_args()

    t0 = time.time()
    device = args.device or ("mps" if torch.backends.mps.is_available()
                             else "cpu")
    if args.smoke:
        args.folds = args.folds or [0]
        args.wm_epochs, args.bc_epochs = 200, 100
        args.wm_members, args.n_random = 1, 2
        args.tag = args.tag or "_smoke"
    folds = args.folds if args.folds is not None else list(range(len(SEED_OFFSETS)))
    print(f"[coord_seq] device={device} folds={folds} "
          f"wm_epochs={args.wm_epochs} members={args.wm_members} "
          f"bc_epochs={args.bc_epochs} tag='{args.tag}'", flush=True)

    df = pd.read_parquet(GEN_DIR / "coord_seq_dataset.parquet")
    assert df.notna().all().all()
    X, A, R, XN, S, FOLD = load_transitions(df)
    # BC 离线标签用的行级数组 (dec <= 8 的行, 动作有后续窗)
    s_map = {s: i for i, s in enumerate(SCENES)}
    row_ctx = df[FX].to_numpy(np.float32)
    row_scene = df["scene"].map(s_map).to_numpy(np.int64)
    row_fold = np.array([SEED_OFFSETS.index(int(sd) - (9000 + 13 * s_map[sc]))
                         for sc, sd in zip(df["scene"], df["seed"])])
    row_dec = df["dec_idx"].to_numpy(np.int64)
    bc_mask = row_dec <= N_DEC - 2
    print(f"[coord_seq] 转移 {len(X)} 条 | BC 离线行 {int(bc_mask.sum())}",
          flush=True)

    refs: dict[tuple[str, int], list[float]] = {}

    def get_ref(scene, seed):
        key = (scene, seed)
        if key not in refs:
            refs[key] = ref_zero_obj(scene, seed)
        return refs[key]

    fold_records = []
    fold_states = []
    for k in folds:
        tf = time.time()
        tr = FOLD != k
        te = ~tr
        # ---- 1. 时序世界模型 ensemble ----
        members, stats, losses = train_wm_ensemble(
            X[tr], A[tr], R[tr], XN[tr], S[tr], args.wm_members,
            args.wm_epochs, device, args.wm_seed0 + k * 10)
        yh = wm_predict(members, X[te], A[te], S[te], stats, device)
        pr, tr_r = yh[:, -1], R[te]
        r2 = float(1 - ((pr - tr_r) ** 2).sum()
                   / ((tr_r - tr_r.mean()) ** 2).sum())
        mae = float(np.abs(pr - tr_r).mean())
        nc_mae = {f: float(np.abs(yh[:, i] - XN[te][:, i]).mean())
                  for i, f in enumerate(FX)}
        print(f"[fold {k}] WM x{args.wm_members} loss={np.mean(losses):.5f} "
              f"| heldout reward MAE={mae:.3f} R2={r2:.3f}", flush=True)

        # ---- 2. BC 数据: 离线标签 + 在线规划轨迹 ----
        off_tr = bc_mask & (row_fold != k)
        lab_off = planner_labels(members, stats, row_ctx[off_tr],
                                 row_scene[off_tr], device)
        train_groups = [(sc, seeds_of(sc)[k2])
                        for k2 in range(len(SEED_OFFSETS)) if k2 != k
                        for sc in SCENES]
        wfn = wm_greedy_fn(members, stats, device)
        on_ctx, on_act, on_scene = [], [], []
        train_returns = []
        for sc, sd in train_groups:
            objs, acts, ctxs = closed_loop(sc, sd, wfn)
            ref = get_ref(sc, sd)
            train_returns.append(float(sum(o - r for o, r in zip(objs, ref))))
            si = s_map[sc]
            for t in range(N_DEC - 1):
                on_ctx.append(ctxs[t])
                on_act.append(COMBO_IDX[acts[t]])
                on_scene.append(si)
        Xb = np.vstack([row_ctx[off_tr], np.array(on_ctx, np.float32)])
        yb = np.concatenate([lab_off, np.array(on_act, np.int64)])
        Sb = np.concatenate([row_scene[off_tr],
                             np.array(on_scene, np.int64)])

        # ---- 3. BC 蒸馏 ----
        pol, bc_stats, bc_acc, bc_loss = train_bc(
            Xb, yb, Sb, args.bc_epochs, device,
            args.bc_seed + k)
        off_te = bc_mask & (row_fold == k)
        lab_te = planner_labels(members, stats, row_ctx[off_te],
                                row_scene[off_te], device)
        bc_te = bc_argmax(pol, bc_stats, row_ctx[off_te],
                          row_scene[off_te], device)
        agree_te = float((lab_te == bc_te).mean())
        print(f"[fold {k}] BC n={len(Xb)} acc={bc_acc:.3f} "
              f"| heldout 规划器一致率={agree_te:.3f} "
              f"| train 规划回报均值={np.mean(train_returns):.3f}", flush=True)

        # ---- 4. 闭环评估 (held-out 组) ----
        groups = []
        held = [(sc, seeds_of(sc)[k]) for sc in SCENES]
        bfn = bc_greedy_fn(pol, bc_stats, device)
        for sc, sd in held:
            ref = get_ref(sc, sd)
            rets, acts_map = {}, {}
            for name, fn in [("zero", zero_fn),
                             ("wm_greedy", wfn),
                             ("bc_policy", bfn),
                             ("sim_greedy", sim_greedy_fn)]:
                objs, acts, _ = closed_loop(sc, sd, fn)
                rets[name] = float(sum(o - r for o, r in zip(objs, ref)))
                acts_map[name] = acts
            assert abs(rets["zero"]) < 1e-9, \
                f"恒零回报应恒为 0, 实测 {rets['zero']} ({sc}|{sd})"
            rand_rets = []
            for ri in range(args.n_random):
                objs, _, _ = closed_loop(sc, sd, random_fn(sd, ri))
                rand_rets.append(float(
                    sum(o - r for o, r in zip(objs, ref))))
            rets["random"] = float(np.mean(rand_rets))
            best_c, best_ret = None, -1e18
            for combo in COMBOS:
                objs, _, _ = closed_loop(sc, sd, const_fn(combo))
                rr = float(sum(o - r for o, r in zip(objs, ref)))
                if rr > best_ret:
                    best_ret, best_c = rr, combo
            rets["best_const"] = best_ret
            mr = lambda a, b: float(np.mean([a[t] == b[t]
                                             for t in range(N_DEC - 1)]))
            groups.append({
                "scene": sc, "seed": int(sd),
                "returns": rets,
                "random_returns": rand_rets,
                "best_const_combo": combo_str(best_c),
                "match_rate": {
                    "wm_vs_sim": mr(acts_map["wm_greedy"],
                                    acts_map["sim_greedy"]),
                    "bc_vs_sim": mr(acts_map["bc_policy"],
                                    acts_map["sim_greedy"]),
                    "bc_vs_wm": mr(acts_map["bc_policy"],
                                   acts_map["wm_greedy"])},
            })
            print(f"  [{sc}|{sd}] best_const={rets['best_const']:.2f} "
                  f"({combo_str(best_c)}) wm={rets['wm_greedy']:.2f} "
                  f"bc={rets['bc_policy']:.2f} sim={rets['sim_greedy']:.2f} "
                  f"rand={rets['random']:.2f}", flush=True)
        fold_records.append({
            "fold": int(k),
            "held_seeds": {sc: int(seeds_of(sc)[k]) for sc in SCENES},
            "wm": {"members": args.wm_members,
                   "final_loss_mean": float(np.mean(losses)),
                   "heldout_reward_mae": mae,
                   "heldout_reward_r2": r2,
                   "heldout_nextctx_mae": nc_mae},
            "bc": {"n_train": int(len(Xb)), "train_acc": bc_acc,
                   "heldout_planner_agreement": agree_te,
                   "train_planner_return_mean": float(np.mean(train_returns))},
            "groups": groups,
        })
        fold_states.append({
            "fold": int(k), "wm": [m.state_dict() for m in members],
            "wm_stats": [np.asarray(s) for s in stats],
            "bc": pol.state_dict(),
            "bc_stats": [np.asarray(s) for s in bc_stats],
        })
        print(f"[fold {k}] 完成, 用时 {time.time()-tf:.0f}s", flush=True)

    # ---- 汇总 ----
    all_groups = [g for fr in fold_records for g in fr["groups"]]
    summary = {}
    for name in CONTROLLERS:
        vals = np.array([g["returns"][name] for g in all_groups])
        summary[name] = {"mean": float(vals.mean()),
                         "p50": float(np.median(vals)),
                         "min": float(vals.min()),
                         "max": float(vals.max())}
    bc_arr = np.array([g["returns"]["best_const"] for g in all_groups])
    win = lambda name: float(np.mean([g["returns"][name] > g["returns"]["best_const"]
                                      for g in all_groups]))
    match_mean = {k: float(np.mean([g["match_rate"][k] for g in all_groups]))
                  for k in ["wm_vs_sim", "bc_vs_sim", "bc_vs_wm"]}
    per_scene = {}
    for sc in SCENES:
        gs = [g for g in all_groups if g["scene"] == sc]
        per_scene[sc] = {name: float(np.mean([g["returns"][name] for g in gs]))
                         for name in CONTROLLERS}
    result = {
        "objective": "时序协同策略 M11: 时序世界模型 → 规划 oracle → BC 蒸馏 "
                     "+ 闭环 LOSO (恒零/随机/最优恒定/WM-贪心/BC/仿真真值贪心)",
        "data": str(GEN_DIR / "coord_seq_dataset.parquet"),
        "generated_by": "scripts/train_coord_seq_policy.py",
        "protocol": {
            "folds": folds, "fold_by": "seed_offset",
            "n_groups_total": len(all_groups),
            "controllers": CONTROLLERS,
            "n_random_eps": args.n_random, "wm_members": args.wm_members,
            "wm_epochs": args.wm_epochs, "bc_epochs": args.bc_epochs,
            "metric": "episode 总回报 Σ_t (win_obj_t − ref_zero_t), 恒零≡0",
        },
        "summary": summary,
        "win_rate_vs_best_const": {n: win(n) for n in
                                   ["random", "wm_greedy", "bc_policy",
                                    "sim_greedy"]},
        "match_rates_mean": match_mean,
        "per_scene_mean_return": per_scene,
        "wm_heldout_mean": {
            "reward_mae": float(np.mean([fr["wm"]["heldout_reward_mae"]
                                         for fr in fold_records])),
            "reward_r2": float(np.mean([fr["wm"]["heldout_reward_r2"]
                                        for fr in fold_records]))},
        "bc_heldout_planner_agreement_mean": float(np.mean(
            [fr["bc"]["heldout_planner_agreement"] for fr in fold_records])),
        "folds": fold_records,
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
        "runtime_s": round(time.time() - t0, 1),
    }
    out_json = GEN_DIR / f"coord_seq_loso{args.tag}.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    torch.save(fold_states, MODELS_DIR / f"coord_seq_folds{args.tag}.pt")
    print(f"\n[coord_seq] LOSO 结果 → {out_json.name} "
          f"| 折模型 → coord_seq_folds{args.tag}.pt")

    # ---- 全数据部署版 (WM ensemble + BC 动作模型) ----
    members, stats, losses = train_wm_ensemble(
        X, A, R, XN, S, args.wm_members, args.wm_epochs, device,
        args.final_wm_seed0)
    lab_all = planner_labels(members, stats, row_ctx[bc_mask],
                             row_scene[bc_mask], device)
    on_ctx, on_act, on_scene = [], [], []
    wfn = wm_greedy_fn(members, stats, device)
    for sc in SCENES:
        for sd in seeds_of(sc):
            objs, acts, ctxs = closed_loop(sc, sd, wfn)
            si = s_map[sc]
            for t in range(N_DEC - 1):
                on_ctx.append(ctxs[t])
                on_act.append(COMBO_IDX[acts[t]])
                on_scene.append(si)
    Xb = np.vstack([row_ctx[bc_mask], np.array(on_ctx, np.float32)])
    yb = np.concatenate([lab_all, np.array(on_act, np.int64)])
    Sb = np.concatenate([row_scene[bc_mask],
                         np.array(on_scene, np.int64)])
    pol, bc_stats, bc_acc, _ = train_bc(Xb, yb, Sb, args.bc_epochs, device,
                                        args.final_bc_seed)
    wm_path = MODELS_DIR / f"coord_seq_wm{args.tag}.pt"
    pol_path = MODELS_DIR / f"coord_seq_policy{args.tag}.pt"
    torch.save({"members": [m.state_dict() for m in members],
                "ctx_mu": np.asarray(stats[0]), "ctx_sd": np.asarray(stats[1]),
                "y_mu": np.asarray(stats[2]), "y_sd": np.asarray(stats[3]),
                "fx": FX, "scenes": SCENES, "grid": GRID}, wm_path)
    torch.save({"policy": pol.state_dict(),
                "ctx_mu": np.asarray(bc_stats[0]),
                "ctx_sd": np.asarray(bc_stats[1]),
                "combos": [combo_str(c) for c in COMBOS],
                "fx": FX, "scenes": SCENES, "grid": GRID}, pol_path)
    n_par = sum(p.numel() for p in members[0].parameters())
    n_par_bc = sum(p.numel() for p in pol.parameters())
    wm_meta = {
        "objective": "时序世界模型 (M11): (ctx, action, scene) → (next_ctx, reward)",
        "model": f"TemporalWM ensemble x{args.wm_members} "
                 f"({IN_DIM}-{args.wm_epochs and 256}-{256}-{256}-{OUT_DIM})",
        "params": int(n_par), "in_dim": IN_DIM, "out_dim": OUT_DIM,
        "ctx_cols": FX, "action_space": {"per_cell_db": GRID, "n_cells": 3,
                                         "n_combos": len(COMBOS)},
        "seed": args.final_wm_seed0, "epochs": args.wm_epochs,
        "data": str(GEN_DIR / "coord_seq_dataset.parquet"),
        "loso_file": f"coord_seq_loso{args.tag}.json",
        "loso_heldout_reward": result["wm_heldout_mean"],
        "usage": "输入 ctx (FX 顺序, raw) + combo idx + scene idx → "
                 "输出 [next_ctx(10), reward(1)] (raw 单位); "
                 "规划: 对 125 组合批量前向取 reward argmax",
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    pol_meta = {
        "objective": "时序协同动作模型 (M11): ctx+scene → 125 组合 logits "
                     "(WM-贪心规划器 BC 蒸馏, 部署侧免 WM 推理)",
        "model": f"SeqPolicy({len(FX)+len(SCENES)}-128-128-{len(COMBOS)})",
        "params": int(n_par_bc),
        "ctx_cols": FX, "n_train": int(len(Xb)), "train_acc": bc_acc,
        "seed": args.final_bc_seed, "epochs": args.bc_epochs,
        "loso_file": f"coord_seq_loso{args.tag}.json",
        "loso_summary": {k: result["summary"]["bc_policy"][k]
                         for k in ["mean", "p50", "min", "max"]},
        "loso_planner_agreement_mean":
            result["bc_heldout_planner_agreement_mean"],
        "combos": [combo_str(c) for c in COMBOS],
        "trained_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    (MODELS_DIR / f"coord_seq_wm_meta{args.tag}.json").write_text(
        json.dumps(wm_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (MODELS_DIR / f"coord_seq_policy_meta{args.tag}.json").write_text(
        json.dumps(pol_meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[coord_seq] 部署版 → {wm_path.name} ({n_par:,} params x"
          f"{args.wm_members}) + {pol_path.name} ({n_par_bc:,} params) + metas")

    print("\n==== 闭环 LOSO 汇总 (总回报, 恒零≡0) ====")
    print(f"{'controller':<12} {'mean':>8} {'p50':>8} {'min':>8} "
          f"{'max':>8} {'win>best_const':>14}")
    for name in CONTROLLERS:
        s = summary[name]
        w = win(name) if name != "zero" else float("nan")
        print(f"{name:<12} {s['mean']:>8.3f} {s['p50']:>8.3f} "
              f"{s['min']:>8.3f} {s['max']:>8.3f} {w:>13.1%}")
    print(f"匹配率 (vs 仿真真值贪心): WM {match_mean['wm_vs_sim']:.1%} | "
          f"BC {match_mean['bc_vs_sim']:.1%} | BC vs WM "
          f"{match_mean['bc_vs_wm']:.1%}")
    print(f"[coord_seq] 总用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
