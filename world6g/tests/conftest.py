# -*- coding: utf-8 -*-
"""pytest 路径引导: 与 uvicorn 冷启动等价 (仓库根 cwd).

uvicorn 以 `python -m uvicorn world6g.backend.api:app` 从仓库根启动时,
world6g/ 目录因包结构自动在 sys.path; 而 pytest world6g/tests/ 直接执行时
sys.path[0] 为 tests/, 需手动把仓库根与 world6g/ 加入, 才能让
`import world6g.backend.api` 与 api.py 内部的
`importlib.import_module("twm_serve")` / `("twin.engine")` / `("scripts.train_planner")`
按 uvicorn 相同顺序解析。
"""
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1]      # world6g/
REPO_DIR = PKG_DIR.parent                          # world-model/

for p in (str(REPO_DIR), str(PKG_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
