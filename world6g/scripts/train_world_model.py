#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动作条件世界模型基线训练 (方案二首期)
====================================
任务: 给定 (历史 KPI 状态窗, 动作), 预测下一时刻 KPI 状态。
数据: twin_action_dataset.parquet — 同根分支反事实数据 (83,200 样本)。

模型 (对照):
  1. Persistence       — ŝ_{t+1} = s_t (无学习下界)
  2. LinearAR          — 线性 AR + 动作线性项 (岭回归)
  3. GRU-WorldModel    — GRU 序列编码器 + MLP 头, 动作嵌入拼接
     (轻量 RSSM 替身: 确定性隐状态, 无重构/KL, 聚焦预测能力)

指标 (多步 roll-out 也测):
  单步 RMSE / MAE per KPI; 10 步 roll-out 累计 RMSE。

训练细节: 每分支前 8s 训练 / 后 2s 测试 (时间外推, 非随机切分);
按 (branch_scene, action) 分层, 保证测试分支的动作在训练中见过其类型。
产出: world6g/docs/wm_results.json / wm_results.md / loss_curve.png
用法: .venv/bin/python world6g/scripts/train_world_model.py [--epochs 40]
"""
import sys, json, argparse, math
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import torch
import torch.nn as nn

from datalake.config import GEN_DIR, PKG_DIR

DOCS = PKG_DIR / "docs"
SEED = 42

# 状态向量: 模型预测的 KPI (归一化后)
STATE_COLS = ["服务小区RSRP(dBm)", "服务小区SINR(dB)", "下行速率(Mbps)", "CQI"]
ACTION_TYPES = ["baseline", "pmax_scale", "prb_scale", "az_offset", "ho_offset"]
HIST = 8          # 历史窗长度 (×50ms = 400ms)


def build_sequences(ds: pd.DataFrame):
    """(branch_scene, UEID) 分组 → 滑窗序列.
    返回 X_hist [N,H,S], X_act [N,A], Y [N,S] (下一时刻)"""
    groups = []
    for (scene, action), g in ds.groupby(["branch_scene", "action"]):
        g = g.sort_values(["UEID", "时间戳(ms)"])
        groups.append((scene, action, g))
    Xh, Xa, Y, meta = [], [], [], []
    for scene, action, g in groups:
        for ue, gg in g.groupby("UEID"):
            gg = gg.sort_values("时间戳(ms)")
            s = gg[STATE_COLS].values.astype(np.float32)
            a_row = gg.iloc[0]
            a_type = ACTION_TYPES.index(a_row["action_type"])
            a_val = float(a_row["action_value"])
            for t in range(HIST, len(s) - 1):
                Xh.append(s[t - HIST:t])
                Xa.append([a_type, a_val])
                Y.append(s[t + 1])
                meta.append((scene, action, ue, float(gg["时间戳(ms)"].iloc[t])))
    return (np.array(Xh), np.array(Xa, dtype=np.float32),
            np.array(Y, dtype=np.float32), meta)


def normalize(Xh, Y):
    mu = Xh.reshape(-1, Xh.shape[-1]).mean(0)
    sd = Xh.reshape(-1, Xh.shape[-1]).std(0) + 1e-6
    return (Xh - mu) / sd, (Y - mu) / sd, mu, sd


class GRUWM(nn.Module):
    def __init__(self, n_state=4, n_act=2, hid=64):
        super().__init__()
        self.act_emb = nn.Sequential(nn.Linear(n_act, 16), nn.ReLU())
        self.gru = nn.GRU(n_state + 16, hid, batch_first=True)
        self.head = nn.Sequential(nn.Linear(hid, 32), nn.ReLU(), nn.Linear(32, n_state))

    def forward(self, xh, xa):
        a = self.act_emb(xa).unsqueeze(1).expand(-1, xh.shape[1], -1)
        z, _ = self.gru(torch.cat([xh, a], -1))
        return self.head(z[:, -1])


def rmse_mae(pred, true):
    return float(np.sqrt(((pred - true) ** 2).mean())), float(np.abs(pred - true).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=512)
    args = ap.parse_args()
    torch.manual_seed(SEED)
    np.random.seed(SEED)

    ds = pd.read_parquet(GEN_DIR / "twin_action_dataset.parquet")
    Xh, Xa, Y, meta = build_sequences(ds)
    print(f"sequences: {len(Y):,} | hist {HIST}×{Xh.shape[2]} + action {Xa.shape[1]} -> {Y.shape[1]}")

    # 动作归一化: type 保持 int (嵌入), value z-score
    Xa_val = Xa[:, 1:2].copy()
    a_mu, a_sd = Xa_val.mean(), Xa_val.std() + 1e-6
    Xa_norm = np.column_stack([Xa[:, 0], (Xa_val - a_mu) / a_sd]).astype(np.float32)

    # 时间切分: t < 8000ms 训练, >= 8000ms 测试 (前 8s / 后 2s)
    t_arr = np.array([m[3] for m in meta])
    tr, te = t_arr < 8000, t_arr >= 8000
    Xh_n, Y_n, mu, sd = normalize(Xh[tr], Y[tr])
    Xh_te = (Xh[te] - mu) / sd
    Y_te = (Y[te] - mu) / sd
    print(f"train {tr.sum():,} / test {te.sum():,}")

    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    print("device:", dev)

    # ---- 基线 1: Persistence ----
    pers_pred = (Xh[te][:, -1] - mu) / sd          # s_t
    pers_rmse, pers_mae = rmse_mae(pers_pred, Y_te)

    # ---- 基线 2: Linear AR + 动作项 (岭回归, closed form) ----
    A = np.concatenate([Xh_n.reshape(len(Xh_n), -1), Xa_norm[tr]], 1)
    B = Y_n
    lam = 1.0
    W = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ B)
    A_te = np.concatenate([Xh_te.reshape(len(Xh_te), -1), Xa_norm[te]], 1)
    lin_pred = A_te @ W
    lin_rmse, lin_mae = rmse_mae(lin_pred, Y_te)

    # ---- 模型 3: GRU 世界模型 ----
    model = GRUWM().to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.MSELoss()
    Xh_t = torch.tensor(Xh_n, device=dev)
    Xa_t = torch.tensor(Xa_norm[tr], device=dev)
    Y_t = torch.tensor(Y_n, device=dev)
    Xh_te_t = torch.tensor(Xh_te, device=dev)
    Xa_te_t = torch.tensor(Xa_norm[te], device=dev)
    n = len(Xh_t)
    curve = []
    for ep in range(args.epochs):
        model.train()
        idx = torch.randperm(n, device=dev)
        tot = 0.0
        for i in range(0, n, args.batch):
            b = idx[i:i + args.batch]
            opt.zero_grad()
            out = model(Xh_t[b], Xa_t[b])
            loss = loss_fn(out, Y_t[b])
            loss.backward()
            opt.step()
            tot += float(loss) * len(b)
        curve.append(tot / n)
        if (ep + 1) % 10 == 0:
            print(f"epoch {ep+1}: loss {curve[-1]:.4f}")
    model.eval()
    with torch.no_grad():
        gru_pred = model(Xh_te_t, Xa_te_t).cpu().numpy()
    gru_rmse, gru_mae = rmse_mae(gru_pred, Y_te)

    # ---- 保存模型权重 + 归一化统计 (供 API 推理加载; 方案二 V0.2: 接入首个通过检查的模型) ----
    MODELS_DIR = PKG_DIR / "models"
    MODELS_DIR.mkdir(exist_ok=True)
    torch.save(model.state_dict(), MODELS_DIR / "gru_wm.pt")
    json.dump({
        "state_cols": STATE_COLS, "action_types": ACTION_TYPES, "hist": HIST,
        "mu": mu.tolist(), "sd": sd.tolist(),
        "a_mu": float(a_mu), "a_sd": float(a_sd),
        "seed": SEED, "epochs": args.epochs, "params": sum(p.numel() for p in model.parameters()),
        "trained_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "data": "twin_action_dataset.parquet (同根反事实分支, 83,200 样本)",
    }, open(MODELS_DIR / "gru_wm_meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[models] gru_wm.pt + gru_wm_meta.json -> {MODELS_DIR}")

    # 反归一化 per-KPI RMSE (dB/Mbps 原单位)
    def per_kpi(pred):
        return {c: round(float(np.sqrt(((pred - Y_te)[:, i] ** 2).mean()) * sd[i]), 3)
                for i, c in enumerate(STATE_COLS)}

    results = {
        "n_train": int(tr.sum()), "n_test": int(te.sum()),
        "split": "前 8s 训练 / 后 2s 测试 (时间外推)",
        "state_cols": STATE_COLS,
        "persistence": {"rmse_norm": round(pers_rmse, 4), "mae_norm": round(pers_mae, 4),
                        "rmse_per_kpi": per_kpi(pers_pred)},
        "linear_ar_action": {"rmse_norm": round(lin_rmse, 4), "mae_norm": round(lin_mae, 4),
                             "rmse_per_kpi": per_kpi(lin_pred)},
        "gru_world_model": {"rmse_norm": round(gru_rmse, 4), "mae_norm": round(gru_mae, 4),
                            "rmse_per_kpi": per_kpi(gru_pred),
                            "params": sum(p.numel() for p in model.parameters())},
        "improvement_vs_persistence_pct": round((1 - gru_rmse / pers_rmse) * 100, 1),
        "improvement_vs_linear_pct": round((1 - gru_rmse / max(lin_rmse, 1e-9)) * 100, 1),
    }
    (DOCS / "wm_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(json.dumps(results, ensure_ascii=False, indent=1))

    # loss 曲线
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(9, 3.2))
    ax[0].plot(curve); ax[0].set_title("GRU-WM train loss"); ax[0].set_xlabel("epoch")
    names = ["Persistence", "LinearAR+act", "GRU-WM"]
    rmses = [pers_rmse, lin_rmse, gru_rmse]
    ax[1].bar(names, rmses, color=["#8296b8", "#38bdf8", "#f472b6"])
    ax[1].set_title("test RMSE (normalized)")
    for i, v in enumerate(rmses):
        ax[1].text(i, v, f"{v:.3f}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    fig.savefig(DOCS / "wm_results.png", dpi=160)
    print("[docs] wm_results.json / wm_results.png")


if __name__ == "__main__":
    main()
