"""浏览器扩展（MV3）静态验收测试：manifest 契约、资产齐全、UI 要素、无油猴依赖。"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EXT = PROJECT_ROOT / "extension" / "pxb7-extension"
# 注意：扩展 match pattern 不支持端口，回环权限必须用不带端口的写法（匹配任意端口）
LOOPBACK_HOSTS = {"http://127.0.0.1/*", "http://localhost/*"}


def _manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_v3_contract() -> None:
    m = _manifest()
    assert m["manifest_version"] == 3, "必须是 MV3 独立扩展"
    assert m["name"] and m["version"] == "0.4.0"
    assert m["background"]["service_worker"] == "background.js"
    assert m["action"]["default_popup"] == "popup.html"
    assert set(m["permissions"]) <= {"storage", "tabs", "clipboardWrite", "alarms"}, "权限最小化"
    assert "alarms" in m["permissions"], "自更新周期检查需要 alarms"
    for entry in m["host_permissions"]:
        assert entry in LOOPBACK_HOSTS, f"host_permissions 只允许本机回环网关：{entry}"
    assert "<all_urls>" not in json.dumps(m)
    matches = m["content_scripts"][0]["matches"]
    assert matches == ["https://www.pxb7.com/buy/*", "https://www.pxb7.com/product/*"]
    assert m["content_scripts"][0]["js"] == ["sweep.js", "content.js"]


def test_extension_assets_exist() -> None:
    for name in ("background.js", "content.js", "sweep.js", "popup.html", "popup.js", "popup.css"):
        assert (EXT / name).is_file(), f"缺资产：{name}"


def test_icons_are_valid_png_with_declared_sizes() -> None:
    sizes = {"icon16.png": 16, "icon32.png": 32, "icon48.png": 48, "icon128.png": 128}
    for name, expected in sizes.items():
        path = EXT / "icons" / name
        assert path.is_file(), f"缺图标：{name}"
        raw = path.read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG"
        width, height = struct.unpack(">II", raw[16:24])
        assert (width, height) == (expected, expected), f"{name} 尺寸应为 {expected}²"
        assert len(raw) > 200, f"{name} 内容异常（疑似空图）"


def test_no_userscript_dependency() -> None:
    """扩展必须完全独立：不得出现 GM_* / 油猴 API 依赖。"""
    for name in ("content.js", "background.js", "popup.js"):
        text = (EXT / name).read_text(encoding="utf-8")
        assert "GM_" not in text, f"{name} 残留油猴 API"
        assert "unsafeWindow" not in text


def test_collector_and_gateway_wiring() -> None:
    content = (EXT / "content.js").read_text(encoding="utf-8")
    assert "document.documentElement.outerHTML" in content, "就地读取已渲染 DOM"
    assert '"/ingest/detail"' in content and '"/ingest/cards"' in content
    assert "collect-now" in content and 'type: "ingest"' in content
    assert "targetBlocked" in content and "TARGETS" in content, \
        "列表页必须按采集目标预检（未勾选游戏不发请求）"
    assert "采集目标" in content, "跳过时必须给出可操作提示"
    assert "expandCards" in content and "cards_target" in content, \
        "每次采集张数：页内加载更多（sweep）必须接线"
    background = (EXT / "background.js").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8765" in background, "网关地址必须为回环"
    assert "msg.path" in background, "背景服务按内容脚本给定的路径代理"
    assert "INGEST_PATHS" in background and "unsupported-path" in background, \
        "代理路径必须经固定白名单（防御性收紧）"
    assert "URLSearchParams" in background, "查询参数必须编码构造"
    assert "SQL" in background or "无 SQL" in background, "需注明无 SQL 的安全边界"
    for marker in ('"/config', '"/stats"', '"/shutdown"', "collect-active", "onInstalled",
                   "check-update", "apply-update", "chrome.runtime.reload",
                   "chrome.alarms", "onStartup"):
        assert marker in background, f"背景服务缺少要素：{marker}"
    popup = (EXT / "popup.js").read_text(encoding="utf-8")
    for marker in ('"快照"', "renderTiles", "renderBars", "renderBatches", "/stats", "/config",
                   "/shutdown", "renderTargets", "renderPaths", "/open-folder",
                   "saveTargets", "check-update", "upd-ext", "c-cards", "cards_target"):
        assert marker in popup, f"弹窗缺少要素：{marker}"


def test_popup_html_references_assets_and_sections() -> None:
    html = (EXT / "popup.html").read_text(encoding="utf-8")
    assert 'href="popup.css"' in html and 'src="popup.js"' in html
    for element_id in ("tiles", "rounds", "batches", "targets", "paths",
                       "c-auto", "c-interval", "c-cards", "save", "grab", "upd-ext",
                       "full", "stop"):
        assert f'id="{element_id}"' in html, f"弹窗缺少元素：{element_id}"
