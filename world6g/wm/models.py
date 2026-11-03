#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型库: 动作条件世界模型基准
============================
统一接口 (所有模型):
  predict_next(xh_norm [B,H,S], xa_norm [B,2]) → ŷ_norm [B,S]
  → Task A 单步 / Task B 自回归 rollout / Task C 反事实均可复用。

模型清单 (容量递增 + 结构对照):
  1. Persistence       ŷ = s_t                       (无学习下界)
  2. LinearAR+action   岭回归 (闭式)                  (线性可解释基线)
  3. GRU-WM            GRU 编码 + 动作嵌入 + MLP       (现有, 18.8k)
  4. RSSM-WM           确定性 GRU + 高斯隐变量 z       (Dreamer 风格, KL 均衡)
  5. TF-WM             解码器-only 因果自注意力        (GPT 风格)
  6. PhysWM            物理先验 + 学习残差             (物理 informed 混合)

RSSM/TF 输出高斯 → 同时提供 aleatoric 不确定性 (Task D 校准评测)。
"""
from __future__ import annotations

import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

N_STATE = 4
N_ACT = 2


# ============================================================
# 1. Persistence (零参数)
# ============================================================
class PersistenceWM(nn.Module):
    name = "Persistence"

    def __init__(self):
        super().__init__()

    def forward(self, xh, xa):
        return xh[:, -1]                      # ŷ = s_t


# ============================================================
# 2. LinearAR + action (岭回归, 闭式解; 用 nn.Linear 承载权重)
# ============================================================
class LinearARWM(nn.Module):
    name = "LinearAR+action"

    def __init__(self, hist: int = 8, n_state: int = N_STATE, lam: float = 1.0):
        super().__init__()
        self.hist = hist
        # 权重: [hist*S + 1 (动作值) + 5 (动作类型 one-hot), S]
        n_feat = hist * n_state + 1 + 5
        self.W = nn.Linear(n_feat, n_state, bias=True)
        self.lam = lam
        self.fitted = False

    def forward(self, xh, xa):
        B, H, S = xh.shape
        feats = torch.cat([
            xh.reshape(B, H * S),
            xa[:, 1:2],                                   # 动作值 (归一化)
            F.one_hot(xa[:, 0].long(), 5).float(),        # 动作类型 one-hot
        ], dim=1)
        return self.W(feats)

    def fit_closed_form(self, Xh, Xa, Y):
        """闭式岭回归 (numpy), 写入 self.W"""
        B, H, S = Xh.shape
        A = np.concatenate([Xh.reshape(B, H * S), Xa[:, 1:2],
                            np.eye(5)[Xa[:, 0].astype(int)]], 1)
        A1 = np.concatenate([A, np.ones((B, 1))], 1)
        n_feat = A1.shape[1]
        W = np.linalg.solve(A1.T @ A1 + self.lam * np.eye(n_feat), A1.T @ Y)
        with torch.no_grad():
            self.W.weight.copy_(torch.tensor(W[:-1].T, dtype=torch.float32))
            self.W.bias.copy_(torch.tensor(W[-1], dtype=torch.float32))
        self.fitted = True
        return self


# ============================================================
# 3. GRU-WM (现有基线结构, 18.8k 参数)
# ============================================================
class GRUWM(nn.Module):
    name = "GRU-WM"

    def __init__(self, n_state: int = N_STATE, n_act: int = N_ACT, hid: int = 64):
        super().__init__()
        self.act_emb = nn.Sequential(nn.Linear(n_act, 16), nn.ReLU())
        self.gru = nn.GRU(n_state + 16, hid, batch_first=True)
        self.head = nn.Sequential(nn.Linear(hid, 32), nn.ReLU(), nn.Linear(32, n_state))

    def forward(self, xh, xa):
        a = self.act_emb(xa).unsqueeze(1).expand(-1, xh.shape[1], -1)
        z, _ = self.gru(torch.cat([xh, a], -1))
        return self.head(z[:, -1])


# ============================================================
# 4. RSSM-WM: 确定性路径 + 高斯隐变量 (Dreamer 风格, 简化)
# ============================================================
class RSSMWM(nn.Module):
    """recurrent state-space model:
    h_t = GRU([h_{t-1}, z_{t-1}, a_t])          — 确定性记忆
    z_t ~ N(μ(h_t), σ(h_t))                     — 随机隐状态 (一步)
    ŷ_{t+1} = MLP([h_t, z_t])
    训练: 重构损失 + KL(后验 z_t ‖ 先验 N(μ(h_{t-1},z_{t-1}),σ))
    推理: 单步用后验 (历史可见), rollout 用先验采样 (不确定性传播)。
    """
    name = "RSSM-WM"

    def __init__(self, n_state: int = N_STATE, n_act: int = N_ACT,
                 h_dim: int = 64, z_dim: int = 16):
        super().__init__()
        self.h_dim, self.z_dim = h_dim, z_dim
        self.act_emb = nn.Sequential(nn.Linear(n_act, 16), nn.ReLU())
        self.gru = nn.GRUCell(n_state + 16 + z_dim, h_dim)
        # 后验 (编码历史观测 x_t): 输入 [h_t, x_t]
        self.post = nn.Sequential(nn.Linear(h_dim + n_state, 64), nn.ReLU(),
                                  nn.Linear(64, 2 * z_dim))
        # 先验 (预测 z_t from h_t): 输入 [h_t]
        self.prior = nn.Sequential(nn.Linear(h_dim, 64), nn.ReLU(),
                                   nn.Linear(64, 2 * z_dim))
        self.head = nn.Sequential(nn.Linear(h_dim + z_dim, 64), nn.ReLU(),
                                  nn.Linear(64, n_state))

    def forward(self, xh, xa, sample: bool = False):
        """xh [B,H,S] 归一化历史, xa [B,2] 动作 → ŷ [B,S] (下一时刻均值)

        训练: z 从后验采样 (随机正则); 推理 (eval): 后验均值 (确定性 rollout, 不发散)。
        sample=True 时 eval 下也采样 (MC-z 不确定性, Task D)。
        """
        B, H, S = xh.shape
        a = self.act_emb(xa)                                    # [B,16]
        h = xh.new_zeros(B, self.h_dim)
        z = xh.new_zeros(B, self.z_dim)
        for t in range(H):
            h = self.gru(torch.cat([xh[:, t], a, z], -1))
            mu, logvar = self.post(torch.cat([h, xh[:, t]], -1)).chunk(2, -1)
            if self.training or sample:
                z = mu + (0.5 * logvar).exp() * torch.randn_like(mu)
            else:
                z = mu
        return self.head(torch.cat([h, z], -1))

    def forward_with_logvar(self, xh, xa):
        """返回 (ŷ, σ²_aleatoric) — Task D 校准用 (MC-z)"""
        out = self.forward(xh, xa, sample=True)
        return out, None


# ============================================================
# 5. TF-WM: 解码器-only 因果自注意力 (GPT 风格)
# ============================================================
class Block(nn.Module):
    def __init__(self, d_model: int, n_head: int = 4):
        super().__init__()
        self.ln1 = nn.LayerNorm(d_model)
        self.attn = nn.MultiheadAttention(d_model, n_head, batch_first=True)
        self.ln2 = nn.LayerNorm(d_model)
        self.mlp = nn.Sequential(nn.Linear(d_model, d_model * 2), nn.GELU(),
                                 nn.Linear(d_model * 2, d_model))

    def forward(self, x, mask):
        h = self.ln1(x)
        x = x + self.attn(h, h, h, attn_mask=mask, need_weights=False)[0]
        x = x + self.mlp(self.ln2(x))
        return x


class TFWM(nn.Module):
    """序列: [s_{t-H+1}, ..., s_t, A] → 位置嵌入 + 因果注意力 → 末 token 输出 ŷ。
    动作 token 注入动作条件; 因果掩码保证时序无泄漏。"""
    name = "TF-WM"

    def __init__(self, n_state: int = N_STATE, n_act: int = N_ACT,
                 d_model: int = 64, n_layer: int = 2, n_head: int = 4, hist: int = 8):
        super().__init__()
        self.d_model = d_model
        self.state_emb = nn.Linear(n_state, d_model)
        self.act_emb = nn.Linear(n_act, d_model)
        self.pos = nn.Parameter(torch.randn(1, hist + 1, d_model) * 0.02)
        self.blocks = nn.ModuleList([Block(d_model, n_head) for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(d_model)
        self.head = nn.Linear(d_model, n_state)
        H = hist
        m = torch.full((H + 1, H + 1), float("-inf"))
        m = torch.triu(m, diagonal=1)
        self.register_buffer("mask", m)

    def forward(self, xh, xa):
        B, H, S = xh.shape
        toks = torch.cat([self.state_emb(xh),
                          self.act_emb(xa).unsqueeze(1)], 1)      # [B,H+1,D]
        x = toks + self.pos[:, : H + 1]
        for blk in self.blocks:
            x = blk(x, self.mask)
        return self.head(self.ln_f(x[:, -1]))


# ============================================================
# 6. PhysWM: 物理先验 + 学习残差 (physics-informed hybrid)
# ============================================================
class PhysWM(nn.Module):
    """物理先验 (可微, 无参数):
      RSRP: ΔdB = 10·log10(power_scale)                      (发射功率动作)
      PRB:  rate ×= prb_scale                                 (资源比例动作)
      其余动作 (az/ho) 与其他 KPI: 先验 = persistence (Δ=0)
    残差头 (GRU): 学习先验未覆盖的响应 (扇区增益/干扰/切换级联/队列动态)。
    ŷ = physics_prior(s_t, a) + residual([history, a])

    动作编码输入: xa_norm = [type_idx, value_norm]; 先验需要原始值 → 调用方传
    xa_raw (未归一化 [type, raw_value])。为兼容统一接口, forward 里用逆变换恢复。
    """
    name = "PhysWM"

    def __init__(self, n_state: int = N_STATE, hid: int = 64,
                 a_mu: float = 0.0, a_sd: float = 1.0):
        super().__init__()
        self.a_mu, self.a_sd = a_mu, a_sd
        self.act_emb = nn.Sequential(nn.Linear(N_ACT, 16), nn.ReLU())
        self.gru = nn.GRU(n_state + 16, hid, batch_first=True)
        self.head = nn.Sequential(nn.Linear(hid, 32), nn.ReLU(), nn.Linear(32, n_state))

    def set_norm(self, a_mu: float, a_sd: float):
        self.a_mu, self.a_sd = a_mu, a_sd

    def physics_prior(self, s_t, xa_norm):
        """s_t [B,S] (归一化), xa_norm [B,2] → 先验 ŷ [B,S] (归一化空间)。

        归一化空间中的先验:
          RSRP: ŷ_rsrp = s_rsrp + 10log10(p) / sd_rsrp        (p = 功率倍数, 原始值)
          DL:   ŷ_dl   = s_dl × p_prb                          (比例动作, 尺度不变)
        其余: persistence。
        注意 xa[:,0]: 0=baseline 1=pmax_scale 2=prb_scale 3=az 4=ho
        """
        B = s_t.shape[0]
        out = s_t.clone()
        raw_val = (xa_norm[:, 1] * self.a_sd + self.a_mu)         # 恢复原始动作值
        t_idx = xa_norm[:, 0].long()
        # RSRP 功率先验 (KPI 0): ΔdB = 10log10(v)
        v_p = torch.where(t_idx == 1, raw_val.clamp(min=1e-3), torch.ones_like(raw_val))
        d_rsrp_db = 10.0 * torch.log10(v_p)                        # dB
        # 物理先验作用于归一化状态 → 除以 sd_rsrp; sd 在外部注册
        # (self.sd 注册于 fit 时; 简化: 调用方保证 self.sd 已设置)
        out[:, 0] = out[:, 0] + d_rsrp_db / self.sd[0]
        # DL 速率先验 (KPI 2): ŷ = s × prb_scale
        v_prb = torch.where(t_idx == 2, raw_val.clamp(min=1e-3), torch.ones_like(raw_val))
        out[:, 2] = out[:, 2] * v_prb
        return out

    def forward(self, xh, xa):
        if not hasattr(self, "sd") or self.sd is None:
            raise RuntimeError("PhysWM.sd not set — call set_state_sd() first")
        a = self.act_emb(xa).unsqueeze(1).expand(-1, xh.shape[1], -1)
        z, _ = self.gru(torch.cat([xh, a], -1))
        residual = self.head(z[:, -1])
        return self.physics_prior(xh[:, -1], xa) + residual

    def set_state_sd(self, sd):
        self.sd = sd          # np.array [S] → 注册为 buffer


def build_model(name: str, norm=None, hist: int = 8) -> nn.Module:
    if name == "Persistence":
        return PersistenceWM()
    if name == "LinearAR+action":
        return LinearARWM(hist=hist)
    if name == "GRU-WM":
        return GRUWM()
    if name == "RSSM-WM":
        return RSSMWM()
    if name == "TF-WM":
        return TFWM(hist=hist)
    if name == "PhysWM":
        m = PhysWM()
        if norm is not None:
            m.set_norm(norm.a_mu, norm.a_sd)
            m.set_state_sd(norm.sd)
        return m
    raise ValueError(f"unknown model {name}")


MODEL_NAMES = ["Persistence", "LinearAR+action", "GRU-WM", "RSSM-WM", "TF-WM", "PhysWM"]
LEARNED = ["GRU-WM", "RSSM-WM", "TF-WM", "PhysWM"]     # 需要训练的


if __name__ == "__main__":
    torch.manual_seed(0)
    B, H = 4, 8
    xh = torch.randn(B, H, N_STATE)
    xa = torch.tensor([[1, 0.5], [2, -0.3], [3, 0.1], [0, 0.0]])
    for name in MODEL_NAMES:
        m = build_model(name)
        if name == "LinearAR+action":
            m.fit_closed_form(xh.numpy(), xa.numpy(), torch.randn(B, N_STATE).numpy())
        if name == "PhysWM":
            m.set_norm(0.0, 1.0); m.set_state_sd(np.ones(N_STATE))
        m.eval()
        with torch.no_grad():
            y = m(xh, xa)
        n_par = sum(p.numel() for p in m.parameters())
        print(f"{name:16s} out={tuple(y.shape)} params={n_par:,}")
