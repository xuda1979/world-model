#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校准仿真器数据的动作模型 (Policy) 训练 — 会话 26
================================================
在已校准/未校准仿真器生成的同根分支数据集上, 各蒸馏一个 CEM-MPC → 策略网络,
实现 "模型配对": wm_cal + 专属策略 vs wm_uncal + 专属策略 (公平对照)。

复用 train_planner.py 的 PolicyNet / gen_expert / train_policy / eval_policy
(与 train_planner_fused.py 相同的 importlib 加载模式, 无代码复制)。

用法:
  cd world6g && ../.venv/bin/python scripts/train_policy_calib.py --which cal
  cd world6g && ../.venv/bin/python scripts/train_policy_calib.py --which uncal
输出:
  models/policy_cal_net.pt   + models/policy_cal_meta.json
  models/policy_uncal_net.pt + models/policy_uncal_meta.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wm.data as D
from wm.planner import (CEMPlanner, CEMConfig, RewardConfig, make_reward,
                        ACTION_SPACE)

MODELS_DIR = ROOT / "models"
GEN_DIR = ROOT / "datalake" / "generated"
SEED = 42


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tp = _load("tp", ROOT / "scripts" / "train_planner.py")   # PolicyNet / gen_expert / train_policy / eval_policy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", choices=["cal", "uncal"], required=True)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--n-window", type=int, default=512)
    ap.add_argument("--lam-action", type=float, default=0.02)
    ap.add_argument("--lam-v", type=float, default=1.0,
                    help="值回归损失权重 (train_policy.lam_v)")
    args = ap.parse_args()
    which = args.which

    torch.manual_seed(SEED)
    np.random.seed(SEED)

    parquet = GEN_DIR / f"twin_{which}_action_dataset.parquet"
    ds = pd.read_parquet(parquet)
    print(f"[1/4] 加载 {parquet.name} (calibrated={which=='cal'}) ...")
    groups = D.load_branches(ds=ds)

    wm_path = MODELS_DIR / f"wm_{which}.pt"
    from wm.planner import WorldModel
    wm = WorldModel(wm_path=wm_path, meta_path=MODELS_DIR / f"wm_{which}_meta.json")
    rcfg = RewardConfig(lam_action=args.lam_action, k=8)
    reward_fn = make_reward(rcfg)
    planner = CEMPlanner(wm=wm, reward=reward_fn, cem=CEMConfig(seed=7), rcfg=rcfg)
    print(f"  WM: {wm_path.name}  mu={np.round(wm.mu, 3).tolist()}")

    print(f"[2/4] 生成专家数据 (CEM-MPC, {args.n_window} 窗) ...")
    X, t_lab, v_lab = tp.gen_expert(planner, groups, n_windows=args.n_window)
    print(f"  type distribution: "
          f"{ {t: int((t_lab == tp.TYPE2IDX[t]).sum()) for t in tp.INT_TYPES} }")

    print(f"[3/4] 训练策略网络 ({args.epochs} epochs) ...")
    net = tp.PolicyNet()
    n_par = sum(p.numel() for p in net.parameters())
    print(f"  PolicyNet params: {n_par:,}")
    net, best_acc, curve = tp.train_policy(net, X, t_lab, v_lab,
                                           epochs=args.epochs, lam_v=args.lam_v)

    print("[4/4] 留出段评测 (policy vs CEM-MPC, 同数据集) ...")
    ev = tp.eval_policy(net, planner, groups, reward_fn, n_windows=128)
    for k, v in ev.items():
        print(f"  {k:34s} {v}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    out_pt = MODELS_DIR / f"policy_{which}_net.pt"
    torch.save(net.state_dict(), out_pt)
    meta = {
        "which": which,
        "calibrated": which == "cal",
        "data": f"datalake/generated/twin_{which}_action_dataset.parquet",
        "source_wm": f"models/wm_{which}.pt + wm_{which}_meta.json",
        "training_budget": "训练段 t_ms<8000 同根基准分支窗 (H=8, S=4)",
        "state_cols": D.STATE_COLS,
        "hist": D.HIST,
        "mu": wm.mu.tolist(),
        "sd": wm.sd.tolist(),
        "int_types": tp.INT_TYPES,
        "action_space": {t: ACTION_SPACE[t] for t in ACTION_SPACE},
        "hidden": 128,
        "seed": SEED,
        "epochs": args.epochs,
        "n_window": args.n_window,
        "lam_action": args.lam_action,
        "lam_v": args.lam_v,
        "params": int(n_par),
        "val_type_acc": float(best_acc),
        "eval": {k: (None if isinstance(v, float) and np.isnan(v) else v)
                 for k, v in ev.items()},
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "objective": ("policy distillation from CEM-MPC on calibrated-simulator "
                      f"({which}) data; dual-head MLP: type CE + chosen-value MSE"),
    }
    (MODELS_DIR / f"policy_{which}_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {out_pt.name} ({n_par:,} params) + policy_{which}_meta.json")


if __name__ == "__main__":
    main()
