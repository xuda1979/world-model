#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动作模型 (Policy) 训练 — 融合奖励蒸馏 (λ=0.5 MS + 0.5 v0.2)
============================================================
背景 (见 ablation_report.md §5): 融合奖励 λ=0.5 在闭环上兼得
单步 v0.2 的进攻性 (lift +0.469) 与多步 MS 的决策保真度 (fid 0.651)。
该发现目前是"在线 CEM 计算" — 运行时须同时加载两个 WM 做 rollout,
不可部署。本脚本把融合专家的决策蒸馏为**单网络一步前馈**动作模型。

方法与 train_planner.py 完全同构 (PolicyNet 双头 / 独热同种子 / 类型 CE +
选中类值 MSE), 唯一差异: 专家 CEM-MPC 的奖励 = FusedReward(λ=0.5),
即 fuse_wm_planner.py 已验证的融合目标 (各 WM 在自身 z 空间打分)。

输出:
  models/policy_fused_net.pt     — 融合奖励蒸馏的 PolicyNet 权重
  models/policy_fused_meta.json  — 归一化统计 + 动作空间 + 融合配置

用法:
  cd world6g && ../.venv/bin/python scripts/train_planner_fused.py [--epochs 60] [--n-window 512]
"""
import sys, json, time, argparse, importlib.util
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tp = _load("tp", ROOT / "scripts" / "train_planner.py")        # PolicyNet / gen_expert / train_policy
fw = _load("fw", ROOT / "scripts" / "fuse_wm_planner.py")      # FusedReward

from wm import data as D                                # noqa: E402
from wm.planner import WorldModel, CEMPlanner           # noqa: E402

MODELS_DIR = ROOT / "models"
SEED = 42
LAM = 0.5   # 融合系数 (ablation_report §5 最优 ∩)
K = 8


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--n-window", type=int, default=512,
                    help="专家窗口数 (每窗口一次融合 CEM 规划, 双 WM rollout)")
    args = ap.parse_args()

    torch.manual_seed(SEED)
    np.random.seed(SEED)

    print("[1/4] 加载双世界模型 + 同根分支数据 ...")
    groups = D.load_branches()
    wm_ms = WorldModel()                                    # v0.3-MS (多步一致性, 生产)
    wm_v2 = WorldModel(wm_path=MODELS_DIR / "gru_wm.pt",
                       meta_path=MODELS_DIR / "gru_wm_meta.json")   # v0.2 (单步)
    frw = fw.FusedReward(wm_ms, wm_v2, lam=LAM, k=K)
    planner = CEMPlanner(wm=wm_ms, reward=frw)
    print(f"  融合奖励: r = {LAM}·r_MS + {1 - LAM:.1f}·r_v2 (各 WM 自身 z 空间), k={K}")

    print(f"[2/4] 生成融合专家数据 (λ={LAM} CEM-MPC, {args.n_window} 窗) ...")
    X, t_lab, v_lab = tp.gen_expert(planner, groups, n_windows=args.n_window)
    print(f"  type distribution: "
          f"{ {t: int((t_lab == tp.TYPE2IDX[t]).sum()) for t in tp.INT_TYPES} }")

    print(f"[3/4] 训练策略网络 ({args.epochs} epochs) ...")
    net = tp.PolicyNet()
    n_par = sum(p.numel() for p in net.parameters())
    print(f"  PolicyNet params: {n_par:,}")
    net, best_acc, curve = tp.train_policy(net, X, t_lab, v_lab, epochs=args.epochs)

    print("[4/4] 留出段评测 (policy-fused vs 融合 CEM-MPC) ...")
    ev = tp.eval_policy(net, planner, groups, planner.reward, n_windows=128)
    for k, v in ev.items():
        print(f"  {k:34s} {v}")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(net.state_dict(), MODELS_DIR / "policy_fused_net.pt")
    meta = {
        "state_cols": D.STATE_COLS,
        "hist": D.HIST,
        "mu": planner.wm.mu.tolist(),
        "sd": planner.wm.sd.tolist(),
        "int_types": tp.INT_TYPES,
        "action_space": {t: tp.ACTION_SPACE[t] for t in tp.ACTION_SPACE},
        "hidden": 128,
        "seed": SEED,
        "epochs": args.epochs,
        "n_window": args.n_window,
        "params": int(n_par),
        "val_type_acc": float(best_acc),
        "eval": {k: (None if isinstance(v, float) and np.isnan(v) else v)
                 for k, v in ev.items()},
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "objective": ("policy distillation from FUSED-reward CEM-MPC "
                      f"(λ={LAM}·r_MS + {1 - LAM:.1f}·r_v2, 各 WM 自身 z 空间), "
                      "dual-head MLP: type CE + chosen-value MSE"),
        "source_wms": ["models/gru_wm_ms.pt (seed42, 多步一致性)",
                       "models/gru_wm.pt (v0.2, 单步)"],
        "fused_lambda": LAM,
    }
    (MODELS_DIR / "policy_fused_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] models/policy_fused_net.pt ({n_par:,} params) + policy_fused_meta.json")


if __name__ == "__main__":
    main()
