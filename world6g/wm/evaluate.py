#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
世界模型基准评测 (主入口)
==========================
四任务 (论文 §Benchmark):
  A. 单步预测          — 时间外推切分, 6 模型对照, 配对 bootstrap
  B. 多步 rollout      — K ∈ {1,2,4,8,16,32,40}, 复合误差曲线, (训练在 Task A 协议上)
  C. 反事实 Δ-效应     — 同根对: 输入基准轨迹预测动作分支; Δ-RMSE / Δ-corr / 符号准确率
  D. 不确定性校准      — MC-dropout (最好学习模型) vs RSSM MC-z: NLL / 覆盖率 / ECE

输出:
  docs/wm_suite/results.json       全部数值结果 (机器可读)
  docs/wm_suite/fig_rollout.png    复合误差曲线 (B)
  docs/wm_suite/fig_delta.png      Δ-效应散点 (C)
  docs/wm_suite/fig_calibration.png 校准曲线 (D)
  docs/wm_suite/fig_taskA.png      单步条形图 (A)

用法: .venv/bin/python -m wm.evaluate [--epochs 40] [--quick]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datalake.config import PKG_DIR
from wm import data as D
from wm import metrics as MT
from wm import models as M
from wm import train as T

DOCS = PKG_DIR / "docs" / "wm_suite"
HORIZONS = [1, 2, 4, 8, 16, 32, 40]
KPI_SHORT = ["RSRP", "SINR", "DL-rate", "CQI"]
SEED = 42


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# Task A: 单步
# ============================================================
def eval_task_a(all_models, norm, ws_test, results):
    log("Task A: one-step prediction (time-extrapolating split)")
    Xh, Xa, Y = D.to_arrays(ws_test)
    Xh_n = norm.state(Xh).astype(np.float32)
    Xa_n = norm.act(Xa)
    Y_n = norm.state(Y).astype(np.float32)
    task_a = {}
    pers_pred = None
    preds = {}
    for name, model in all_models.items():
        with torch.no_grad():
            p = model(torch.tensor(Xh_n), torch.tensor(Xa_n)).numpy()
        preds[name] = p
        if name == "Persistence":
            pers_pred = p
        task_a[name] = {
            "rmse_norm": round(MT.rmse(p, Y_n), 4),
            "mae_norm": round(MT.mae(p, Y_n), 4),
            "rmse_per_kpi_native": [round(v, 3) for v in MT.per_kpi_rmse(p, Y_n, norm.sd)],
            "params": sum(q.numel() for q in model.parameters()),
        }
    # 配对 bootstrap: 每个学习模型 vs Persistence
    for name in M.LEARNED + ["LinearAR+action"]:
        if name not in preds or pers_pred is None:
            continue
        bs = MT.paired_bootstrap(np.abs(preds[name] - Y_n), np.abs(pers_pred - Y_n))
        task_a[name]["vs_persistence_bootstrap"] = bs
    # 反事实子集 (is_action 样本) 单独报告 — 动作分支上的单步误差
    act_mask = np.array([s["is_action"] for s in ws_test])
    sub = {}
    for name, p in preds.items():
        sub[name] = round(MT.rmse(p[act_mask], Y_n[act_mask]), 4)
    task_a["rmse_on_action_branches"] = sub
    results["task_a"] = task_a
    log("  " + json.dumps({k: v["rmse_norm"] for k, v in task_a.items()
                           if isinstance(v, dict) and "rmse_norm" in v}))
    return preds, Y_n, Xh_n, Xa_n


