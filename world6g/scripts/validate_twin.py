#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tiny-Twin 统计保真度验证 (论文级)
====================================
方法 (逐场景 × 逐 KPI):
  1. Kolmogorov-Smirnov 双样本检验    — 分布形状差异 (p>0.05 不可拒绝同分布)
  2. Wasserstein-1 距离              — 分布搬运代价 (KPI 单位, 可解释)
  3. 分位数回归 Q-Q 斜率/截距/R²      — 尾部对齐
  4. 均值/标准差/偏度/峰度 对照表      — 矩对齐
输出:
  world6g/docs/fidelity_report.json   — 机器可读全表
  world6g/docs/fidelity_report.md     — 论文表格 (LaTeX tabular 同步生成)
  world6g/docs/qq_plots.png           — Q-Q 图矩阵 (论文图)
用法: .venv/bin/python world6g/scripts/validate_twin.py
"""
import sys, json
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb

from datalake.config import DB_PATH, GEN_DIR, PKG_DIR
from scipy import stats

DOCS = PKG_DIR / "docs"
KPIS = {
    "RSRP (dBm)": "服务小区RSRP(dBm)",
    "SINR (dB)": "服务小区SINR(dB)",
    "下行速率 (Mbps)": "下行速率(Mbps)",
    "端到端时延 (ms)": "端到端(ms)",
    "CQI": "CQI",
}
SCENE_KEY = {"城区": "城区", "郊区": "郊区", "低空": "低空", "机器人工厂": "机器人工厂"}


def real_by_scene() -> dict[str, pd.DataFrame]:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = con.execute("""
        SELECT u.*, m.描述 AS scene
        FROM ue u JOIN map m
          ON m.episode_id = u.episode_id AND m.字段 = '类型'
    """).fetchdf()
    con.close()
    df["scene_key"] = df["scene"].map(
        lambda s: next((k for k in SCENE_KEY if k in str(s)), "其他"))
    return {sc: g for sc, g in df.groupby("scene_key")}


def twin_by_scene() -> dict[str, pd.DataFrame]:
    rep = json.loads((GEN_DIR / "twin_report.json").read_text())
    ep2scene = {r["gen_episode"]: r["scene"] for r in rep}
    frames = []
    for pq in sorted(GEN_DIR.glob("twin_*_ue.parquet")):
        ep = "_".join(pq.stem.split("_")[:2])
        df = pd.read_parquet(pq)
        df["scene_key"] = ep2scene.get(ep, "?")
        frames.append(df)
    allf = pd.concat(frames, ignore_index=True)
    return {sc: g for sc, g in allf.groupby("scene_key")}


def wasserstein(x, y):
    try:
        return float(stats.wasserstein_distance(x, y))
    except Exception:
        return float("nan")


def qq_fit(x, y, nq=41):
    """分位数对分位数回归: y = a + b*x (理想 a=0, b=1)"""
    qs = np.linspace(0.01, 0.99, nq)
    qx = np.quantile(x, qs)
    qy = np.quantile(y, qs)
    if np.std(qx) < 1e-12:
        return 1.0, 0.0, 0.0
    b, a = np.polyfit(qx, qy, 1)
    pred = a + b * qx
    ss_res = np.sum((qy - pred) ** 2)
    ss_tot = np.sum((qy - qy.mean()) ** 2)
    r2 = 1 - ss_res / max(ss_tot, 1e-12)
    return float(b), float(a), float(r2)


def main():
    DOCS.mkdir(exist_ok=True)
    real, twin = real_by_scene(), twin_by_scene()
    rows, qq_data = [], []

    for scene in sorted(set(real) & set(twin)):
        r_df, t_df = real[scene], twin[scene]
        for kpi_name, col in KPIS.items():
            if col not in r_df.columns or col not in t_df.columns:
                continue
            x = r_df[col].dropna().values.astype(float)   # 真实
            y = t_df[col].dropna().values.astype(float)   # twin
            # CQI/离散量: KS 仍可用; Wasserstein 用同单位
            ks_stat, ks_p = stats.ks_2samp(x, y)
            w1 = wasserstein(x, y)
            b, a, r2 = qq_fit(x, y)
            # 相对 Wasserstein (以真实 IQR 归一, 跨 KPI 可比)
            iqr = float(np.percentile(x, 75) - np.percentile(x, 25))
            w1_rel = w1 / iqr if iqr > 1e-12 else float("nan")
            rows.append({
                "scene": scene, "kpi": kpi_name, "n_real": len(x), "n_twin": len(y),
                "real_mean": float(np.mean(x)), "twin_mean": float(np.mean(y)),
                "real_sd": float(np.std(x)), "twin_sd": float(np.std(y)),
                "real_skew": float(stats.skew(x)), "twin_skew": float(stats.skew(y)),
                "real_kurt": float(stats.kurtosis(x)), "twin_kurt": float(stats.kurtosis(y)),
                "ks_stat": float(ks_stat), "ks_p": float(ks_p),
                "w1": w1, "w1_rel_IQR": w1_rel,
                "qq_slope": b, "qq_intercept": a, "qq_r2": r2,
            })
            qq_data.append((scene, kpi_name, x, y))

    df = pd.DataFrame(rows)
    # ---- JSON ----
    (DOCS / "fidelity_report.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1))
    # ---- Markdown + LaTeX ----
    md = ["# Tiny-Twin 统计保真度验证\n",
          "方法: 双样本 KS 检验 / Wasserstein-1 / Q-Q 分位数回归 (理想 slope=1, R²=1)\n",
          "结论口径: KS p>0.05 = 与真实分布不可区分; W1/IQR<0.25 = 搬运代价低于四分位距 1/4\n"]
    md.append("| 场景 | KPI | 真实均值±SD | Twin均值±SD | KS统计 | KS p | W1 (单位) | W1/IQR | QQ斜率 | QQ R² |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for _, r in df.iterrows():
        md.append(f"| {r.scene} | {r.kpi} | {r.real_mean:.1f}±{r.real_sd:.1f} | "
                  f"{r.twin_mean:.1f}±{r.twin_sd:.1f} | {r.ks_stat:.3f} | "
                  f"{r.ks_p:.3f} | {r.w1:.2f} | {r.w1_rel_IQR:.3f} | "
                  f"{r.qq_slope:.2f} | {r.qq_r2:.3f} |")
    (DOCS / "fidelity_report.md").write_text("\n".join(md) + "\n")

    # LaTeX 表 (论文用)
    tex = ["\\begin{table}[t]", "\\centering",
           "\\caption{Tiny-Twin fidelity vs.\\ real-network simulation, per scene and KPI. "
           "KS = two-sample Kolmogorov--Smirnov statistic ($p$-value); "
           "$W_1$ = Wasserstein-1 distance in KPI units; "
           "$W_1/\\mathrm{IQR}$ = normalized by real-data interquartile range.}",
           "\\label{tab:fidelity}",
           "\\small",
           "\\begin{tabular}{llcccccc}", "\\toprule",
           "Scene & KPI & Real (mean$\\pm$sd) & Twin (mean$\\pm$sd) & KS ($p$) & $W_1$ & $W_1/$IQR & QQ $R^2$ \\\\", "\\midrule"]
    for _, r in df.iterrows():
        p_str = f"{r.ks_p:.2f}" if r.ks_p >= 0.005 else "<0.01"
        tex.append(f"{' & '.join([r.scene, r.kpi.replace(' (', '~('), f"{r.real_mean:.1f}$\\pm${r.real_sd:.1f}",
                    f"{r.twin_mean:.1f}$\\pm${r.twin_sd:.1f}",
                    f"{r.ks_stat:.2f} ({p_str})", f"{r.w1:.2f}", f"{r.w1_rel_IQR:.2f}",
                    f"{r.qq_r2:.2f}"])} \\\\")
    tex += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    (DOCS / "fidelity_table.tex").write_text("\n".join(tex) + "\n")

    # ---- Q-Q 图矩阵 ----
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    scenes = sorted({d[0] for d in qq_data})
    kpis = sorted({d[1] for d in qq_data})
    fig, axes = plt.subplots(len(scenes), len(kpis), figsize=(3.2 * len(kpis), 2.8 * len(scenes)))
    axes = np.atleast_2d(axes)
    for i, sc in enumerate(scenes):
        for j, kp in enumerate(kpis):
            ax = axes[i][j]
            hit = next((d for d in qq_data if d[0] == sc and d[1] == kp), None)
            if hit is None:
                ax.axis("off"); continue
            _, _, x, y = hit
            lo = min(x.min(), y.min()); hi = max(x.max(), y.max())
            # 分位数对齐 (长度可不同): q 个分位点对 q 个分位点
            qs = np.linspace(0.005, 0.995, 200)
            qx = np.quantile(x, qs); qy = np.quantile(y, qs)
            ax.scatter(qx, qy, s=5, alpha=0.5, color="#38bdf8")
            ax.plot([lo, hi], [lo, hi], "k--", lw=1, label="y=x")
            ax.set_title(f"{sc} · {kp}", fontsize=9)
            ax.tick_params(labelsize=7)
    fig.suptitle("Q–Q: Tiny-Twin (y) vs. real simulation (x)", fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(DOCS / "qq_plots.png", dpi=180)
    print(f"[docs] fidelity_report.json / .md / fidelity_table.tex / qq_plots.png")
    print(df[["scene", "kpi", "ks_stat", "ks_p", "w1_rel_IQR", "qq_r2"]].round(3).to_string())


if __name__ == "__main__":
    main()
