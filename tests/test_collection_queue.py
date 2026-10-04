"""执行扩展实际任务队列，覆盖 MV3 重启、跨页关联、停止和张数边界。"""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('script', ['collection_queue.cjs', 'content_modes.cjs'])
def test_extension_collection_queue(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    result = subprocess.run([node, str(Path(__file__).with_name(script))],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
