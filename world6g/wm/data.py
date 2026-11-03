#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据协议: 反事实动作条件世界模型基准
====================================
数据源: twin_action_dataset.parquet — 同根反事实分支
  对每个 (场景 × UE): 基准分支 (a=∅) 与动作分支 (a≠∅) 共享同一几何/阴影/噪声流,
  因此 Δs = s^(a) − s^(∅) 是动作的精确因果效应 (无混淆)。

三任务:
  Task A "预测下一步"      : (s_{t-H+1..t}, a) → s_{t+1}           — 单步, 动作恒定
  Task B "多步 rollout"    : 自回归 H∈{1..40}, 误差-步长曲线        — 复合误差分析
  Task C "反事实 Δ-效应"   : (基准轨迹 s^(∅), a) → s^(a)_{t+1..t+K} — 动作条件核心问题
                             评价 Δs 预测 (Δ=0 为零假设基线)

动作向量: [type_idx (int), value (float, z-score)] — 与现有 twm_serve 兼容
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datalake.config import GEN_DIR

# ---------------- 常量 (与 train_world_model.py / twm_serve.py 一致) ----------------
STATE_COLS = ["服务小区RSRP(dBm)", "服务小区SINR(dB)", "下行速率(Mbps)", "CQI"]
ACTION_TYPES = ["baseline", "pmax_scale", "prb_scale", "az_offset", "ho_offset"]
SCENES = ["城区", "郊区", "低空", "机器人工厂"]
HIST = 8            # 历史窗 (×50ms = 400ms)
STEP_MS = 50.0
TRAIN_MS = 8000.0   # 时间外推切分: 前 8s 训练 / 后 2s 测试

DATASET = GEN_DIR / "twin_action_dataset.parquet"


# ---------------- 分支索引数据结构 ----------------
@dataclass
class Branch:
    """一条轨迹分支: 单 (scene, action_label, UEID) 的状态序列 [T, S]"""
    scene: str
    action_label: str
    action_type: str          # ACTION_TYPES 之一
    action_value: float       # 动作参数值 (baseline=0)
    ue: str
    states: np.ndarray        # [T, S] float32, 原始单位
    times_ms: np.ndarray      # [T]


@dataclass
class SameRootGroup:
    """同根组: 一条基准分支 + 其全部动作分支 (相同 scene/UE/seed → 相同随机流)"""
    scene: str
    ue: str
    baseline: Branch
    actions: list = field(default_factory=list)   # list[Branch], a≠∅

    def find(self, label: str) -> Branch | None:
        for b in self.actions:
            if b.action_label == label:
                return b
        return None


def load_branches(ds: pd.DataFrame | None = None) -> list[SameRootGroup]:
    """读取 parquet → 同根组列表.

    返回 32 组 (4 场景 × 8 UE), 每组含 1 基准 + 12 动作分支 (各 200 步)。
    """
    if ds is None:
        ds = pd.read_parquet(DATASET)
    groups: dict[tuple, SameRootGroup] = {}
    for (scene, action), g in ds.groupby(["branch_scene", "action"], sort=False):
        a_row = g.iloc[0]
        a_type = a_row["action_type"]
        a_val = float(a_row["action_value"])
        for ue, gg in g.groupby("UEID", sort=False):
            gg = gg.sort_values("时间戳(ms)")
            br = Branch(scene=scene, action_label=action, action_type=a_type,
                        action_value=a_val, ue=str(ue),
                        states=gg[STATE_COLS].values.astype(np.float32),
                        times_ms=gg["时间戳(ms)"].values.astype(np.float64))
            key = (scene, str(ue))
            if key not in groups:
                groups[key] = SameRootGroup(scene=scene, ue=str(ue),
                                            baseline=br)   # 首见记为基准
            elif action == "baseline":
                groups[key].baseline = br
            else:
                groups[key].actions.append(br)
    # 校验: 每组动作分支时间轴与基准完全对齐
    out = list(groups.values())
    for grp in out:
        assert grp.baseline is not None, f"missing baseline for {grp.scene}/{grp.ue}"
        for b in grp.actions:
            assert len(b.states) == len(grp.baseline.states), "branch length mismatch"
            assert np.allclose(b.times_ms, grp.baseline.times_ms), "branch time misaligned"
    return out


# ---------------- 动作编码 ----------------
def action_vec(action_type: str, action_value: float) -> np.ndarray:
    """[type_idx, value] — 与 twm_serve._action_vec 同构"""
    return np.array([ACTION_TYPES.index(action_type), action_value], dtype=np.float32)


