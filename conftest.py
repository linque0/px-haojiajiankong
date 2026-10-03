"""pytest 根配置：保证 `import pxb7` 可用（不依赖 CWD），并固定测试期环境。

- 即使 pytest 未启用 ``pythonpath`` 选项，这里也会把项目根加入 sys.path；
- 测试不访问网络与真实站点；webhook 环境变量在测试期被清空，避免误发通知。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 测试期禁用外部推送（凭据只从环境变量读取，这里显式清空以免误发）
for _env in ("PXB7_NOTIFY_WEBHOOK", "PXB7_NOTIFY_BUSINESS", "PXB7_NOTIFY_OPS",
             "PXB7_DB_PATH", "PXB7_RAW_ROOT"):
    os.environ.pop(_env, None)
