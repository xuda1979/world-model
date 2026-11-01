#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
世界模型推理服务 (GRU-WM Serving)
==================================
方案二 §四: 11月13日 V0.2 "接入首个通过检查的模型; 未通过的任务保留原引擎"
- 加载 models/gru_wm.pt (动作条件 GRU 世界模型, 训练自同根反事实分支数据)
- 提供: 多步 rollout KPI 预测 / 动作候选效应预测 (与 Persistence 基线对照)
- 所有输出带来源标注 ("世界模型预测" / "基线预测") 与模型版本
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

PKG_DIR = Path(__file__).resolve().parent
MODELS_DIR = PKG_DIR / "models"
GEN_DIR = PKG_DIR / "datalake" / "generated"

MODEL_VERSION = "GRU-WM v0.2 (seed42, 18,772 params, twin_action_dataset)"


class GRUWM(nn.Module):
    """与 train_world_model.py 完全一致的结构 (加载权重用)"""

    def __init__(self, n_state=4, n_act=2, hid=64):
        super().__init__()
        self.act_emb = nn.Sequential(nn.Linear(n_act, 16), nn.ReLU())
        self.gru = nn.GRU(n_state + 16, hid, batch_first=True)
        self.head = nn.Sequential(nn.Linear(hid, 32), nn.ReLU(), nn.Linear(32, n_state))

    def forward(self, xh, xa):
        a = self.act_emb(xa).unsqueeze(1).expand(-1, xh.shape[1], -1)
        z, _ = self.gru(torch.cat([xh, a], -1))
        return self.head(z[:, -1])


_MODEL = None
_META = None


def load_model():
    """懒加载 GRU-WM 权重; 失败返回 None (保留基线引擎, 方案二要求)"""
    global _MODEL, _META
    if _MODEL is not None:
        return _MODEL
    try:
        meta = json.loads((MODELS_DIR / "gru_wm_meta.json").read_text(encoding="utf-8"))
        model = GRUWM()
        model.load_state_dict(torch.load(MODELS_DIR / "gru_wm.pt", map_location="cpu"))
        model.eval()
        _MODEL, _META = model, meta
        return model
    except Exception:
        return None


def _action_vec(action: dict) -> np.ndarray:
    """动作 → (type_idx, value). type 与训练时 ACTION_TYPES 对齐"""
    types = _META["action_types"]
    if action is None or action.get("type") == "baseline":
        return np.array([0, 0.0], dtype=np.float32)
    t = str(action.get("type", "baseline"))
    ti = types.index(t) if t in types else 0
    v = float(action.get("value", 0.0))
    return np.array([ti, v], dtype=np.float32)


def predict_rollout(hist_states: np.ndarray, action: dict, steps: int = 40):
    """多步自回归 rollout.
    hist_states: [H, S] 原始单位状态窗; action: {type, value}; steps: 步数 (×50ms)
    返回: preds [steps, S] 原始单位 + 来源标注"""
    model = load_model()
    mu = np.array(_META["mu"]); sd = np.array(_META["sd"])
    a_mu, a_sd = _META["a_mu"], _META["a_sd"]
    if model is None:
        return None
    h = (hist_states - mu) / sd                      # [H,S] normalized
    av = _action_vec(action)
    av_n = np.array([av[0], (av[1] - a_mu) / a_sd], dtype=np.float32)
    preds = []
    with torch.no_grad():
        for _ in range(steps):
            xh = torch.tensor(h[np.newaxis], dtype=torch.float32)
            xa = torch.tensor(av_n[np.newaxis], dtype=torch.float32)
            y = model(xh, xa).numpy()[0]              # normalized next
            preds.append(y)
            h = np.vstack([h[1:], y])                 # 滚动窗
    preds = np.array(preds) * sd + mu
    return preds


def persistence_rollout(hist_states: np.ndarray, steps: int = 40):
    """Persistence 基线: ŝ_{t+k} = s_t (无学习下界)"""
    return np.tile(hist_states[-1], (steps, 1))


