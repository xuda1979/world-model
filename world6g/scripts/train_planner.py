#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动作模型 (Policy) 训练: CEM-MPC 策略蒸馏
==========================================
目标: 把 CEM-MPC 规划器蒸馏为一步前馈动作模型 (policy network),
      实时性: 规划器 ~秒级 → 策略网络 ~毫秒级, 精度接近规划器。

方法 (DAgger 式简化版 — imitation learning):
  1. 专家数据: 对 twin_action_dataset 基准分支的每个 (场景×UE×时刻窗),
     用 CEM-MPC 在世界模型内规划 → 专家动作 a* = argmax reward。
     (窗口 t 遍历测试段外的训练段, 历史窗 s[t-7..t])
  2. 策略网络: MLP 输入 = 归一化历史窗 [H,S] 展平, 输出 = 动作分布
     - 分类头: 动作类型 (4 类干预 + 不干预)
     - 回归头: 动作值 (对每类一个值, 选类型后取对应值)
  3. 损失: CE(type) + chosen-type MSE(value) + 不干预置信正则。
  4. 评测: 在留出时段上对比 策略网络 vs CEM-MPC 的动作一致率 +
     世界模型内奖励差距 (policy score / planner score)。

输出:
  models/policy_net.pt          — PolicyNet 权重
  models/policy_meta.json       — 归一化统计 + 动作空间 (可服务化加载)

用法:
  cd world6g && ../.venv/bin/python scripts/train_planner.py [--epochs 60] [--n-window 512]
