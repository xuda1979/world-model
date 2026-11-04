#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一训练循环: 动作条件世界模型
==============================
- 任务 A 协议训练 (单步, 时间外推切分); RSSM 加 ELBO (KL 均衡)
- 早停: 固定 epoch (CPU/MPS 可复现), 训练曲线记录
- 种子固定 → 论文可复现
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from . import models as M


def set_seed(seed: int = 42):
    np.random.seed(seed)
    torch.manual_seed(seed)


def train_model(model: nn.Module, Xh, Xa, Y, epochs: int = 40, batch: int = 512,
                lr: float = 1e-3, device: str = "cpu", verbose: bool = True,
                seed: int = 42, kl_beta: float = 1.0):
    """标准 MSE 训练 (所有模型统一). RSSM 由 forward 内部处理随机性."""
    set_seed(seed)
    model = model.to(device)
    if getattr(model, "fitted", False):      # LinearAR 闭式解已拟合
        return []
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.MSELoss()
    Xh_t = torch.tensor(Xh, device=device)
    Xa_t = torch.tensor(Xa, device=device)
    Y_t = torch.tensor(Y, device=device)
    n = len(Xh_t)
    curve = []
    t0 = time.time()
    for ep in range(epochs):
        model.train()
        idx = torch.randperm(n, device=device)
        tot = 0.0
        for i in range(0, n, batch):
            b = idx[i: i + batch]
            opt.zero_grad()
            out = model(Xh_t[b], Xa_t[b])
            loss = loss_fn(out, Y_t[b])
            loss.backward()
            opt.step()
            tot += float(loss) * len(b)
        curve.append(tot / n)
        if verbose and (ep + 1) % 10 == 0:
            print(f"  epoch {ep + 1:3d}: loss {curve[-1]:.4f}  ({time.time() - t0:.0f}s)")
    return curve


def fit_all(norm, Xh, Xa, Y, device: str = "cpu", epochs: int = 40, seed: int = 42,
            names: list | None = None):
    """训练全部模型 (或子集), 返回 {name: model}"""
    Xh_n = norm.state(Xh).astype(np.float32)
    Xa_n = norm.act(Xa)
    Y_n = norm.state(Y).astype(np.float32)
    out = {}
    for name in (names or M.LEARNED):
        print(f"[train] {name}")
        model = M.build_model(name, norm=norm)
        if name == "LinearAR+action":
            model.fit_closed_form(Xh_n, Xa_n, Y_n)
        else:
            train_model(model, Xh_n, Xa_n, Y_n, epochs=epochs, device=device, seed=seed)
        model.eval()
        out[name] = model
    # Persistence 无需训练
    out["Persistence"] = M.PersistenceWM().eval()
    return out, (Xh_n, Xa_n, Y_n)