def evaluate_on_action_dataset(action_type: str | None = None):
    """在 twin_action_dataset 测试段 (t>=8000ms) 上评测模型 vs Persistence 基线.
    与 train_world_model.py 相同的滑窗构造; 返回分 KPI 误差 (原始单位)"""
    model = load_model()
    if model is None:
        return None
    ds = pd.read_parquet(GEN_DIR / "twin_action_dataset.parquet")
    mu = np.array(_META["mu"]); sd = np.array(_META["sd"])
    a_mu, a_sd = _META["a_mu"], _META["a_sd"]
    cols = _META["state_cols"]; H = _META["hist"]
    if action_type:
        ds = ds[ds["action_type"] == action_type]
        if len(ds) < 100:
            return None

    # 滑窗序列 (与训练一致: (branch_scene, action) × UEID 分组)
    Xh_l, Xa_l, Y_l = [], [], []
    for (scene, action), g in ds.groupby(["branch_scene", "action"]):
        for ue, gg in g.groupby("UEID"):
            gg = gg.sort_values("时间戳(ms)")
            # 只取测试段 (窗口末点 t+1 >= 8000ms)
            s = gg[cols].values.astype(np.float32)
            t_arr = gg["时间戳(ms)"].values.astype(float)
            a_row = gg.iloc[0]
            a_ti = _META["action_types"].index(a_row["action_type"])
            a_v = float(a_row["action_value"])
            for t in range(H, len(s) - 1):
                if t_arr[t + 1] < 8000:
                    continue
                Xh_l.append(s[t - H:t]); Xa_l.append([a_ti, a_v]); Y_l.append(s[t + 1])
    if not Xh_l:
        return None
    Xh = np.array(Xh_l, np.float32); Xa = np.array(Xa_l, np.float32)
    Y = np.array(Y_l, np.float32)
    Xh_n = (Xh - mu) / sd
    a_n = np.column_stack([Xa[:, 0], (Xa[:, 1] - a_mu) / a_sd]).astype(np.float32)
    with torch.no_grad():
        pred = model(torch.tensor(Xh_n, dtype=torch.float32),
                     torch.tensor(a_n, dtype=torch.float32)).numpy() * sd + mu
    pers = Xh[:, -1].copy()
    out = {"n": int(len(Y)), "model_version": MODEL_VERSION, "per_kpi": {}}
    for j, c in enumerate(cols):
        out["per_kpi"][c] = {
            "wm_mae": round(float(np.abs(pred[:, j] - Y[:, j]).mean()), 2),
            "pers_mae": round(float(np.abs(pers[:, j] - Y[:, j]).mean()), 2),
        }
    out["wm_rmse_norm"] = round(float(np.sqrt((((pred - Y) / sd) ** 2).mean())), 4)
    out["pers_rmse_norm"] = round(float(np.sqrt((((pers - Y) / sd) ** 2).mean())), 4)
    return out


def model_info() -> dict:
    if _META is None and load_model() is None:
        return {"available": False, "reason": "gru_wm.pt 未找到或加载失败 — 保留基线引擎"}
    return {"available": True, "version": MODEL_VERSION,
            "params": _META["params"], "trained_at": _META["trained_at"],
            "data": _META["data"], "hist": _META["hist"],
            "state_cols": _META["state_cols"]}


if __name__ == "__main__":
    print(json.dumps(model_info(), ensure_ascii=False, indent=1))
    # 冒烟测试: 用 twin_01 前窗 rollout
    df = pd.read_parquet(GEN_DIR / "twin_01_ue.parquet")
    cols = _META["state_cols"]
    g = df[df.UEID == df.UEID.iloc[0]].sort_values("时间戳(ms)")
    hist = g[cols].values[:8]
    t0 = time.time()
    preds = predict_rollout(hist, {"type": "baseline"}, steps=40)
    dt = (time.time() - t0) * 1000
    print(f"rollout 40 步耗时 {dt:.1f} ms | 前 3 步 RSRP: {preds[:3, 0].round(1)}")
    pers = persistence_rollout(hist, 40)
    print(f"persistence RSRP: {pers[:3, 0].round(1)}")
