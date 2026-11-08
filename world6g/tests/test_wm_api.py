# -*- coding: utf-8 -*-
"""
WM 服务栈回归套件 — 锁定 2026-11-06 会话 6 修复的 bug 类 + 服务/闭环协议裂缝.

背景: 会话 6 发现前端 "策略推荐" 把字符串 "auto" 提交 /api/wm/rollout,
backend 只按 dict 处理 → 500 "'str' object has no attribute 'get'"。
修复: rollout/verify 均增加 `isinstance(action, str) and action == "auto"`
分支 → 动作模型一步决策, 返回 auto_action + policy 版本。
本套件把该 bug 类固化为回归护栏: 任何重写若回归, 立即红灯。

路径前提 (与 uvicorn 冷启动等价): 从仓库根运行
    .venv/bin/python -m pytest world6g/tests/ -q

模型/数据依赖: models/{gru_wm_ms,policy_fused_net}.pt,
datalake/generated/{twin_*_ue,twin_action_dataset}.parquet, datalake/lake.duckdb。
任一缺失 → 相关用例 skip (不假绿)。
"""
import json
import numpy as np
import pytest
import torch
from pathlib import Path

from fastapi.testclient import TestClient
from world6g.backend.api import app, _TWIN_EP
import twm_serve as wm

H = (wm._META or {}).get("hist", 8)
client = TestClient(app)

ACTION_TYPES = ("none", "az_offset", "pmax_scale", "prb_scale", "ho_offset")


@pytest.fixture(scope="module")
def tw_eps():
    """(episode_id, 首个UE, 行数) 列表, 仅 twin (verify 需要确定性重放)"""
    out = []
    for ep in sorted(_TWIN_EP):
        df = wm.pd.read_parquet(wm.GEN_DIR / f"{ep}_ue.parquet")
        out.append((ep, int(df.UEID.iloc[0]), len(df)))
    return out


@pytest.fixture(scope="module")
def policy_ok():
    return wm.load_policy() is not None


@pytest.fixture(scope="module")
def wm_ok():
    return wm.load_model() is not None


# ---------- 1. 会话 6 bug 类: action:"auto" (字符串) → 200 + auto_action ----------

