"""全文请求间隔 0–10 秒（0.1 秒步进）契约测试（2026-10-04 用户指令）。

旧口径 min=2/max=15/step=1 + 网关 2000–15000ms 整数钳制导致"调不动"；新口径：
弹窗/看板输入框 0–10 步进 0.1，网关接受 0–10000ms（100ms 粒度取整），
title-source.js/titles.js 客户端钳制同步（0 是显式设置不得当缺省吞掉）。
静态契约断言沿用 test_extension.py 的验收风格；网关数值行为见 test_gateway.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXT = PROJECT_ROOT / "extension" / "pxb7-extension"
DASHBOARD = PROJECT_ROOT / "extension" / "dashboard.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_popup_input_allows_0_to_10_step_0_1() -> None:
    html = _read(EXT / "popup.html")
    assert 'id="c-title-interval" min="0" max="10" step="0.1"' in html


def test_dashboard_input_allows_0_to_10_step_0_1() -> None:
    html = _read(DASHBOARD)
    assert 'id="c-title-interval" min="0" max="10" step="0.1"' in html


def test_title_source_interval_clamp_0_to_10000() -> None:
    js = _read(EXT / "title-source.js")
    assert "Math.max(0, Math.min(10000, n))" in js, "intervalFor 必须钳制 0–10000ms"
    assert "Math.max(2000" not in js, "旧的 2 秒下限必须移除（0 是显式设置）"


def test_titles_stats_interval_clamp_0_to_10000() -> None:
    js = _read(EXT / "titles.js")
    assert "Math.max(0, Math.min(10000, n))" in js, "stats.interval_ms 必须同口径钳制"
    assert "Math.max(2000" not in js, "旧的 2 秒下限必须移除"
    # 整页结束后补采一轮的暂停保留 6 秒下限（间隔归 0 时的兜底护栏，docs/10 §4.3）
    assert "Math.max(6000" in js


def test_detail_interval_separate_from_list() -> None:
    """详情采集间隔独立于列表间隔（2026-10-05 用户指令）：双输入框 + 互斥启停 + 独立配置键。"""
    popup_html = _read(EXT / "popup.html")
    dashboard = _read(DASHBOARD)
    popup_js = _read(EXT / "popup.js")
    assert 'id="c-detail-interval" min="0" max="10" step="0.1"' in popup_html
    assert 'id="c-detail-interval" min="0" max="10" step="0.1"' in dashboard
    assert 'detail_interval_ms: Number($("c-detail-interval").value) * 1000' in popup_js
    assert 'detail_interval_ms: Number($("c-detail-interval").value) * 1000' in dashboard
    # 弹窗按模式互斥启停：详情模式启用详情间隔、停用列表间隔
    assert '$("c-title-interval").disabled = detail;' in popup_js
    assert '$("c-detail-interval").disabled = !detail;' in popup_js
    # 详情队列独立取值：collection.js 的钳制与网关配置键
    queue_js = _read(EXT / "collection.js")
    assert "clampInterval" in queue_js and "Math.min(10000" in queue_js
    assert "config.detail_interval_ms" in queue_js, "队列应从网关配置读取详情间隔"
    py = PROJECT_ROOT / "pxb7" / "gateway.py"
    assert '"detail_interval_ms": (float, (0, 10000))' in _read(py)