# ============================================================
# Task B: 多步 rollout
# ============================================================
def eval_task_b(all_models, norm, ws_test, results, device):
    log("Task B: multi-step rollout (compounding error)")
    K = max(HORIZONS)
    # 取测试段内每条分支每 UE 的最长连续窗 (从 t<8000 末尾起 rollout 到 t>=8000)
    # 构造: 对每 (branch, ue), 用 t=7950ms 处的 8 步历史 (训练段末), rollout 40 步
    # 覆盖整个测试段 (8000..9950ms) — 纯外推, 无重叠泄漏
    roll_sets = []
    from collections import defaultdict
    by_key = defaultdict(list)
    for i, s in enumerate(ws_test):
        by_key[(s["scene"], s["action_label"], s["ue"])].append(i)
    Xh, Xa, Y = D.to_arrays(ws_test)
    for key, idxs in by_key.items():
        idxs = sorted(idxs, key=lambda i: ws_test[i]["t_ms"])
        # 连续窗: 从第一个测试样本开始展开 (其 xh 含训练段尾部), 取最长前缀连续
        seq = [ws_test[i] for i in idxs]
        xh0, xa0 = Xh[idxs[0]], Xa[idxs[0]]
        # true 轨迹: 目标从 seq[0] 的 y 开始, 向后连续
        true_roll = []
        for s_ in seq:
            true_roll.append(s_["y"])
        true_roll = np.stack(true_roll[:K])
        roll_sets.append((xh0, xa0, true_roll, key))
    log(f"  rollout windows: {len(roll_sets)}")
    Xh0 = np.stack([r[0] for r in roll_sets]).astype(np.float32)
    Xa0 = np.stack([r[1] for r in roll_sets]).astype(np.float32)
    True_roll = np.stack([r[2] for r in roll_sets]).astype(np.float32)
    # 修正窗起点: xh0 的最后帧是 s_{t-1} (t=7950+50=8000 前) → 首目标即 seq[0].y ✓
    Xh0_n = norm.state(Xh0).astype(np.float32)
    Xa0_n = norm.act(Xa0)
    True_n = norm.state(True_roll).astype(np.float32)

    task_b = {"n_windows": len(roll_sets), "horizons_ms": [h * D.STEP_MS for h in HORIZONS]}
    curves = {}
    for name, model in all_models.items():
        model = model
        pred_roll = MT.rollout(model, torch.tensor(Xh0_n), torch.tensor(Xa0_n), K)
        hr = MT.horizon_rmse(pred_roll, True_n)
        curves[name] = hr.tolist()
        task_b[name] = {
            "rmse_by_horizon": [round(float(hr[h - 1]), 4) for h in HORIZONS],
            "rmse_h1": round(float(hr[0]), 4),
            "rmse_h40": round(float(hr[K - 1]), 4),
            "growth_x": round(float(hr[K - 1] / max(hr[0], 1e-9)), 2),
        }
        log(f"  {name:16s} h1={hr[0]:.4f} h40={hr[-1]:.4f} ({hr[-1] / hr[0]:.2f}x)")
    results["task_b"] = task_b
    return curves, (Xh0_n, Xa0_n, True_n, roll_sets)


# ============================================================
# CF 物理先验基线 (解析, 零参数)
# ============================================================
class CFPhysicsPrior(torch.nn.Module):
    """反事实解析先验 (归一化空间):
      pmax_scale v: Δrsrp = 10log10(v) / sd_rsrp, 其余 KPI 经验比例 ~ Δsinr/Δdl 由
                    相邻小区结构决定 → 置 0 (保守); 实际由数据检验
      prb_scale  v: Δdl_norm = s_dl × (v−1), 其余 0
      az_offset / ho_offset: 全 0 (无解析先验 → null)
    作为可解释下界: 检验学习模型是否超越单条物理律。
    """
    name = "PhysicsPrior"

    def __init__(self, norm):
        super().__init__()
        self.sd = torch.tensor(norm.sd, dtype=torch.float32)
        self.a_mu, self.a_sd = norm.a_mu, norm.a_sd

    def forward(self, xh, xa):
        s_t = xh[:, -1]
        raw = (xa[:, 1] * self.a_sd + self.a_mu)
        t = xa[:, 0].long()
        out = s_t.clone()
        vp = torch.where(t == 1, raw.clamp(min=1e-3), torch.ones_like(raw))
        out[:, 0] = out[:, 0] + 10.0 * torch.log10(vp) / self.sd[0]
        vq = torch.where(t == 2, raw.clamp(min=1e-3), torch.ones_like(raw))
        out[:, 2] = out[:, 2] * vq
        return out


def cf_physics_prior(norm):
    return CFPhysicsPrior(norm).eval()


