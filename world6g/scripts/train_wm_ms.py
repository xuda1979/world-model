#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GRU-WM-MS 服务化导出 (v2 多步一致性模型)
=========================================
复现 wm/evaluate.py 的 GRU-WM-MS 训练协议 (同 seed=42 / 同采样 / 同超参),
训练后将权重 + 归一化统计导出为 twm_serve.py 可加载的格式:

  models/gru_wm_ms.pt          — GRUWM 权重 (架构与 gru_wm 相同, 仅训练目标不同)
  models/gru_wm_ms_meta.json   — state_cols / action_types / mu / sd / a_mu / a_sd / hist

用途: /api/wm/rollout 与 /api/wm/verify 服务 v2 模型
  (v0.2 单步模型 80 步自回归 rollout 复合漂移严重 — h40 RMSE 1.103 vs MS 0.849)。

用法: python3 scripts/train_wm_ms.py [--epochs 40]
"""
import sys, json, argparse
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wm.data as D
import wm.train as T
import wm.models as M

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
SEED = 42


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--k", type=int, default=8, help="rollout 目标步数 (与 evaluate.py 一致)")
    args = ap.parse_args()

    torch.manual_seed(SEED)
    np.random.seed(SEED)

    print("[1/3] 加载同根分支数据 ...")
    groups = D.load_branches()
    rw = D.rollout_windows(groups, k=args.k, seed=SEED)
    Xh_r = np.stack([s["xh"] for s in rw]).astype(np.float32)
    Xa_r = np.stack([s["xa"] for s in rw]).astype(np.float32)
    Yk_r = np.stack([s["y_k"] for s in rw]).astype(np.float32)
    print(f"  rollout windows: {len(rw):,} (k≤{args.k})")

    # 归一化: 与 evaluate.py 相同 — Task A 训练段统计 (fit_norm 仅用 t<8000ms)
    ws = D.branch_windows(groups)
    norm = D.fit_norm(ws)
    print(f"  norm.mu = {norm.mu.round(2)}  norm.sd = {norm.sd.round(2)}")
    print(f"  a_mu = {norm.a_mu:.4f}  a_sd = {norm.a_sd:.4f}")

    print(f"[2/3] 训练 GRU-WM-MS ({args.epochs} epochs, 多步一致性 λ curriculum) ...")
    model = M.build_model("GRU-WM", norm=norm)
    T.train_model_multistep(model, norm, Xh_r, Xa_r, Yk_r,
                            epochs=args.epochs, ks=(1, 2, 4, 8), seed=SEED)
    n_params = sum(q.numel() for q in model.parameters())

    print("[3/3] 导出服务格式 ...")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), MODELS_DIR / "gru_wm_ms.pt")
    meta = {
        "state_cols": D.STATE_COLS,
        "action_types": D.ACTION_TYPES,
        "hist": D.HIST,
        "mu": norm.mu.tolist(),
        "sd": norm.sd.tolist(),
        "a_mu": float(norm.a_mu),
        "a_sd": float(norm.a_sd),
        "seed": SEED,
        "epochs": args.epochs,
        "params": int(n_params),
        "trained_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "data": ("twin_action_dataset.parquet rollout windows "
                 f"(同根反事实分支, {len(rw):,} 窗 × k≤{args.k})"),
        "objective": "multi-step consistency: λ·rollout_mse + (1−λ)·teacher_forcing, λ 0.3→0.9",
        "source_eval": "wm/evaluate.py Task B v2 (GRU-WM-MS h40 RMSE_norm 0.849 vs 单步 1.103)",
    }
    (MODELS_DIR / "gru_wm_ms_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] models/gru_wm_ms.pt ({n_params:,} params) + gru_wm_ms_meta.json")


if __name__ == "__main__":
    main()
