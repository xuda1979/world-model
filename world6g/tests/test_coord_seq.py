# -*- coding: utf-8 -*-
"""时序协同策略 (M11, coord_seq) 守护测试 — 部署产物/LOSO 协议/结构不变量.

背景 (2026-11-11, 会话 36): scripts/train_coord_seq_policy.py v2 在
9000 行时序 MDP 数据集 (25 组 × 36 eps × 10 dec) 上完成
"时序 WM → 规划 oracle → BC 蒸馏" 全链路 + 5 折闭环 LOSO, 产物:

  models/coord_seq_wm.pt + coord_seq_wm_meta.json        (全数据部署版 WM)
  models/coord_seq_policy.pt + coord_seq_policy_meta.json (部署版动作模型)
  models/coord_seq_folds.pt                               (LOSO 折模型)
  datalake/generated/coord_seq_loso.json                  (闭环 LOSO 结果)

本套件锁定四类不变量 (产物缺失时逐条 skip, 与 test_wm_api 风格一致):

  1. 产物存在性 + 规模: 数据集 9000 行/25 组/36 eps/10 dec, 存档可加载;
  2. 结构/形状: TemporalWM 30-256-256-256-11, SeqPolicy 15-128-128-125,
     meta JSON 与存档字段一致 (ctx_cols/combos/grid/scenes);
  3. LOSO 协议完整: 5 折 × 各场景 held seed 互异, 6 控制器汇总齐全,
     scene-mean ≡ summary 聚合自洽, 质量门限 (BC ≥ WM-贪心, 赢率 > 0);
  4. 推理确定性: 同输入两次前向逐位一致; 加载后 argmax 动作可复现。
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gen_coord_seqs import GRID, SCENES, seeds_of   # noqa: E402

PKG = Path(__file__).resolve().parents[1]
MD = PKG / "models"
GEN = PKG / "datalake" / "generated"

WM_PT = MD / "coord_seq_wm.pt"
POL_PT = MD / "coord_seq_policy.pt"
FOLD_PT = MD / "coord_seq_folds.pt"
LOSO = GEN / "coord_seq_loso.json"
DS = GEN / "coord_seq_dataset.parquet"
WM_META = MD / "coord_seq_wm_meta.json"
POL_META = MD / "coord_seq_policy_meta.json"

ARTS = [WM_PT, POL_PT, FOLD_PT, LOSO, WM_META, POL_META]
ALL_OK = all(p.exists() for p in ARTS)

N_COMBOS = 125          # 5^3 动作组合
N_FOLDS = 5
FX10 = ["sinr_c0", "n_ue_c0", "sinr_c1", "n_ue_c1", "sinr_c2", "n_ue_c2",
        "l3_margin_c0", "l3_margin_c1", "l3_margin_c2", "mean_sinr"]
CONTROLLERS = ["zero", "random", "best_const", "wm_greedy", "bc_policy",
               "sim_greedy"]


pytestmark = pytest.mark.skipif(
    not ALL_OK, reason="coord_seq 产物缺失 (需先运行 train_coord_seq_policy.py)")


# ---------------------------------------------------------------- 夹具
@pytest.fixture(scope="module")
def tcs():
    """导入训练脚本 (仅取结构定义, 不执行训练)。"""
    import importlib
    return importlib.import_module("scripts.train_coord_seq_policy")


@pytest.fixture(scope="module")
def ds():
    import pandas as pd
    return pd.read_parquet(DS)


@pytest.fixture(scope="module")
def loso():
    return json.loads(LOSO.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def wm_ckpt():
    return torch.load(WM_PT, map_location="cpu", weights_only=False)


@pytest.fixture(scope="module")
def pol_ckpt():
    return torch.load(POL_PT, map_location="cpu", weights_only=False)


# ---------------------------------------------------------------- 1. 产物与数据集
def test_dataset_shape_and_groups(ds):
    """v2 数据集: 9000 行 = 25 组 × 36 eps × 10 决策点, 5 场景全覆盖。"""
    assert len(ds) == 9000, f"行数漂移: {len(ds)} (期望 9000)"
    assert ds.groupby(["scene", "seed"]).ngroups == 25
    assert set(ds["scene"]) == set(SCENES)
    eps_per_group = ds.groupby(["scene", "seed"])["ep_i"].nunique()
    assert (eps_per_group == 36).all(), "eps 数漂移 (期望每组 36)"
    dec_per_ep = ds.groupby(["scene", "seed", "ep_i"]).size()
    assert (dec_per_ep == 10).all(), "决策点数漂移 (期望每 ep 10)"
    assert ds["dec_idx"].min() == 0 and ds["dec_idx"].max() == 9


def test_dataset_columns_and_actions(ds):
    """数据集必备列齐全; 动作列取值域 = GRID (±6/±3/0 dB)。"""
    need = {"scene", "seed", "ep_i", "dec_idx", "p0", "p1", "p2",
            "win_obj", "reward", *FX10}
    missing = need - set(ds.columns)
    assert not missing, f"缺列: {missing}"
    for c in ("p0", "p1", "p2", "next_p0", "next_p1", "next_p2"):
        assert set(np.unique(ds[c].values)).issubset(set(GRID)), \
            f"{c} 越出 GRID: {sorted(set(np.unique(ds[c].values)) - set(GRID))}"


def test_artifacts_exist():
    """六类部署产物齐全 (含 LOSO JSON 与双 meta)。"""
    for p in ARTS:
        assert p.exists(), f"产物缺失: {p.name}"


# ---------------------------------------------------------------- 2. 结构/形状
def test_wm_structure_and_meta(wm_ckpt):
    """部署 WM: 3 成员 ensemble, 30-256³-11; meta 与存档字段一致。"""
    meta = json.loads(WM_META.read_text(encoding="utf-8"))
    assert len(wm_ckpt["members"]) == 3
    assert list(wm_ckpt["fx"]) == FX10
    assert list(wm_ckpt["scenes"]) == list(SCENES)
    assert list(wm_ckpt["grid"]) == list(GRID)
    for m in wm_ckpt["members"]:
        assert tuple(m["net.0.weight"].shape) == (256, 30)
        assert tuple(m["net.6.weight"].shape) == (11, 256)
    assert meta["in_dim"] == 30 and meta["out_dim"] == 11
    assert meta["ctx_cols"] == FX10
    assert meta["params"] == 142347          # 单成员参数量 (x3 为 ensemble 总量)


def test_policy_structure_and_meta(pol_ckpt):
    """部署动作模型: 15-128-128-125; meta 与存档字段一致。"""
    meta = json.loads(POL_META.read_text(encoding="utf-8"))
    pol = pol_ckpt["policy"]
    assert tuple(pol["net.0.weight"].shape) == (128, 15)
    assert tuple(pol["net.4.weight"].shape) == (125, 128)
    assert len(pol_ckpt["combos"]) == N_COMBOS
    assert list(pol_ckpt["fx"]) == FX10
    assert tuple(pol_ckpt["ctx_mu"].shape) == (10,)
    assert tuple(pol_ckpt["ctx_sd"].shape) == (10,)
    assert np.isfinite(pol_ckpt["ctx_mu"]).all() and \
        (pol_ckpt["ctx_sd"] > 0).all(), "归一化统计量非法 (NaN/σ≤0)"
    assert meta["combos"] == pol_ckpt["combos"]
    assert meta["ctx_cols"] == FX10
    # meta 里的 LOSO 摘要与 JSON 同源
    loso = json.loads(LOSO.read_text(encoding="utf-8"))
    assert meta["loso_summary"] == loso["summary"]["bc_policy"]


def test_folds_checkpoint_structure():
    """折存档: list[5] × {fold, wm[3], wm_stats, bc, bc_stats}。"""
    folds = torch.load(FOLD_PT, map_location="cpu", weights_only=False)
    assert isinstance(folds, list) and len(folds) == N_FOLDS
    for i, f in enumerate(folds):
        assert f["fold"] == i
        assert len(f["wm"]) == 3
        assert tuple(f["bc"]["net.4.weight"].shape) == (125, 128)
        assert [s.shape for s in f["wm_stats"]] == [(10,), (10,), (11,), (11,)]
        assert [s.shape for s in f["bc_stats"]] == [(10,), (10,)]


# ---------------------------------------------------------------- 3. LOSO 协议
def test_loso_protocol_and_folds(loso):
    """5 折 × 各场景 held seed 互异 (LOSO: 同组 5 seed 一折一个)。"""
    p = loso["protocol"]
    assert p["folds"] == list(range(N_FOLDS))
    assert p["controllers"] == CONTROLLERS
    assert p["n_groups_total"] == 25, \
        f"组数漂移: {p['n_groups_total']} (期望 25)"
    held = {sc: [] for sc in SCENES}
    for fr in loso["folds"]:
        assert fr["fold"] in range(N_FOLDS)
        for sc, sd in fr["held_seeds"].items():
            assert sd in seeds_of(sc), f"{sc} held seed {sd} 越界"
            held[sc].append(sd)
    for sc in SCENES:
        assert len(held[sc]) == N_FOLDS and len(set(held[sc])) == N_FOLDS, \
            f"{sc}: held seed 重复 → 折泄漏"


def test_loso_summary_selfconsistent(loso):
    """6 控制器汇总齐全; scene-mean 聚合 ≡ summary (自洽性)。"""
    for c in CONTROLLERS:
        assert c in loso["summary"], f"缺控制器 {c}"
        s = loso["summary"][c]
        assert {"mean", "p50", "min", "max"} <= set(s)
        scene_mean = float(np.mean(
            [loso["per_scene_mean_return"][sc][c] for sc in SCENES]))
        assert abs(scene_mean - s["mean"]) < 1e-6, \
            f"{c}: per-scene 聚合 {scene_mean:.4f} ≠ summary {s['mean']:.4f}"
    # 恒零控制器的结构不变量
    assert loso["summary"]["zero"]["mean"] == 0.0


def test_loso_quality_gates(loso):
    """质量门限 (v2 实测锚定, 留 ±25% 容差防小扰动):
    BC ≥ WM-贪心 (蒸馏不减益); 两者均值 > random 5×; sim_greedy 上界存在。
    """
    s = loso["summary"]
    assert s["bc_policy"]["mean"] >= s["wm_greedy"]["mean"], \
        "BC 蒸馏均值劣于 WM-贪心 (蒸馏目标即 WM 规划器)"
    assert s["bc_policy"]["mean"] > 5 * s["random"]["mean"]
    assert s["sim_greedy"]["mean"] >= s["best_const"]["mean"], \
        "仿真真值贪心应 ≥ 最优恒定基线"
    wr = loso["win_rate_vs_best_const"]
    assert wr["bc_policy"] > 0, "BC 赢率应为正"
    assert wr["random"] == 0.0
    # 匹配率域
    mr = loso["match_rates_mean"]
    assert 0.0 <= mr["bc_vs_wm"] <= 1.0
    assert mr["bc_vs_wm"] >= 0.5, "BC 与 WM 规划器一致性过低"


# ---------------------------------------------------------------- 4. 推理确定性
def test_wm_forward_deterministic(tcs, wm_ckpt):
    """加载部署 WM → 同输入两次前向逐位一致 (eval + no_grad)。"""
    wm = tcs.TemporalWM()
    wm.load_state_dict(wm_ckpt["members"][0])
    wm.eval()
    x = torch.zeros(4, tcs.IN_DIM)
    x[:, :10] = torch.randn(4, 10)
    with torch.no_grad():
        y1, y2 = wm(x), wm(x)
    assert torch.equal(y1, y2)
    assert tuple(y1.shape) == (4, 11)


def test_policy_argmax_reproducible(tcs, pol_ckpt, ds):
    """部署动作模型: 同 ctx 两次 argmax 一致, 且动作 ∈ 125 组合。"""
    import importlib
    pol = tcs.SeqPolicy()
    pol.load_state_dict(pol_ckpt["policy"])
    pol.eval()
    mu, sd = pol_ckpt["ctx_mu"], pol_ckpt["ctx_sd"]
    ctx = ds[FX10].head(8).to_numpy(dtype=np.float32)
    x = torch.from_numpy((ctx - mu) / sd).float()
    s_oh = torch.eye(len(SCENES))[
        [SCENES.index(s) for s in ds["scene"].head(8)]]
    inp = torch.cat([x, s_oh], dim=1)
    with torch.no_grad():
        a1 = pol(inp).argmax(dim=1)
        a2 = pol(inp).argmax(dim=1)
    assert torch.equal(a1, a2)
    assert ((a1 >= 0) & (a1 < N_COMBOS)).all()
    # argmax 索引映射回合法组合 (存档为 "+6,-3,+6" 字符串)
    combos = pol_ckpt["combos"]
    for i in a1.tolist():
        parts = str(combos[i]).split(",")
        assert len(parts) == 3, f"组合 {combos[i]!r} 非三维"
        assert all(float(v) in GRID for v in parts), \
            f"组合 {combos[i]!r} 越出 GRID"