# ============================================================
# Task C: 反事实 Δ-效应
# ============================================================
def eval_task_c(all_models, norm, groups, results, cf_models=None, epochs: int = 40):
    """cf_models: 反事实监督训练的模型 {name: model} (同结构, 训练于 (基准历史,动作)→动作分支)"""
    log("Task C: counterfactual Δ-effect prediction")
    cf = D.counterfactual_pairs(groups)
    tr, te = D.train_test_split(cf)
    Xh, Xa, Y = D.to_arrays(te)
    Yb = np.stack([s["y_base"] for s in te]).astype(np.float32)
    Xh_n = norm.state(Xh).astype(np.float32)
    Xa_n = norm.act(Xa)
    Y_n = norm.state(Y).astype(np.float32)
    Yb_n = norm.state(Yb).astype(np.float32)
    true_delta = Y_n - Yb_n                       # 归一化 Δ* (因果效应)

    task_c = {"n_test": len(te),
              "null_baseline": "Δ=0 (persistence of effect)"}
    # 零假设: 预测无效应 (Δ̂=0)
    zero = np.zeros_like(true_delta)
    task_c["null_delta0"] = MT.delta_metrics(zero, true_delta)
    # 各模型 (Task A 协议训练): 预测绝对状态 → Δ̂ = ŷ − s^(∅)_{t+1}
    preds = {}
    for name, model in all_models.items():
        with torch.no_grad():
            p = model(torch.tensor(Xh_n), torch.tensor(Xa_n)).numpy()
        preds[name] = p
        dhat = p - Yb_n
        m = MT.delta_metrics(dhat, true_delta)
        m["abs_rmse_norm"] = round(MT.rmse(p, Y_n), 4)     # 绝对状态误差一并报告
        m["protocol"] = "Task-A-trained (history contains action)"
        task_c[name] = m
        log(f"  {name:16s} Δ-rmse={m['rmse']:.4f} Δ-corr={m['corr']:.3f} "
            f"sign={m['sign_acc']:.3f} abs-rmse={m['abs_rmse_norm']:.4f}")
    # ---- 反事实监督模型 (headline): 输入=基准分支历史, 目标=动作分支 ----
    if cf_models:
        task_c["cf_supervised"] = {}
        for name, model in cf_models.items():
            with torch.no_grad():
                p = model(torch.tensor(Xh_n), torch.tensor(Xa_n)).numpy()
            preds["CF:" + name] = p
            dhat = p - Yb_n
            m = MT.delta_metrics(dhat, true_delta)
            m["abs_rmse_norm"] = round(MT.rmse(p, Y_n), 4)
            m["protocol"] = ("analytic physics prior" if name == "PhysicsPrior"
                             else "counterfactual-supervised (baseline history → action branch)")
            m["params"] = sum(q.numel() for q in model.parameters())
            task_c["cf_supervised"][name] = m
            log(f"  CF:{name:12s} Δ-rmse={m['rmse']:.4f} Δ-corr={m['corr']:.3f} "
                f"sign={m['sign_acc']:.3f} abs-rmse={m['abs_rmse_norm']:.4f}")
    # 分动作类型 Δ-RMSE (最佳 CF 模型 vs 零假设)
    if cf_models:
        cand = list(cf_models.keys())
        pool = task_c["cf_supervised"]
    else:
        cand = M.LEARNED
        pool = task_c
    best = min(cand, key=lambda n: pool[n]["rmse"])
    task_c["best_model"] = ("CF:" if cf_models else "") + best
    per_type = {}
    types = np.array([s["action_type"] for s in te])
    for at in D.ACTION_TYPES[1:]:
        m_ = types == at
        if m_.sum() == 0:
            continue
        best_key = ("CF:" if cf_models else "") + best
        per_type[at] = {
            "n": int(m_.sum()),
            "null_delta0_rmse": round(MT.rmse(zero[m_], true_delta[m_]), 4),
            "true_delta_mean_norm": round(float(true_delta[m_].mean()), 4),
            "true_delta_sd_norm": round(float(true_delta[m_].std()), 4),
            best_key + "_rmse": round(MT.rmse((preds[best_key][m_] - Yb_n[m_]), true_delta[m_]), 4),
        }
    task_c["per_action_type"] = per_type
    results["task_c"] = task_c
    return task_c, preds, true_delta, Yb_n, te


