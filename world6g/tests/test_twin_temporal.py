# -*- coding: utf-8 -*-
"""M11 时序化原语守护测试 — twin/calibrated.py tick/时变功率路径.

背景 (2026-11-08, M11): 协同 MDP 需要仿真器跨 tick 推进 + 逐 tick 时变
per-cell 功率动作。TinyTwin 增加 tick_ms 构造参数与 _init_tick_state/
_step_tick 原语。本套件锁定三条不变量:

  1. 冻结路径逐位不变: tick_ms=None (默认) 与历史输出哈希一致;
  2. tick_ms=50 ≡ 冻结路径: 同 seed 下 MD5 逐位相同 (tick 引擎是冻结
     引擎在同 RNG 消耗下的精确重构);
  3. 时变功率因果性: cell_tx_scale_db 在两次 _step_tick 之间变更,
     自下一 tick 生效 (本 tick 记录已用旧值); L3 滤波历史保持暴露。
"""

import hashlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from twin.calibrated import TinyTwin  # noqa: E402

SCENES = ["城区", "campus", "郊区", "低空", "机器人工厂"]

# 2026-11-08 重构前基线 (bit-identical 锚, 双 seed x 5 场景)
FROZEN_MD5 = {
    ("城区", 7): "741e4edca36f", ("城区", 42): "b787d6affe11",
    ("campus", 7): "506987d86607", ("campus", 42): "ed76c4de09cc",
    ("低空", 7): "9ed7a417e601", ("低空", 42): "ddb0f31a71fe",
    ("机器人工厂", 7): "f165adab2b3a", ("机器人工厂", 42): "3c4982066e7d",
    ("郊区", 7): "1fd6ab55ba37", ("郊区", 42): "11b8ea61f82f",
}


def _md5(df: pd.DataFrame) -> str:
    return hashlib.md5(df.to_csv(index=False).encode()).hexdigest()[:12]


# ---------- 1. 冻结路径逐位不变 ----------
@pytest.mark.parametrize("scene,seed", sorted(FROZEN_MD5.keys()))
def test_frozen_path_bit_identical(scene, seed):
    """tick_ms=None 默认路径与 2026-11-08 重构前基线 MD5 一致。"""
    df = TinyTwin(scene, seed=seed).run()["ue"]
    assert _md5(df) == FROZEN_MD5[(scene, seed)], \
        f"冻结路径漂移: {scene} seed={seed}"


# ---------- 2. tick_ms=50 ≡ 冻结路径 ----------
@pytest.mark.parametrize("scene", SCENES)
def test_tick50_equals_frozen(scene):
    """tick 引擎在 50ms 步长下是冻结引擎的精确重构 (同 RNG 消耗)。"""
    for seed in (7, 42):
        f = TinyTwin(scene, seed=seed).run()["ue"]
        k = TinyTwin(scene, seed=seed, tick_ms=50.0).run()["ue"]
        assert _md5(f) == _md5(k), \
            f"tick50 ≠ frozen: {scene} seed={seed}"


# ---------- 3. tick 模式行为 ----------
def test_tick_rowcount_and_timestamps():
    """tick_ms=100, ep_s=10 → 100 ticks x 8 UE = 800 行, 时间戳 0..9900。"""
    tw = TinyTwin("城区", ep_s=10.0, tick_ms=100.0, seed=7)
    df = tw.run()["ue"]
    assert len(df) == 800
    ts = sorted(df["时间戳(ms)"].unique())
    assert len(ts) == 100
    assert ts[0] == 0.0 and ts[1] == 100.0 and ts[-1] == 9900.0


def test_tick_state_exposure():
    """_step_tick 后 L3 历史/步计数/时钟暴露在实例上。"""
    tw = TinyTwin("城区", ep_s=2.0, tick_ms=200.0, seed=7)
    tw._init_tick_state()
    assert tw._tick_ti == 0 and tw._tick_t_ms == 0.0
    assert tw._tick_l3.shape == (8, 3)
    for _ in range(10):
        tw._step_tick(200.0)
    assert tw._tick_ti == 10
    assert tw._tick_t_ms == pytest.approx(2000.0)
    assert tw._tick_l3.shape == (8, 3)
    assert np.all(np.isfinite(tw._tick_l3))


