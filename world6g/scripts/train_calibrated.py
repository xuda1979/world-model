#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
训练校准版世界模型 (GRU-WM-MS) — 数据来自校准仿真器
=====================================================
在 datalake/generated/twin_cal_action_dataset.parquet (5 场景含 campus)
上用与已提交 train_wm_ms.py 完全相同的协议训练 GRU-WM-MS:

  - 同 seed=42 / 同 fit_norm (前 8s 训练段) / 同 multi-step curriculum
  - 仅数据源不同 (cal vs uncal) → 公平对照: 校准仿真器是否带来更强 WM

模型命名: models/wm_cal.pt / wm_uncal.pt (对照)
用法: python scripts/train_calibrated.py [--which cal|uncal] [--epochs 40]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wm.data as D
import wm.train as T
import wm.models as M

PKG_DIR = Path(__file__).resolve().parents[1]
GEN_DIR = PKG_DIR / "datalake" / "generated"
MODELS_DIR = PKG_DIR / "models"
SEED = 42


def build_pipeline(which: str):
    """加载 cal 或 uncal 的 parquet, 构造与 wm/data.py 相同的 groups"""
    if which == "cal":
        import twin.calibrated
        scenes = ["城区", "郊区", "低空", "机器人工厂", "campus"]
    else:
        scenes = ["城区", "郊区", "低空", "机器人工厂"]
    ds = None
    # 直接复用 load_branches (读取固定 parquet, 但自定义场景集协议)
    import pandas as pd
    parquet = GEN_DIR / f"twin_{which}_action_dataset.parquet"
    df = pd.read_parquet(parquet)
    groups = D.load_branches(df)
    # 过滤: 只保留已知场景 (load_branches 已按 branch_scene 分组; campus 自动纳入)
    return groups, scenes


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", default="cal", choices=["cal", "uncal"])
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--k", type=int, default=8)
    args = ap.parse_args()

    torch.manual_seed(SEED)
    np.random.seed(SEED)

    groups, scenes = build_pipeline(args.which)
    print(f"[{args.which}] scenes {scenes}")

    rw = D.rollout_windows(groups, k=args.k, seed=SEED)
    Xh_r = np.stack([s["xh"] for s in rw]).astype(np.float32)
    Xa_r = np.stack([s["xa"] for s in rw]).astype(np.float32)
    Yk_r = np.stack([s["y_k"] for s in rw]).astype(np.float32)
    print(f"  rollout windows: {len(rw):,} (k≤{args.k})")

    ws = D.branch_windows(groups)
    norm = D.fit_norm(ws)
    print(f"  norm.mu = {norm.mu.round(2)}  norm.sd = {norm.sd.round(2)}")
    print(f"  a_mu = {norm.a_mu:.4f}  a_sd = {norm.a_sd:.4f}")

    model = M.build_model("GRU-WM", norm=norm)
    T.train_model_multistep(model, norm, Xh_r, Xa_r, Yk_r,
                            epochs=args.epochs, ks=(1, 2, 4, 8), seed=SEED)
    n_params = sum(q.numel() for q in model.parameters())

    tag = f"wm_{args.which}"
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), MODELS_DIR / f"{tag}.pt")
    meta = {
        "tag": args.which, "data": f"twin_{args.which}_action_dataset.parquet",
        "scenes": scenes, "n_windows": len(rw),
        "state_cols": D.STATE_COLS, "action_types": D.ACTION_TYPES,
        "hist": D.HIST, "mu": norm.mu.tolist(), "sd": norm.sd.tolist(),
        "a_mu": float(norm.a_mu), "a_sd": float(norm.a_sd),
        "seed": SEED, "epochs": args.epochs, "params": int(n_params),
        "trained_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "calibrated": (args.which == "cal"),
    }
    (MODELS_DIR / f"{tag}_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] models/{tag}.pt ({n_params:,} params) + {tag}_meta.json")


if __name__ == "__main__":
    main()