# ============================================================
# v2: 多步一致性训练 (Task B rollout 复合误差)
# ============================================================
def train_model_multistep(model: nn.Module, norm, Xh, Xa, Yk,
                          epochs: int = 40, batch: int = 256,
                          lr: float = 1e-3, device: str = "cpu",
                          seed: int = 42, ks=(1, 2, 4, 8),
                          lam_start: float = 0.3, lam_end: float = 0.9):
    """多步一致性训练 (GRU-WM-MS / PhysWM-MS; RSSM 排除 — KL 训练与 rollout 目标不兼容).

    输入 Xh [N,H,S] 原始, Xa [N,2] 原始, Yk [N,K,S] 原始 (K=max(ks)).
    每个样本按其 y_k 长度分组; 每 batch 从 ks 均匀采样 k, 取分组子集。

    损失 = λ·rollout_mse(k 步自回归) + (1−λ)·teacher_forcing_mse (第 1 步)
    λ 随 epoch 线性从 lam_start → lam_end (curriculum: 先单步后 rollout)。
    """
    set_seed(seed)
    model = model.to(device)
    Xh_n = torch.tensor(norm.state(Xh), dtype=torch.float32, device=device)
    Xa_n = torch.tensor(norm.act(Xa), dtype=torch.float32, device=device)
    Yk_n = torch.tensor(norm.state(Yk), dtype=torch.float32, device=device)
    # 校验: 每个 k 不超过 Yk 序列长度
    lens = Yk_n.shape[1]
    for k in ks:
        assert 1 <= k <= lens, f"k={k} exceeds Yk length {lens}"
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    n = len(Xh_n)
    t0 = time.time()
    for ep in range(epochs):
        model.train()
        lam = lam_start + (lam_end - lam_start) * ep / max(epochs - 1, 1)
        idx = torch.randperm(n, device=device)
        tot = 0.0
        for i in range(0, n, batch):
            b = idx[i: i + batch]
            k = int(ks[torch.randint(len(ks), (1,)).item()])
            h = Xh_n[b]
            xa = Xa_n[b]
            # teacher forcing: 第 1 步 (历史真实)
            y1 = model(h, xa)
            loss_tf = F.mse_loss(y1, Yk_n[b, 0])
            # rollout: k 步自回归 (预测滚动进窗口)
            hh = h.clone()
            outs = []
            for _ in range(k):
                y = model(hh, xa)
                outs.append(y)
                hh = torch.cat([hh[:, 1:], y.unsqueeze(1)], dim=1)
            outs = torch.stack(outs, dim=1)                    # [B,k,S]
            loss_roll = F.mse_loss(outs, Yk_n[b, :k])
            loss = lam * loss_roll + (1 - lam) * loss_tf
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss) * len(b)
        if (ep + 1) % 10 == 0:
            print(f"  ms-epoch {ep + 1:3d}: loss {tot / n:.4f} (λ={lam:.2f})  "
                  f"({time.time() - t0:.0f}s)")
    return model


# ============================================================
# v2: 异方差 NLL 训练 (Task D 不确定性校准)
# ============================================================
def train_model_nll(model: nn.Module, norm, Xh, Xa, Y,
                    epochs: int = 40, batch: int = 512,
                    lr: float = 1e-3, device: str = "cpu",
                    seed: int = 42, warmup_epochs: int = 5):
    """异方差高斯 NLL 训练 (Task D v2).

    前置: model.install_sigma_head() 已装配 σ 头;
    warmup_epochs: 前 N 个 epoch 冻结 σ 头只训均值 (σ 随机初始化会污染 NLL 梯度)。
    NLL = 0.5·[log σ² + (y−ŷ)²/σ²] (每元素, 对 KPI 维平均)。
    """
    set_seed(seed)
    model = model.to(device)
    Xh_n = torch.tensor(norm.state(Xh), dtype=torch.float32, device=device)
    Xa_n = torch.tensor(norm.act(Xa), dtype=torch.float32, device=device)
    Y_n = torch.tensor(norm.state(Y), dtype=torch.float32, device=device)
    assert getattr(model, "sigma_head", None) is not None, "call install_sigma_head() first"
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    n = len(Xh_n)
    t0 = time.time()
    for ep in range(epochs):
        model.train()
        freeze_sigma = ep < warmup_epochs
        if freeze_sigma and model.sigma_head is not None:
            for p in model.sigma_head.parameters():
                p.requires_grad_(False)
        elif model.sigma_head is not None:
            for p in model.sigma_head.parameters():
                p.requires_grad_(True)
        idx = torch.randperm(n, device=device)
        tot = 0.0
        for i in range(0, n, batch):
            b = idx[i: i + batch]
            y, s = model(Xh_n[b], Xa_n[b], return_sigma=True)
            if freeze_sigma:
                loss = F.mse_loss(y, Y_n[b])
            else:
                nll = 0.5 * (2 * torch.log(s) + (Y_n[b] - y) ** 2 / s ** 2)
                loss = nll.mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss) * len(b)
        if (ep + 1) % 10 == 0:
            print(f"  nll-epoch {ep + 1:3d}: loss {tot / n:.4f}"
                  f"{' (mean warmup)' if freeze_sigma else ''}  ({time.time() - t0:.0f}s)")
    # 恢复梯度开关
    for p in model.sigma_head.parameters():
        p.requires_grad_(True)
    return model