# ---------------- Task A/B: 分支内滑窗序列 ----------------
def branch_windows(groups: list[SameRootGroup], hist: int = HIST):
    """每条分支 (基准+动作) 的滑窗样本.

    修正原协议 off-by-one: Xh 取 s[t-hist+1 .. t] (右端含 t), Y 取 s[t+1] → 真单步。
    返回 dict: samples 列表, 每项含
      xh [hist,S], xa [2], y [S], t_ms (目标时刻), scene, action_label, ue, is_action
    """
    samples = []
    for grp in groups:
        for br in [grp.baseline] + grp.actions:
            xa = action_vec(br.action_type, br.action_value)
            s = br.states
            for t in range(hist - 1, len(s) - 1):
                samples.append({
                    "xh": s[t - hist + 1: t + 1],
                    "xa": xa,
                    "y": s[t + 1],
                    "t_ms": float(br.times_ms[t + 1]),
                    "scene": br.scene, "action_label": br.action_label,
                    "ue": br.ue,
                    "is_action": br.action_type != "baseline",
                    "action_type": br.action_type,
                })
    return samples


# ---------------- Task C: 反事实对 (基准 → 动作分支) ----------------
def counterfactual_pairs(groups: list[SameRootGroup], hist: int = HIST):
    """同根反事实对: 输入=基准分支轨迹 (a=∅), 目标=动作分支 (a≠∅) 同时刻状态.

    每个样本: xh = s^(∅)[t-hist+1..t], xa = 动作, y = s^(a)[t+1]
    附加: y_base = s^(∅)[t+1] (供 Δ 评价: Δs* = y − y_base)
    这是动作条件任务的核心: 历史不含动作效应, 模型必须从动作参数推断因果效应。
    """
    samples = []
    for grp in groups:
        sb = grp.baseline.states
        tb = grp.baseline.times_ms
        for br in grp.actions:
            xa = action_vec(br.action_type, br.action_value)
            sa = br.states
            for t in range(hist - 1, len(sb) - 1):
                samples.append({
                    "xh": sb[t - hist + 1: t + 1],
                    "xa": xa,
                    "y": sa[t + 1],
                    "y_base": sb[t + 1],
                    "t_ms": float(tb[t + 1]),
                    "scene": br.scene, "action_label": br.action_label,
                    "ue": br.ue, "action_type": br.action_type,
                    "action_value": br.action_value,
                })
    return samples


# ---------------- 归一化 (训练段统计, 防泄漏) ----------------
@dataclass
class Norm:
    mu: np.ndarray
    sd: np.ndarray
    a_mu: float
    a_sd: float

    def state(self, x):
        return (x - self.mu) / self.sd

    def state_inv(self, x):
        return x * self.sd + self.mu

    def act(self, xa: np.ndarray):
        out = xa.copy()
        out[:, 1] = (xa[:, 1] - self.a_mu) / self.a_sd
        return out.astype(np.float32)

    def to_json(self):
        return {"mu": self.mu.tolist(), "sd": self.sd.tolist(),
                "a_mu": float(self.a_mu), "a_sd": float(self.a_sd)}

    @staticmethod
    def from_json(d):
        return Norm(np.array(d["mu"]), np.array(d["sd"]), d["a_mu"], d["a_sd"])


def fit_norm(samples: list[dict], train_ms: float = TRAIN_MS) -> Norm:
    """仅用训练时间段的样本统计归一化参数"""
    tr = [s for s in samples if s["t_ms"] < train_ms]
    xh = np.stack([s["xh"] for s in tr])
    y = np.stack([s["y"] for s in tr])
    xa = np.stack([s["xa"] for s in tr])
    all_states = np.concatenate([xh.reshape(-1, xh.shape[-1]), y], 0)
    mu = all_states.mean(0)
    sd = all_states.std(0) + 1e-6
    a_mu = float(xa[:, 1].mean())
    a_sd = float(xa[:, 1].std() + 1e-6)
    return Norm(mu, sd, a_mu, a_sd)


def train_test_split(samples: list[dict], train_ms: float = TRAIN_MS):
    """时间外推切分 (t < 8000ms / >= 8000ms), 与现有协议一致"""
    tr = [s for s in samples if s["t_ms"] < train_ms]
    te = [s for s in samples if s["t_ms"] >= train_ms]
    return tr, te


def to_arrays(samples: list[dict]):
    """样本列表 → (Xh [N,H,S], Xa [N,2], Y [N,S])"""
    Xh = np.stack([s["xh"] for s in samples]).astype(np.float32)
    Xa = np.stack([s["xa"] for s in samples]).astype(np.float32)
    Y = np.stack([s["y"] for s in samples]).astype(np.float32)
    return Xh, Xa, Y


if __name__ == "__main__":
    groups = load_branches()
    n_pairs = sum(len(g.actions) for g in groups)
    print(f"groups={len(groups)}  action-branches={n_pairs}")
    ws = branch_windows(groups)
    print(f"Task A/B windows: {len(ws):,}")
    tr, te = train_test_split(ws)
    print(f"  train={len(tr):,}  test={len(te):,}")
    cf = counterfactual_pairs(groups)
    print(f"Task C counterfactual pairs: {len(cf):,}")
    trc, tec = train_test_split(cf)
    print(f"  train={len(trc):,}  test={len(tec):,}")
    norm = fit_norm(ws)
    print("norm.mu =", norm.mu.round(2), " norm.sd =", norm.sd.round(2))
