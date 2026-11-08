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