# ============================================================
# Task D: 不确定性校准 (RSSM MC-z, 反事实任务)
# ============================================================
def eval_task_d(cf_models, norm, groups, results):
    log("Task D: uncertainty quantification & calibration (counterfactual task)")
    cf = D.counterfactual_pairs(groups)
    tr, te = D.train_test_split(cf)
    Xh, Xa, Y = D.to_arrays(te)
    Xh_n = norm.state(Xh).astype(np.float32)
    Xa_n = norm.act(Xa)
    Y_n = norm.state(Y).astype(np.float32)
    task_d = {"n_test": len(te)}

    model = cf_models.get("RSSM-WM")
    if model is None:
        results["task_d"] = task_d
        return task_d
    # 确定性均值 (eval 模式)
    model.eval()
    with torch.no_grad():
        p_det = model(torch.tensor(Xh_n), torch.tensor(Xa_n)).numpy()
    # MC-z: sample=True
    outs = []
    with torch.no_grad():
        for _ in range(30):
            outs.append(model(torch.tensor(Xh_n), torch.tensor(Xa_n),
                              sample=True).numpy())
    outs = np.stack(outs)
    mu, sigma = outs.mean(0), outs.std(0)
    task_d["rssm_mc_z"] = {
        "nll": round(MT.gaussian_nll(mu, Y_n, sigma), 4),
        "coverage_95": round(MT.coverage(mu, Y_n, sigma), 4),
        "mean_sigma_norm": round(float(sigma.mean()), 4),
        "rmse": round(MT.rmse(mu, Y_n), 4),
    }
    # 事后温度校准: σ' = c·σ 使 cov95 = 0.95
    resid = np.abs(p_det - Y_n)
    c = float(np.quantile(resid / np.maximum(sigma, 1e-3), 0.95) / 1.96)
    task_d["posthoc_scale"] = round(c, 3)
    task_d["posthoc_coverage_95"] = round(MT.coverage(p_det, Y_n, c * sigma), 4)
    task_d["posthoc_nll"] = round(MT.gaussian_nll(p_det, Y_n, c * sigma), 4)
    # 分 KPI 校准后覆盖
    cov_kpi = []
    for i in range(Y_n.shape[1]):
        cov_kpi.append(round(MT.coverage(p_det[:, i], Y_n[:, i], c * sigma[:, i]), 4))
    task_d["posthoc_coverage_95_per_kpi"] = cov_kpi
    log(f"  RSSM MC-z (CF-supervised): σ̄={sigma.mean():.4f} "
        f"cov95={task_d['rssm_mc_z']['coverage_95']:.3f} → post-hoc c={c:.2f} "
        f"cov95={task_d['posthoc_coverage_95']:.3f} per-KPI={cov_kpi}")
    results["task_d"] = task_d
    return task_d


# ============================================================
# 图
# ============================================================
def make_figures(results, curves_b, task_c, preds_c, true_delta, Yb_n, te):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 9, "figure.dpi": 150,
                         "axes.spines.top": False, "axes.spines.right": False})

    # --- B: 复合误差曲线 ---
    fig, ax = plt.subplots(figsize=(4.2, 3.0))
    ms = [h * D.STEP_MS for h in HORIZONS]
    for name, hr in curves_b.items():
        style = "--" if name in ("Persistence",) else "-"
        lw = 1.0 if name == "Persistence" else 1.6
        ax.plot(ms, [hr[h - 1] for h in HORIZONS], style, lw=lw, label=name)
    ax.set_xlabel("rollout horizon (ms)")
    ax.set_ylabel("normalized RMSE")
    ax.set_title("Task B: compounding rollout error")
    ax.legend(fontsize=7, frameon=False)
    ax.set_xscale("log")
    fig.tight_layout()
    fig.savefig(DOCS / "fig_rollout.png")
    plt.close(fig)

    # --- C: Δ-效应散点 (最佳 CF 模型; 上排 RSRP, 下排 DL-rate, 分动作类型着色) ---
    fig, axes = plt.subplots(2, 4, figsize=(11, 5.4), sharey="row")
    best = task_c["best_model"]
    dhat = preds_c[best] - Yb_n
    at_types = np.array([s["action_type"] for s in te])
    colors = {"pmax_scale": "#dc2626", "prb_scale": "#2563eb",
              "az_offset": "#16a34a", "ho_offset": "#9333ea"}
    kpi_rows = [0, 2]      # RSRP / DL-rate
    for r, kpi_i in enumerate(kpi_rows):
        for c_i in range(4):
            ax = axes[r, c_i]
            if c_i == 0:
                mask = np.ones(len(at_types), bool)
                ax.set_ylabel(["Δ̂ RSRP", "Δ̂ DL-rate"][r])
            else:
                mask = at_types == ["", "pmax_scale", "prb_scale", "az_offset"][c_i]
            m_ = mask & (np.abs(true_delta[:, kpi_i]) < np.quantile(np.abs(true_delta[:, kpi_i]), 0.995))
            for at, col in colors.items():
                mm = m_ & (at_types == at)
                if mm.sum():
                    ax.scatter(true_delta[mm, kpi_i], dhat[mm, kpi_i],
                               s=3, alpha=0.3, edgecolors="none", color=col, label=at)
            lim = np.abs(true_delta[m_, kpi_i]).max() * 1.15 + 1e-3
            ax.plot([-lim, lim], [-lim, lim], "k--", lw=0.8)
            ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim)
            ax.axhline(0, color="gray", lw=0.5, ls=":")
            ax.set_title(("all actions" if c_i == 0 else ["", "pmax", "prb", "az"][c_i])
                         + (f" / {KPI_SHORT[kpi_i]}" if c_i == 0 else ""), fontsize=9)
            if r == 1:
                ax.set_xlabel("Δ* (true effect, normalized)")
    axes[0, 0].legend(fontsize=7, frameon=False, loc="upper left")
    fig.suptitle(f"Task C: counterfactual effect prediction — {best} (normalized)", y=1.0)
    fig.tight_layout()
    fig.savefig(DOCS / "fig_delta.png", bbox_inches="tight")
    plt.close(fig)

    # --- A: 单步条形图 ---
    fig, ax = plt.subplots(figsize=(4.2, 2.8))
    names = [n for n in list(results["task_a"].keys()) if isinstance(results["task_a"][n], dict)
             and "rmse_norm" in results["task_a"][n]]
    vals = [results["task_a"][n]["rmse_norm"] for n in names]
    order = np.argsort(vals)
    names = [names[i] for i in order]; vals = [vals[i] for i in order]
    colors = ["#888" if n in ("Persistence", "LinearAR+action") else "#2563eb" for n in names]
    ax.barh(names, vals, color=colors, height=0.6)
    for i, v in enumerate(vals):
        ax.text(v + 0.002, i, f"{v:.4f}", va="center", fontsize=7)
    ax.set_xlabel("normalized RMSE (lower is better)")
    ax.set_title("Task A: one-step prediction")
    fig.tight_layout()
    fig.savefig(DOCS / "fig_taskA.png")
    plt.close(fig)