def test_rollout_auto_no_500(policy_ok, wm_ok, tw_eps):
    if not (policy_ok and wm_ok):
        pytest.skip("模型权重缺失")
    ep, _, _ = tw_eps[0]
    r = client.post("/api/wm/rollout", json={"episode_id": ep, "action": "auto"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["auto_action"] is not None, "auto 模式必须返回 auto_action (旧 bug 返回 500)"
    assert d["auto_action"]["type"] in ACTION_TYPES
    assert d["policy"] is not None and d["policy"]["available"]
    assert "PolicyNet-Fused" in d["policy"]["version"], d["policy"]["version"]
    # 语义: 推荐动作 → action 与 auto_action 类型一致; 不推荐(none) → 回退 baseline
    if d["auto_action"]["recommend"]:
        assert d["action"]["type"] == d["auto_action"]["type"]
    else:
        assert d["action"]["type"] == "baseline"
    # rollout 轨形状完整
    assert len(d["baseline"]) == d["steps"] == len(d["acted"])


def test_rollout_dict_action_no_auto_branch(policy_ok, wm_ok, tw_eps):
    """dict 动作 (含 baseline / az_offset) 不得触发 auto 分支 (回归护栏)"""
    if not (policy_ok and wm_ok):
        pytest.skip("模型权重缺失")
    for action in ({"type": "baseline", "value": 0},
                   {"type": "az_offset", "value": 30.0}):
        r = client.post("/api/wm/rollout",
                        json={"episode_id": tw_eps[0][0], "action": action})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["auto_action"] is None, "dict 动作不得触发 auto 分支"
        assert d["policy"] is None
        assert len(d["baseline"]) == d["steps"] == len(d["acted"])


# ---------- 2. 验证闭环: auto 与 dict 双路径 ----------
P = {"action": "auto", "dict_base": {"type": "baseline", "value": 0},
     "dict_az": {"type": "az_offset", "value": 30.0}}

@pytest.mark.parametrize("action,expect_auto",
                         [("auto", True),
                          (P["dict_base"], False),
                          (P["dict_az"], False)])
def test_verify_auto_and_dict(policy_ok, wm_ok, tw_eps, action, expect_auto):
    if not (policy_ok and wm_ok):
        pytest.skip("模型权重缺失")
    ep, ue, n = tw_eps[0]
    steps = min(8, n - H - 1)
    if steps < 1:
        pytest.skip("episode 过短")
    r = client.post("/api/wm/verify", json={
        "episode_id": ep, "ue": ue, "action": action, "steps": steps,
        "window_t": H - 1})
    assert r.status_code == 200, r.text
    d = r.json()
    if expect_auto:
        assert d["auto_action"] is not None
        assert d["policy"] is not None and "PolicyNet-Fused" in d["policy"]["version"]
    else:
        assert d["auto_action"] is None
        assert d["policy"] is None
    assert d["scene"] == _TWIN_EP[ep][0]
    assert len(d["kpis"]) == 4
    kp0 = d["kpis"][0]
    assert {"rmse_base", "rmse_act", "delta_pred", "delta_truth"} <= set(kp0)
    # 真值 Δ 应可读: 引擎双分支重放产出动作因果效应
    assert all("delta_truth" in k for k in d["kpis"])


def test_verify_non_twin_episode_400(policy_ok, wm_ok):
    r = client.post("/api/wm/verify", json={"episode_id": "real_1",
                                            "action": "auto"})
    assert r.status_code == 400, "verify 仅支持 twin episodes (确定性重放)"


# ---------- 3. 决策端点 (服务化协议) ----------

def test_wm_action_endpoint(policy_ok, tw_eps):
    if not policy_ok:
        pytest.skip("策略权重缺失")
    ep, ue, n = tw_eps[0]
    t_w = min(H + 5, n - 1)
    r = client.post("/api/wm/action",
                    json={"episode_id": ep, "ue": ue, "window_t": t_w})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["action"]["type"] in (d["policy"]["int_types"] + ["none"])
    assert d["recommend"] in (True, False)
    assert d["policy"]["available"] and "PolicyNet-Fused" in d["policy"]["version"]
    # value 语义: none → 0.0; 推荐 → 有限数值
    if d["action"]["type"] == "none":
        assert d["action"]["value"] == 0.0
    else:
        assert np.isfinite(d["action"]["value"])


# ---------- 4. 协议裂缝: 服务端 policy_action == 已提交闭环动作 ----------

def test_policy_action_parity_with_committed_closedloop(policy_ok):
    """锁死服务/闭环协议裂缝: on 同一 hist 窗, twm_serve.policy_action 与已提交
    closedloop_fused.json 的 Policy-Fused 动作 (type,value) 逐位一致.
    回归若发生 (如权重替换/归一化失配/同构破损), 此处立即红灯."""
    if not policy_ok:
        pytest.skip("策略权重缺失")
    committed = Path(wm.PKG_DIR) / "docs" / "closedloop" / "closedloop_fused.json"
    if not committed.exists():
        pytest.skip("无已提交闭环结果")
    cl = json.loads(committed.read_text(encoding="utf-8"))

    # 重建同根基准分支 (与 eval_closedloop 同源: twin_action_dataset → load_branches)
    from wm.data import load_branches
    ds = wm.pd.read_parquet(wm.GEN_DIR / "twin_action_dataset.parquet")
    by_key = {(g.scene, str(g.ue)): g for g in load_branches(ds)}

    rows = [w for w in cl["windows"] if w["strategy"] == "Policy-Fused"]
    assert len(rows) > 0, "闭环结果缺少 Policy-Fused 行"
    checked = mismatches = 0
    for w in rows:
        grp = by_key.get((w["scene"], str(w["ue"])))
        if grp is None:
            continue
        t = int(w["t_idx"])
        hist_s = grp.baseline.states[t - H + 1: t + 1]
        got = wm.policy_action(hist_s)          # 服务端路径 (与动作服务同构)
        checked += 1
        if got["type"] != w["action"] or abs(float(got["value"] or 0.0) - w["value"]) > 1e-3:
            mismatches += 1
            if mismatches <= 3:
                print(f"  PARITY MISMATCH {w['scene']}/UE{w['ue']}@{w['t_ms']:.0f}ms "
                      f"committed=({w['action']},{w['value']}) got=({got['type']},{got['value']})")
    assert checked == len(rows), f"仅核验 {checked}/{len(rows)} 窗 (by_key 未全覆盖)"
    assert mismatches == 0, f"协议裂缝: {mismatches}/{checked} 窗动作不一致"

    # 动作率一致性 (已知先验: none 22 / az 74)
    from collections import Counter
    cnt = Counter(w["action"] for w in rows)
    assert set(cnt) <= {"none", "az_offset"}, f"意外动作类型 {dict(cnt)}"


# ---------- 5. 空参缺陷类回归 (会话 170 修复: 空 episode_id 曾 500) ----------
# 背景: /api/wm/{action,rollout,verify} 未传 episode_id 时, "" 传入
# _ep_ue_df → int("") → ValueError → 500。修复: 显式 422 + 中文提示。
# 三端点 + verify 空 body 双路径均锁定。

def test_wm_endpoints_empty_ep_id_422_not_500():
    for url in ("/api/wm/action", "/api/wm/rollout", "/api/wm/verify"):
        r = client.post(url, json={})
        assert r.status_code == 422, \
            f"{url} 空 episode_id 应 422 而非 {r.status_code}: {r.text[:120]}"
        assert "episode_id" in r.json().get("detail", ""), url


def test_wm_verify_empty_action_key_only_422():
    # 有 episode_id 但其它字段全缺 → 仍不应 500 (默认 action/baseline, steps 合法)
    r = client.post("/api/wm/verify", json={"episode_id": "twin_01"})
    assert r.status_code in (200, 400, 422), f"意外状态码 {r.status_code}: {r.text[:120]}"
    assert r.status_code != 500


# ---------- 6. 前端-后端契约核验 (会话 171: 零死链固化) ----------
# index.html/workbench.html 调用的全部 API 路径必须存在于 backend 路由表,
# 且以真实 id/载荷打通 200。防前端演进引入死链 (或后端删路由未同步前端)。

_FE_CALLS = [
    ("GET",  "/api/episodes",        None),
    ("GET",  "/api/kpi/1",           None),
    ("GET",  "/api/map/1",           None),
    ("GET",  "/api/capabilities",    None),
    ("GET",  "/api/twin/report",     None),
    ("GET",  "/api/campus",          None),
    ("GET",  "/api/scene3d/1",       None),
    ("POST", "/api/wm/rollout",      {"episode_id": "1", "action": {"type": "az_offset", "value": 5.0}, "steps": 6}),
    ("POST", "/api/wm/verify",       {"episode_id": "twin_01", "action": "auto", "steps": 4, "window_t": 60}),
    ("POST", "/api/chat",            {"message": "你好"}),
]


def test_frontend_backend_contract_zero_dead_link():
    """前端 11 条调用 (10 唯一路径) 全部命中后端路由并 200 — 零死链。
    前端 HTML 若新增 fetch 端点, 此测试不会自动感知, 但任何后端删路由/
    改协议导致前端主链路断, 此处立即红灯。"""
    for method, url, body in _FE_CALLS:
        r = client.request(method, url, json=body) if body is not None else client.request(method, url)
        assert r.status_code == 200, \
            f"死链或协议断裂: {method} {url} → {r.status_code}: {r.text[:120]}"


# ---------- 7. 权重-架构-元数据三方一致性 (会话 172: 防漂移固化) ----------
# .pt state_dict 键集必须 ≡ 活体网络键集; .pt 与 *_meta.json 必须成对在位;
# 推理必须确定性。权重替换/架构改动/元数据丢失任一发生, 此处立即红灯。

_MD = Path(wm.PKG_DIR) / "models"

def test_weight_arch_metadata_three_way_parity():
    m = wm.load_model()
    if m is None:
        pytest.skip("WM 权重缺失")
    mkeys = set(m.state_dict().keys())

    for pt in ("gru_wm_ms.pt", "gru_wm.pt"):
        sd = torch.load(_MD / pt, map_location="cpu")
        assert set(sd.keys()) == mkeys, \
            f"{pt} 键集与活体 GRU-WM 不一致 (权重-架构漂移)"

    p = wm.load_policy()
    if p is not None:
        sd = torch.load(_MD / "policy_fused_net.pt", map_location="cpu")
        assert set(sd.keys()) == set(p.state_dict().keys()), \
            "policy_fused_net.pt 键集与活体 PolicyNet 不一致"

    pairs = [("gru_wm.pt", "gru_wm_meta.json"), ("gru_wm_ms.pt", "gru_wm_ms_meta.json"),
             ("policy_fused_net.pt", "policy_fused_meta.json"), ("policy_net.pt", "policy_meta.json"),
             ("policy_cal_net.pt", "policy_cal_meta.json"), ("policy_uncal_net.pt", "policy_uncal_meta.json"),
             ("wm_cal.pt", "wm_cal_meta.json"), ("wm_uncal.pt", "wm_uncal_meta.json")]
    for pt, mt in pairs:
        assert (_MD / pt).exists() and (_MD / mt).exists(), f"{pt} 或 {mt} 缺失 (权重-元数据不成对)"

    # 推理确定性: 同输入两次 → 逐位一致
    hist = np.tile(np.array([[-110.0, 15.0, 75.0, 13.5]]), (H, 1))
    r1 = wm.policy_action(hist)
    r2 = wm.policy_action(hist)
    assert r1["type"] == r2["type"]
    assert abs(float(r1["value"] or 0) - float(r2["value"] or 0)) < 1e-12


# ---------- 8. 数据湖-生成物完整性 (会话 173: 资产在位与设计对称性固化) ----------
# twin 01–08 × 6 表 = 48 parquet 全在位; cal/uncal 数据集规模比符合设计
# (cal 5 场景含 campus, uncal 4 场景有意排除 — sim_calib.py:150);
# 湖核心表非空; 报告锚点 json 在位。

def test_datalake_generated_assets_integrity():
    GEN = Path(wm.PKG_DIR) / "datalake" / "generated"
    eps = [f"twin_{i:02d}" for i in range(1, 9)]
    tbls = ["ue", "link", "phy", "cellperf", "mro", "sys"]
    missing = [f"{e}_{t}.parquet" for e in eps for t in tbls
               if not (GEN / f"{e}_{t}.parquet").exists()]
    assert not missing, f"twin parquet 缺失: {missing}"

    # cal/uncal 规模比 = 设计 (cal 含 campus 5 场景, uncal 4 场景)
    cal = wm.pd.read_parquet(GEN / "twin_cal_action_dataset.parquet")
    unc = wm.pd.read_parquet(GEN / "twin_uncal_action_dataset.parquet")
    assert len(cal) == len(unc) + unc.groupby("branch_scene").size().iloc[0] * 0 + \
        20800, f"cal 规模异常: {len(cal)} (期望 uncal {len(unc)} + campus 20800)"
    assert "campus" in set(cal["branch_scene"])
    assert "campus" not in set(unc["branch_scene"])

    # 主 action dataset: 4 场景 5 动作类型
    ds = wm.pd.read_parquet(GEN / "twin_action_dataset.parquet")
    assert ds["branch_scene"].nunique() == 4
    assert len(ds["action_type"].unique()) >= 5  # baseline+4 干预

    # 报告锚点
    for f in ("twin_report.json", "sim_calib_report.json", "action_report.json"):
        assert (GEN / f).exists(), f"{f} 缺失"