"""
import sys, json, time, argparse
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wm.data as D
from wm.planner import (WorldModel, CEMPlanner, CEMConfig, RewardConfig,
                        make_reward, ACTION_SPACE, ACTION_TYPES, DEFAULT_W)
from wm.data import HIST, STATE_COLS
from wm.models import N_STATE

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
SEED = 42

# ---------------- 动作空间编码 ----------------
INT_TYPES = ["none", "pmax_scale", "prb_scale", "az_offset", "ho_offset"]  # 5 类含不干预
TYPE2IDX = {t: i for i, t in enumerate(INT_TYPES)}


# ============================================================
# 策略网络: MLP 双头 (类型 + 值)
# ============================================================
class PolicyNet(nn.Module):
    """输入: 归一化历史窗 [B,H*S]; 输出: type_logits [B,5], values [B,4]"""

    def __init__(self, hist: int = HIST, n_state: int = N_STATE,
                 hidden: int = 128, n_type: int = 5, n_value: int = 4):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(hist * n_state, hidden), nn.GELU(),
            nn.Linear(hidden, hidden), nn.GELU(),
            nn.Linear(hidden, hidden // 2), nn.GELU(),
        )
        self.type_head = nn.Linear(hidden // 2, n_type)
        self.value_head = nn.Linear(hidden // 2, n_value)
        # 值域缩放 (回归目标归一化到 [-1,1])
        self.register_buffer("v_lo", torch.tensor(
            [ACTION_SPACE[t]["lo"] for t in ["pmax_scale", "prb_scale", "az_offset", "ho_offset"]],
            dtype=torch.float32))
        self.register_buffer("v_hi", torch.tensor(
            [ACTION_SPACE[t]["hi"] for t in ["pmax_scale", "prb_scale", "az_offset", "ho_offset"]],
            dtype=torch.float32))

    def norm_value(self, values_raw: torch.Tensor) -> torch.Tensor:
        """原始动作值 → [-1,1] (min-max 归一化, 按类型列)"""
        return 2 * (values_raw - self.v_lo) / (self.v_hi - self.v_lo) - 1

    def denorm_value(self, values_n: torch.Tensor) -> torch.Tensor:
        return (values_n + 1) / 2 * (self.v_hi - self.v_lo) + self.v_lo

    def forward(self, x: torch.Tensor):
        h = self.net(x)
        return self.type_head(h), self.value_head(h)


# ============================================================
# 专家数据生成: CEM-MPC 规划 → (窗口, 专家动作) 对
# ============================================================
def gen_expert(planner: CEMPlanner, groups, n_windows: int = 512,
               seed: int = SEED, lam_action: float = 0.02):
    """从基准分支 (a=∅) 采样历史窗, CEM-MPC 输出专家动作.

    专家标签: planner.recommend 为真 → (type, value); 否则 → 不干预 (none)。
    返回: X [N, H*S] (归一化), type_idx [N], value_raw [N,4] (每类一组值, 监督选中类)。
    """
    rng = np.random.default_rng(seed)
    mu, sd = planner.wm.mu, planner.wm.sd

    # 收集所有基准分支的可用窗口 (训练段: t_ms < 8000)
    wins = []
    for grp in groups:
        br = grp.baseline
        for t in range(HIST - 1, len(br.states) - 1):
            if br.times_ms[t] >= 8000.0:
                continue
            wins.append((br.states[t - HIST + 1: t + 1], br.scene, br.ue, float(br.times_ms[t])))
    idx = rng.choice(len(wins), size=min(n_windows, len(wins)), replace=False)

    X, t_lab, v_lab = [], [], []
    t0 = time.time()
    n_rec = 0
    for i in idx:
        hist_s, scene, ue, tms = wins[i]
        res = planner.plan(hist_s[np.newaxis], return_all=False)
        if res["recommend"]:
            t_lab.append(TYPE2IDX[res["type"]])
            v_lab.append([res["value"] if t == res["type"] else 0.0
                          for t in ["pmax_scale", "prb_scale", "az_offset", "ho_offset"]])
            n_rec += 1
        else:
            t_lab.append(TYPE2IDX["none"])
            v_lab.append([0.0] * 4)
        X.append(((hist_s - mu) / sd).reshape(-1))
    print(f"  expert windows: {len(X):,}  (recommend={n_rec}, "
          f"none={len(X) - n_rec})  [{time.time() - t0:.0f}s]")
    return (np.array(X, dtype=np.float32),
            np.array(t_lab, dtype=np.int64),
            np.array(v_lab, dtype=np.float32))


# ============================================================
# 训练循环
# ============================================================
def train_policy(net: PolicyNet, X, t_lab, v_lab, epochs: int = 60,
                 batch: int = 256, lr: float = 1e-3, seed: int = SEED,
                 val_frac: float = 0.15, lam_v: float = 1.0):
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)

    n = len(X)
    perm = rng.permutation(n)
    n_val = max(int(n * val_frac), 8)
    vi, ti = perm[:n_val], perm[n_val:]
    X_t = torch.tensor(X[ti]); T_t = torch.tensor(t_lab[ti]); V_t = torch.tensor(v_lab[ti])
    X_v = torch.tensor(X[vi]); T_v = torch.tensor(t_lab[vi]); V_v = torch.tensor(v_lab[vi])

    opt = torch.optim.Adam(net.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    n_tr = len(X_t)
    best_acc, best_state, curve = -1.0, None, []
    t0 = time.time()
    for ep in range(epochs):
        net.train()
        idx = torch.randperm(n_tr)
        tot = 0.0
        for i in range(0, n_tr, batch):
            b = idx[i: i + batch]
            logits, vals = net(X_t[b])
            loss_t = F.cross_entropy(logits, T_t[b])
            # 值回归: 仅监督被选中的类型 (type≠none 的样本, 其对应列)
            mask = (T_t[b] != TYPE2IDX["none"]).float()               # [B]
            tgt = net.norm_value(V_t[b])                               # [B,4] → [-1,1]
            # 每样本选中类型的列索引 (0..3); none → 0 (被 mask 掉)
            col = torch.clamp(T_t[b] - 1, min=0)
            pred_v = vals.gather(1, col.unsqueeze(1)).squeeze(1)
            loss_v = (mask * (pred_v - tgt.gather(1, col.unsqueeze(1)).squeeze(1)) ** 2).sum() / mask.sum().clamp(min=1)
            loss = loss_t + lam_v * loss_v
            opt.zero_grad(); loss.backward(); opt.step()
            tot += float(loss) * len(b)
        sched.step()
        curve.append(tot / n_tr)

        # 验证: 类型准确率
        net.eval()
        with torch.no_grad():
            logits_v, _ = net(X_v)
            acc = float((logits_v.argmax(1) == T_v).float().mean())
        if acc >= best_acc:
            best_acc = acc
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
        if (ep + 1) % 10 == 0:
            print(f"  epoch {ep + 1:3d}: loss {curve[-1]:.4f}  val-type-acc {acc:.3f}  ({time.time() - t0:.0f}s)")
    if best_state is not None:
        net.load_state_dict(best_state)
    return net, best_acc, curve


# ============================================================
# 评测: 策略网络 vs CEM-MPC (世界模型内奖励 + 一致率)
# ============================================================
def eval_policy(net: PolicyNet, planner: CEMPlanner, groups,
                reward_fn, n_windows: int = 128, seed: int = 7):
    """留出段 (t_ms ≥ 8000) 上:
    1) 类型一致率 + 值 MAE (仅类型匹配时)
    2) WM 内奖励: policy score vs planner score (重规划 CEM 作参照)
    """
    rng = np.random.default_rng(seed)
    mu, sd = planner.wm.mu, planner.wm.sd

    wins = []
    for grp in groups:
        br = grp.baseline
        for t in range(HIST - 1, len(br.states) - 1):
            if br.times_ms[t] < 8000.0:
                continue
            wins.append((br.states[t - HIST + 1: t + 1], br.scene, br.ue))
    idx = rng.choice(len(wins), size=min(n_windows, len(wins)), replace=False)

    n_match_type, n_rec_pl, n_rec_po = 0, 0, 0
    val_errs, score_gap = [], []
    planner_scores, policy_scores, none_scores = [], [], []
    for i in idx:
        hist_s, scene, ue = wins[i]
        # CEM-MPC 参照
        res = planner.plan(hist_s[np.newaxis])
        # 策略网络
        x = torch.tensor(((hist_s - mu) / sd).reshape(1, -1), dtype=torch.float32)
        with torch.no_grad():
            logits, vals = net(x)
        t_i = int(logits.argmax(1)[0])
        if t_i == TYPE2IDX["none"]:
            po_type, po_val = "none", 0.0
            sc_po = float(res["baseline_score"])       # 不干预 = baseline 奖励
            none_scores.append(sc_po)
        else:
            po_type = INT_TYPES[t_i]
            po_val = float(net.denorm_value(vals)[0, t_i - 1])
            ti_arr = np.array([ACTION_SPACE[po_type]["idx"]], dtype=np.float32)
            v_arr = np.array([po_val], dtype=np.float32)
            sc_po = float(reward_fn(planner.wm, hist_s[np.newaxis], ti_arr, v_arr)[0])
            n_rec_po += 1
        sc_pl = res["score"] if res["recommend"] else res["baseline_score"]
        if res["recommend"]:
            n_rec_pl += 1
            planner_scores.append(sc_pl)
        policy_scores.append(sc_po)
        # 一致性
        if (res["recommend"] and t_i != TYPE2IDX["none"] and po_type == res["type"]):
            n_match_type += 1
            val_errs.append(abs(po_val - res["value"]))
        score_gap.append(sc_po - sc_pl)

    out = {
        "n_windows": len(idx),
        "type_match_rate": n_match_type / max(n_rec_pl, 1),
        "value_mae (matched)": float(np.mean(val_errs)) if val_errs else float("nan"),
        "planner_recommend_rate": n_rec_pl / len(idx),
        "policy_recommend_rate": n_rec_po / len(idx),
        "wm_reward_planner": float(np.mean(planner_scores)) if planner_scores else float("nan"),
        "wm_reward_policy": float(np.mean(policy_scores)),
        "reward_gap_policy_minus_planner": float(np.mean(score_gap)),
    }
    return out


# ============================================================
# main
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--n-window", type=int, default=512,
                    help="专家窗口数 (每窗口一次 CEM 规划, ~1-2s)")
    ap.add_argument("--lam-action", type=float, default=0.02,
                    help="专家规划的动作幅值惩罚 (鼓励温和干预)")
    args = ap.parse_args()

    torch.manual_seed(SEED)
    np.random.seed(SEED)

    print("[1/4] 加载世界模型 + 同根分支数据 ...")
    groups = D.load_branches()
    rcfg = RewardConfig(lam_action=args.lam_action, k=8)
    reward_fn = make_reward(rcfg)
    planner = CEMPlanner(reward=reward_fn, rcfg=rcfg)

    print(f"[2/4] 生成专家数据 (CEM-MPC, {args.n_window} 窗) ...")
    X, t_lab, v_lab = gen_expert(planner, groups, n_windows=args.n_window)
    print(f"  type distribution: "
          f"{ {t: int((t_lab == TYPE2IDX[t]).sum()) for t in INT_TYPES} }")

    print(f"[3/4] 训练策略网络 ({args.epochs} epochs) ...")
    net = PolicyNet()
    n_par = sum(p.numel() for p in net.parameters())
    print(f"  PolicyNet params: {n_par:,}")
    net, best_acc, curve = train_policy(net, X, t_lab, v_lab, epochs=args.epochs)

    print("[4/4] 留出段评测 (policy vs CEM-MPC) ...")
    ev = eval_policy(net, planner, groups, reward_fn, n_windows=128)
    for k, v in ev.items():
        print(f"  {k:34s} {v}")

    # 导出
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(net.state_dict(), MODELS_DIR / "policy_net.pt")
    meta = {
        "state_cols": STATE_COLS,
        "hist": HIST,
        "mu": planner.wm.mu.tolist(),
        "sd": planner.wm.sd.tolist(),
        "int_types": INT_TYPES,
        "action_space": {t: ACTION_SPACE[t] for t in ACTION_SPACE},
        "hidden": 128,
        "seed": SEED,
        "epochs": args.epochs,
        "n_window": args.n_window,
        "lam_action": args.lam_action,
        "params": int(n_par),
        "val_type_acc": float(best_acc),
        "eval": {k: (None if isinstance(v, float) and np.isnan(v) else v)
                 for k, v in ev.items()},
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "objective": ("policy distillation from CEM-MPC (GRU-WM-MS reward), "
                      "dual-head MLP: type CE + chosen-value MSE"),
        "source_wm": "models/gru_wm_ms.pt (seed42, 多步一致性)",
    }
    (MODELS_DIR / "policy_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] models/policy_net.pt ({n_par:,} params) + policy_meta.json")


if __name__ == "__main__":
    main()