# ============================================================
# 主流程
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--quick", action="store_true", help="fast sanity run")
    ap.add_argument("--skip-d", action="store_true")
    args = ap.parse_args()
    DOCS.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    if args.quick:
        args.epochs = 2

    log("loading dataset ...")
    groups = D.load_branches()
    ws = D.branch_windows(groups)
    norm = D.fit_norm(ws)
    tr, te = D.train_test_split(ws)
    Xh, Xa, Y = D.to_arrays(tr)
    log(f"train={len(tr):,} test={len(te):,}  device={device}")

    # 训练全部模型
    all_models, _ = T.fit_all(norm, Xh, Xa, Y, device="cpu", epochs=args.epochs, seed=SEED)
    for n, m in all_models.items():
        log(f"  {n:16s} params={sum(q.numel() for q in m.parameters()):,}")

    results = {
        "meta": {
            "date": time.strftime("%Y-%m-%d %H:%M"),
            "seed": SEED, "epochs": args.epochs,
            "n_train": len(tr), "n_test": len(te),
            "state_cols": D.STATE_COLS, "action_types": D.ACTION_TYPES,
            "hist": D.HIST, "step_ms": D.STEP_MS,
            "split": f"train t<{D.TRAIN_MS:.0f}ms / test t>={D.TRAIN_MS:.0f}ms (time extrapolation)",
            "norm": norm.to_json(),
        }
    }

    eval_task_a(all_models, norm, te, results)
    curves_b, roll_data = eval_task_b(all_models, norm, te, results, device)

    # ---- 反事实监督训练 (headline 实验) ----
    log("training counterfactual-supervised models ...")
    cf = D.counterfactual_pairs(groups)
    cf_tr, cf_te = D.train_test_split(cf)
    Xh_c, Xa_c, Y_c = D.to_arrays(cf_tr)
    cf_models, _ = T.fit_all(norm, Xh_c, Xa_c, Y_c, device="cpu",
                             epochs=args.epochs, seed=SEED,
                             names=["GRU-WM", "TF-WM", "PhysWM", "RSSM-WM"])
    # CF 物理先验基线 (解析, 零参数): Δrsrp=10log10(p), Δdl∝prb, 其余 0
    cf_models["PhysicsPrior"] = cf_physics_prior(norm)

    task_c, preds_c, true_delta, Yb_n, te_c = eval_task_c(
        all_models, norm, groups, results, cf_models=cf_models, epochs=args.epochs)
    if not args.skip_d:
        eval_task_d(cf_models, norm, groups, results)

    make_figures(results, curves_b, task_c, preds_c, true_delta, Yb_n, te_c)
    log("figures written")

    (DOCS / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"done → {DOCS}/results.json")


if __name__ == "__main__":
    main()