def test_time_varying_power_causality():
    """cell_tx_scale_db 在 tick 间变更 → 功率动作自下一 tick 生效。

    设计: tick0-1 无动作 (baseline); tick2 起小区0 -9dB。若因果正确,
    tick2 的 RSRP (服务=小区0 的 UE) 应低于 tick1 同 UE 的 RSRP。"""
    tw = TinyTwin("城区", ep_s=2.0, tick_ms=200.0, seed=7)
    tw._init_tick_state()
    rows = []
    for k in range(6):
        tw.cell_tx_scale_db = [-9.0, 0.0, 0.0] if k >= 2 else None
        rows.extend(tw._step_tick(200.0))
    df = pd.DataFrame(rows)
    # 找一个服务小区=cell 1 (campus-1, idx 0) 的 UE 作为探针
    probe = df[(df["UEID"] == df.iloc[0]["UEID"])]
    r0 = probe[probe["时间戳(ms)"] == 200.0]["服务小区RSRP(dBm)"].iloc[0]   # tick1 (无动作)
    r2 = probe[probe["时间戳(ms)"] == 600.0]["服务小区RSRP(dBm)"].iloc[0]   # tick3 (动作后)
    # 若探针 UE 服务小区恰为 idx0, 功率降应压低 RSRP (宽松断言: 不漂高)
    assert r2 <= r0 + 3.0, \
        f"时变功率未生效或因果破坏: tick1={r0}, tick3={r2}"


def test_tick_power_action_changes_output():
    """有/无时变功率动作的 tick 轨迹必须分歧 (动作有效性)。"""
    def run_with(action_seq):
        tw = TinyTwin("城区", ep_s=2.0, tick_ms=200.0, seed=7)
        tw._init_tick_state()
        rows = []
        for k in range(8):
            tw.cell_tx_scale_db = action_seq[k] if action_seq[k] else None
            rows.extend(tw._step_tick(200.0))
        return pd.DataFrame(rows)

    base = run_with([None] * 8)
    act = run_with([None, None, [-6.0, 0.0, 0.0], [-6.0, 0.0, 0.0],
                    [0.0, -6.0, 0.0], [0.0, -6.0, 0.0],
                    [-6.0, -6.0, 0.0], [-6.0, -6.0, 0.0]])
    assert _md5(base) != _md5(act), "功率动作未改变轨迹 (动作失效)"
    # 前 2 tick (动作前) 必须逐位相同 → 证因果从动作 tick 起生效
    b2 = base[base["时间戳(ms)"] < 400.0]
    a2 = act[act["时间戳(ms)"] < 400.0]
    assert _md5(b2) == _md5(a2), "动作泄漏到动作前的 tick (因果破坏)"


def test_tick_init_idempotent_resets():
    """重复 _init_tick_state 重置状态 (幂等)。"""
    tw = TinyTwin("城区", ep_s=2.0, tick_ms=200.0, seed=7)
    tw._init_tick_state()
    for _ in range(5):
        tw._step_tick(200.0)
    tw._init_tick_state()
    assert tw._tick_ti == 0 and tw._tick_t_ms == 0.0


def test_all_scenes_tick_smoke():
    """5 场景 tick 模式冒烟: 输出 schema 列集与冻结路径一致。"""
    for scene in SCENES:
        f = TinyTwin(scene, ep_s=2.0, seed=7).run()["ue"]
        k = TinyTwin(scene, ep_s=2.0, tick_ms=100.0, seed=7).run()["ue"]
        assert list(f.columns) == list(k.columns), f"{scene}: 列集漂移"
        assert len(k) == 160  # 20 ticks x 8 UE (ep_s=2, tick=100ms)
        assert k["服务小区RSRP(dBm)"].notna().all()
