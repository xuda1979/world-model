"""数据底座路径与常量配置 (单一事实来源)"""
from pathlib import Path

# ---------- 仓库根 ----------
DATALAKE_DIR = Path(__file__).resolve().parent     # world6g/datalake/
PKG_DIR      = DATALAKE_DIR.parent                 # world6g/
REPO_DIR     = PKG_DIR.parent                      # world-model/

# ---------- 源数据: 6G_data_simulation 真实仿真数据 ----------
SIM_ROOT   = Path("/Users/daxu/software/6G_data_simulation")
EPISODES   = SIM_ROOT / "6G_episode_data" / "6G_episodes_10s_fullcatalog"
STATS_JSON = SIM_ROOT / "6G_episode_data" / "6G_episodes_10scenarios_fullfields" / "_tools" / "stats.json"
DICT_CSV   = SIM_ROOT / "6G_episode_data" / "6G_sample_package" / "数据字典_字段说明与示例.csv"

# ---------- 数据湖分层 ----------
LAKE      = PKG_DIR / "datalake"
RAW_DIR   = LAKE / "raw"          # 源数据注册表 (不复制大文件, 记录指纹)
CURATED   = LAKE / "curated"      # 10 episodes 规整后的 Parquet (AI 就绪)
GEN_DIR   = LAKE / "generated"    # Tiny-Twin 生成数据 (与 curated 同 schema)
DB_PATH   = LAKE / "lake.duckdb"

# ---------- 数据表清单 (episode 内目录名 -> 表名) ----------
# 表名与中文数据字典的工作表对齐
EP_TABLES = {
    "ue":         ("终端性能表",     "终端性能表/终端性能表.csv"),
    "cellperf":   ("小区性能表",     "小区性能表/小区性能表.csv"),
    "phy":        ("物理层指标",     "物理层指标/物理层指标.csv"),
    "link":       ("链路级指标",     "链路级指标/链路级指标.csv"),
    "sys":        ("系统级指标",     "系统级指标/系统级指标.csv"),
    "mro":        ("测量报告_MRO",   "测量报告/测量报告_MRO.csv"),
    "action":     ("小区配置_动作族", "小区配置/小区配置_动作族.csv"),
    "site":       ("小区工参_网格6",  "小区工参/小区工参_网格6.csv"),
    "map":        ("场景地图",       "地图/场景_地图.csv"),
    "nrm":        ("NRM-CM",        "小区配置/小区配置_完整配置_NRM-CM.csv"),
    "csi":        ("信道快照_CSI",   "终端性能表/信道快照_CSI.csv"),   # 说明文件, 实际矩阵在 csi/ 目录
}

# ---------- 物理常量 (与 gen_10ep_fullcatalog.py 一致) ----------
SLOT_MS        = 0.5
EP_S           = 10.0
TOTAL_SLOTS    = 20000
UE_STEP_MS     = 50      # 终端级采样 50ms
CELL_STEP_MS   = 100     # 小区级采样 100ms
NOISE_FLOOR_DBM = -125.2  # per-RE 噪声 (15kHz, -174dBm/Hz + NF 7dB)
TX_POWER_DBM   = 33.7     # SSB 发射功率 (真实工参)

# ---------- 真实数据锚定统计 (96 文件 338 万 MRO 样本) ----------
# 由 stats.json 提取, tiny twin 校准目标
def load_real_anchors():
    import json
    with open(STATS_JSON) as f:
        return json.load(f)

if __name__ == "__main__":
    print("EPISODES exists:", EPISODES.exists())
    print("STATS_JSON    :", STATS_JSON.exists())
    print("DICT_CSV      :", DICT_CSV.exists())
    print("tables        :", len(EP_TABLES))
