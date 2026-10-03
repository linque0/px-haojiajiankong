"""项目入口：把项目根加入 sys.path 后调用 pxb7.cli.main()。

用法（在工作区根执行）：
    pxb7-price-monitor/.venv/Scripts/python.exe pxb7-price-monitor/run.py init-db
    pxb7-price-monitor/.venv/Scripts/python.exe pxb7-price-monitor/run.py --help

说明：加入 sys.path 的是本文件所在目录（项目根），因此无论 CWD 在哪都能 import pxb7；
配置里的内部路径由 pxb7/config.py 以项目根为基准解析，CLI 显式路径参数按 CWD 解析。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7.cli import main  # noqa: E402  （必须在 sys.path 调整之后导入）

if __name__ == "__main__":
    sys.exit(main())
