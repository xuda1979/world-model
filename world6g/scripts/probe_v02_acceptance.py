#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V0.2 验收证据探针 (可重复运行)
================================
方案一 §7 表4 (11月13日 V0.2) 验收方式 — 机器实测证据:
  1. 固定环境连续演示三轮无阻断 — demo_script_v02.md 全演示序列连续回放 × 3 轮,
     断言: 全部调用期望状态码、无 5xx/超时、守卫 422 语义、导出 CSV 结构完好。
  2. 缓存交互 P95 ≤ 2 秒 — api.py 无显式缓存层 (DuckDB 进程内 + OS 页缓存即热路径),
     口径: 稳态轮 (第 2/3 轮) 常规交互调用 P95; 冷轮 (第 1 轮) 单列。
  3. 长任务显示进度 — 工作台进度标记 ('推理中…'/按钮禁用/tick 播放控件) +
     /api/wm/verify timing_ms (predict/execute) 服务端时长披露。

输出: world6g/datalake/generated/v02_acceptance_evidence.json (验收报告权威证据源)
用法: .venv/bin/python world6g/scripts/probe_v02_acceptance.py [--base http://127.0.0.1:8811]
退出码: 0 = PASS (3/3 轮 + P95 达标 + 守卫/导出/进度标记全过), 1 = FAIL
"""
import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "world6g" / "datalake" / "generated" / "v02_acceptance_evidence.json"
P95_BUDGET_MS = 2000.0  # 方案一表4: 缓存交互 P95 不高于 2 秒

# ---- 单轮演示序列 (对齐 demo_script_v02.md 六个演示动作) ----
# (标签, 方法, 路径, body, 期望状态码, 校验函数名)
VALID_COORD = {"scene": "城区", "sinr_c": [8.2, 5.1, -3.4], "n_ue_c": [12, 9, 5]}
INVALID_COORD = {"scene": "城区", "sinr_c": [999.0, 5.1, -3.4], "n_ue_c": [12, 9, 5]}
WM_VERIFY = {"episode_id": "twin_01", "ue": "0",
             "action": {"type": "baseline", "value": 0}, "steps": 80}

ROUND_SEQ = [
    ("episodes", "GET", "/api/episodes", None, 200, "json_nonempty"),
    ("scene3d", "GET", "/api/scene3d/twin_01", None, 200, "scene3d_ok"),
    ("coord_action", "POST", "/api/coord/action", VALID_COORD, 200, "coord_ok"),
    ("coord_info", "GET", "/api/coord/info", None, 200, "coord_info_ok"),
    ("coord_ticks", "GET", "/api/coord/ticks?scene=campus&seed=9000&ep_i=0", None, 200, "ticks_ok"),
    ("wm_verify", "POST", "/api/wm/verify", WM_VERIFY, 200, "wm_verify_ok"),
    ("chat", "POST", "/api/chat", {"q": "城区平均RSRP"}, 200, "chat_ok"),
    ("guard_422", "POST", "/api/coord/action", INVALID_COORD, 422, "guard_422_ok"),
    ("export_csv", "GET", "/api/coord/ticks/export?scene=campus&seed=9000&ep_i=0", None, 200, "export_ok"),
]
GUARD_LABELS = {"guard_422"}  # 非常规交互 (错误路径), 不计入缓存交互 P95


def http_call(base, method, path, body, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    return status, (time.perf_counter() - t0) * 1000.0, raw


# ---- 响应校验 (与 demo_script_v02.md 验收点一一对应) ----
def _check(kind, status, raw):
    if kind == "json_nonempty":
        return len(json.loads(raw)) > 0
    if kind == "scene3d_ok":
        d = json.loads(raw)
        return "traces" in d and len(d["traces"]) > 0
    if kind == "coord_ok":
        d = json.loads(raw)
        return "action" in d and "top_k" in d and len(d["top_k"]) == 5
    if kind == "coord_info_ok":
        d = json.loads(raw)
        # /api/coord/info 实际字段: available/model/deploy_rule/params/action_space/loso
        return (d.get("available") is True and "loso" in d
                and "action_space" in d and "params" in d)
    if kind == "ticks_ok":
        d = json.loads(raw)
        return d.get("n_ticks") == 10 and len(d.get("rows", [])) == 10 \
            and "episode_reward" in d
    if kind == "wm_verify_ok":
        d = json.loads(raw)
        tm = d.get("timing_ms", {})
        kpis = d.get("kpis", [])
        # KPI 元素字段 (api.py /api/wm/verify): name/unit/rmse_base/rmse_act/rmse_persist/delta_*
        return (isinstance(kpis, list) and len(kpis) > 0
                and all("rmse_base" in k and "delta_err" in k for k in kpis)
                and "predict" in tm and "execute" in tm)
    if kind == "chat_ok":
        d = json.loads(raw)
        return "RSRP" in d.get("reply", "")
    if kind == "guard_422_ok":
        return b"sinr_c" in raw  # 量程守卫语义 (方案二 §7 "非法输入护栏")
    if kind == "export_ok":
        text = raw.decode("utf-8")
        lines = text.strip().splitlines()
        # 表头 (scene,seed,ep_i,dec_idx,t_ms,...,reward,...) + 10 tick 行, 与 ticks 同切片同源
        return (len(lines) == 11 and lines[0].startswith("scene,")
                and "t_ms" in lines[0] and "reward" in lines[0])
    raise ValueError(kind)


def run_round(base, r_idx):
    calls, failures = [], []
    for label, method, path, body, expect, check in ROUND_SEQ:
        try:
            status, ms, raw = http_call(base, method, path, body)
            ok = (status == expect) and _check(check, status, raw)
        except Exception as e:  # 超时/连接错误 = 阻断
            status, ms, ok = -1, -1.0, False
            failures.append({"label": label, "error": str(e)})
        if not ok and status != -1:
            failures.append({"label": label,
                             "got_status": status, "expect": expect})
        calls.append({"label": label, "status": status,
                      "ms": round(ms, 1), "ok": ok})
    return {"round": r_idx, "pass": not failures,
            "n_calls": len(calls), "calls": calls, "failures": failures}


def p95(samples):
    if not samples:
        return None
    s = sorted(samples)
    import math
    k = max(0, math.ceil(0.95 * len(s)) - 1)
    return round(s[k], 1)


def check_progress_markers(base):
    """长任务显示进度: 工作台进度标记 (live HTML 源内断言)."""
    try:
        status, _, raw = http_call(base, "GET", "/workbench", None)
        html = raw.decode("utf-8")
        if status != 200:
            return {"ok": False, "reason": f"/workbench -> {status}"}
        markers = {
            "coord_inference_progress": "推理中…" in html,   # 协同策略推理中状态
            "button_disabled_wait": "cursor: wait" in html,  # 长任务按钮禁用
            "tick_play_controls": "▶ 播放" in html,          # tick 回放播放/暂停/逐tick
            "tick_step_progress": "tick-bar" in html,        # tick 逐步进度条
        }
        # 服务端长任务时长披露 (wm/verify timing_ms) 已在 run_round wm_verify_ok 校验
        return {"ok": all(markers.values()), "markers": markers}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8811")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          cwd=ROOT, capture_output=True, text=True).stdout.strip()
    rounds = [run_round(args.base, i + 1) for i in range(args.rounds)]
    progress = check_progress_markers(args.base)

    # ---- 时延统计: 冷轮 (1) vs 稳态轮 (2..N) ----
    def lat_ms(rd, guard=False):
        return [c["ms"] for c in rd["calls"]
                if c["ok"] and (guard is None or (c["label"] in GUARD_LABELS) == guard)]

    warm = rounds[1:]
    warm_norm = [ms for rd in warm for ms in lat_ms(rd, guard=False)]
    cold_norm = lat_ms(rounds[0], guard=False)
    def _mean_or_none(xs):
        return round(mean(xs), 1) if xs else None

    per_endpoint = {}
    for label, *_ in ROUND_SEQ:
        warm_xs = [c["ms"] for rd in warm for c in rd["calls"]
                   if c["label"] == label and c["ok"]]
        per_endpoint[label] = {
            "cold_ms_p95": p95([c["ms"] for c in rounds[0]["calls"] if c["label"] == label and c["ok"]]),
            "warm_ms_mean": _mean_or_none(warm_xs),
            "warm_ms_max": max(warm_xs) if warm_xs else None,
        }
    warm_p95 = p95(warm_norm)
    cold_p95 = p95(cold_norm)

    rounds_pass = sum(1 for r in rounds if r["pass"])
    verdict = (rounds_pass == len(rounds)
               and warm_p95 is not None and warm_p95 <= P95_BUDGET_MS
               and progress["ok"])

    evidence = {
        "probe": "probe_v02_acceptance.py",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": head,
        "acceptance_ref": "方案一 §7 表4 (11月13日 V0.2): 固定环境连续演示三轮无阻断; "
                          "缓存交互 P95 ≤ 2 秒; 长任务显示进度",
        "base_url": args.base,
        "cache_implementation": "api.py 无显式缓存层 — DuckDB 进程内查询 + OS 页缓存即热路径; "
                                "P95 口径 = 稳态轮 (第 2..N 轮) 常规交互调用 (不含 422 守卫错误路径)",
        "rounds_total": len(rounds),
        "rounds_pass": rounds_pass,
        "rounds_no_block": all(r["pass"] for r in rounds),
        "latency": {
            "cold_round_p95_ms": cold_p95,
            "warm_p95_ms": warm_p95,
            "p95_budget_ms": P95_BUDGET_MS,
            "p95_pass": warm_p95 is not None and warm_p95 <= P95_BUDGET_MS,
            "n_warm_samples": len(warm_norm),
            "per_endpoint": per_endpoint,
        },
        "guards": {
            "coord_422_range": all(
                any(c["label"] == "guard_422" and c["ok"] for c in r["calls"]) for r in rounds),
            "export_csv_rows": 11,
        },
        "long_task_progress": progress,
        "rounds_detail": rounds,
        "verdict": "PASS" if verdict else "FAIL",
    }
    OUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")

    print(f"[v02-acceptance] rounds {rounds_pass}/{len(rounds)} 无阻断 | "
          f"稳态 P95 {warm_p95} ms (预算 {P95_BUDGET_MS:.0f}) | "
          f"冷轮 P95 {cold_p95} ms | 长任务进度标记 {'OK' if progress['ok'] else 'MISS'}")
    for r in rounds:
        fails = r["failures"]
        print(f"  round {r['round']}: {'PASS' if r['pass'] else 'FAIL ' + json.dumps(fails, ensure_ascii=False)}")
    print(f"[v02-acceptance] verdict = {evidence['verdict']}  → {OUT.relative_to(ROOT)}")
    raise SystemExit(0 if verdict else 1)


if __name__ == "__main__":
    main()
