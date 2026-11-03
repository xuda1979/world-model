#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评测指标: 单步 / 多步 rollout / 反事实 Δ-效应 / 不确定性校准
============================================================
所有指标在归一化空间计算 (与论文表一致), 关键指标附原生单位换算。
"""
from __future__ import annotations

import numpy as np
import torch


# ---------------- 基础误差 ----------------
def rmse(pred, true):
    return float(np.sqrt(((np.asarray(pred) - np.asarray(true)) ** 2).mean()))


def mae(pred, true):
    return float(np.abs(np.asarray(pred) - np.asarray(true)).mean())


def per_kpi_rmse(pred, true, sd):
    """每 KPI 的原生单位 RMSE: 输入为归一化预测/真值"""
    pred, true, sd = np.asarray(pred), np.asarray(true), np.asarray(sd)
    return (np.sqrt(((pred - true) ** 2).mean(0)) * sd).tolist()


# ---------------- 多步 rollout (Task B) ----------------
@torch.no_grad()
def rollout(model, xh, xa, steps: int, chunk: int = 4096):
    """自回归 rollout.

    model(xh [B,H,S], xa [B,2]) → ŷ [B,S]; 预测滚动进窗口。
    返回 [B, steps, S] (归一化空间)。
    """
    model.eval()
    outs = []
    B = xh.shape[0]
    for i in range(0, B, chunk):
        h = xh[i: i + chunk].clone()
        o = []
        for _ in range(steps):
            y = model(h, xa[i: i + chunk])
            o.append(y)
            h = torch.cat([h[:, 1:], y.unsqueeze(1)], 1)
        outs.append(torch.stack(o, 1))
    return torch.cat(outs, 0).numpy()


def rollout_from_persistence(model, xh, xa, steps: int):
    """Persistence rollout: ŷ_k = s_t ∀k → 仅用于对照曲线"""
    return np.repeat(xh[:, -1:].numpy()[:, None, :], steps, axis=1)


def horizon_rmse(pred_roll: np.ndarray, true_roll: np.ndarray):
    """pred_roll/true_roll [B, K, S] → 每 step 的 RMSE (归一化空间) [K]"""
    d2 = ((pred_roll - true_roll) ** 2).mean(axis=(0, 2))
    return np.sqrt(d2)


# ---------------- 反事实 Δ-效应 (Task C) ----------------
def delta_metrics(pred, true_delta):
    """Δ-效应指标.

    pred, true_delta: [N, S] 归一化空间 (Δs = s^(a) − s^(∅) 已归一化)
    返回: rmse, mae, corr (Pearson, 全元素), sign_acc (仅 |Δ*|>0.1 样本)
    """
    p, t = np.asarray(pred), np.asarray(true_delta)
    d = p - t
    rmse_v = float(np.sqrt((d ** 2).mean()))
    mae_v = float(np.abs(d).mean())
    # 相关性: 展平全元素 Pearson
    pf, tf = p.ravel(), t.ravel()
    if pf.std() < 1e-9 or tf.std() < 1e-9:
        corr = 0.0
    else:
        corr = float(np.corrcoef(pf, tf)[0, 1])
    # 符号准确率: 真实效应显著样本
    mask = np.abs(tf) > 0.1
    sign_acc = float(((np.sign(pf[mask]) == np.sign(tf[mask])).mean())) if mask.sum() > 10 else float("nan")
    return {"rmse": rmse_v, "mae": mae_v, "corr": corr, "sign_acc": sign_acc,
            "n_sig": int(mask.sum())}


# ---------------- 不确定性校准 (Task D) ----------------
def gaussian_nll(pred, true, sigma):
    """高斯 NLL (归一化空间), sigma [N,S] 或标量"""
    p, t, s = map(np.asarray, (pred, true, sigma))
    s = np.maximum(s, 1e-3)
    return float((np.log(s) + 0.5 * ((t - p) / s) ** 2).mean() + 0.5 * np.log(2 * np.pi))


def coverage(pred, true, sigma, z: float = 1.96):
    """经验覆盖率 vs 名义覆盖率 (±zσ 区间)"""
    p, t, s = map(np.asarray, (pred, true, sigma))
    inside = np.abs(t - p) <= z * s
    return float(inside.mean())


def ece_regression(pred, true, sigma, n_bins: int = 10, z: float = 1.96):
    """回归 ECE: |经验覆盖 − 名义覆盖| 加权分箱"""
    p, t, s = map(np.asarray, (pred, true, sigma))
    e = np.abs(t - p) / np.maximum(s, 1e-3)
    # 名义: P(|N(0,1)| ≤ z) = erf(z/√2)
    from math import erf, sqrt
    nominal = erf(z / sqrt(2))
    edges = np.linspace(0, 4 * z / 1.96, n_bins + 1)
    ece, n_total = 0.0, len(e.ravel())
    for i in range(n_bins):
        m = (e >= edges[i]) & (e < edges[i + 1])
        if m.sum() == 0:
            continue
        ece += m.sum() / n_total * abs(m.mean() - (edges[i] + edges[i + 1]) / 2 / (4 * z / 1.96) * nominal)
    return float(ece)


# ---------------- 统计功效: 配对 bootstrap ----------------
def paired_bootstrap(a_err, b_err, n_boot: int = 2000, seed: int = 0):
    """配对 bootstrap: 返回 P(a_err > b_err) 与 (a−b) 均值的 95% CI.
    a_err, b_err: 同一样本集上的逐样本误差 (|pred−true|, 展平)"""
    rng = np.random.default_rng(seed)
    a, b = np.asarray(a_err).ravel(), np.asarray(b_err).ravel()
    n = len(a)
    diffs = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        diffs.append(a[idx].mean() - b[idx].mean())
    diffs = np.array(diffs)
    return {
        "p_a_worse": float((diffs > 0).mean()),
        "ci95": [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))],
    }
