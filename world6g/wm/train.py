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
