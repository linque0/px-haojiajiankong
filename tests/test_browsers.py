"""Chromium 系浏览器发现与扩展更新指引测试（只读检测，不启动任何进程）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import browsers  # noqa: E402
from pxb7 import cli  # noqa: E402


def test_identify_known_and_unknown(tmp_path: Path) -> None:
    cases = (("chrome.exe", "chrome", "chrome://extensions/"),
             ("MSEDGE.EXE", "edge", "edge://extensions/"),
             ("quark.exe", "quark", "quark://extensions/"),
             ("QQBrowser.exe", "qq", "qqbrowser://extensions/"))
    for name, key, url in cases:
        browser = browsers.identify(tmp_path / name)
        assert browser is not None, name
        assert (browser.key, browser.extensions_url) == (key, url)
    assert browsers.identify(tmp_path / "unknown-browser.exe") is None, "未知 exe 不猜"


def test_discover_dedup_and_order(tmp_path: Path) -> None:
    for name in ("chrome.exe", "quark.exe"):
        (tmp_path / name).write_bytes(b"stub")
    found = browsers.discover_browsers(
        candidates=[str(tmp_path / "quark.exe"), str(tmp_path / "chrome.exe"),
                    str(tmp_path / "chrome.exe"), str(tmp_path / "nope.exe")],
        registry=False)
    assert [b.key for b in found] == ["chrome", "quark"], "去重且按优先级排序"


def test_standard_location_flag(tmp_path: Path) -> None:
    standard = browsers.Browser(
        key="chrome", name="Google Chrome",
        exe=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        extensions_url="chrome://extensions/")
    assert standard.standard_location is True
    custom = browsers.identify(tmp_path / "quark.exe")
    assert custom is not None and custom.standard_location is False


def test_guide_lines_cover_quark_and_self_update() -> None:
    quark = browsers.identify(Path(r"F:\夸克\Quark\quark.exe"))
    assert quark is not None
    update = "\n".join(browsers.guide_lines("update", [quark]))
    for marker in ("quark://extensions/", "开发者模式", "夸克实验室", "自更新",
                   "剪贴板", "重新加载"):
        assert marker in update, f"更新指引缺少：{marker}"
    assert "命令行" in update, "必须说明内部页不能靠命令行打开（实测结论）"
    install = "\n".join(browsers.guide_lines("install", [quark]))
    assert "加载已解压的扩展程序" in install and "manifest.json" in install


def test_guide_lines_empty() -> None:
    lines = browsers.guide_lines("update", [])
    assert lines and "未检测到" in lines[0]


def test_cli_browser_extension_json(monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture,
                                     tmp_path: Path) -> None:
    (tmp_path / "quark.exe").write_bytes(b"stub")
    fake = browsers.Browser(key="quark", name="夸克浏览器", exe=tmp_path / "quark.exe",
                            extensions_url="quark://extensions/")
    monkeypatch.setattr(browsers, "discover_browsers", lambda **kwargs: [fake])
    code = cli.main(["browser-extension", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["browsers"][0]["extensions_url"] == "quark://extensions/"
    assert payload["extension_dir"].endswith("pxb7-extension")

    monkeypatch.setattr(browsers, "discover_browsers", lambda **kwargs: [])
    assert cli.main(["browser-extension"]) == 1, "未检测到浏览器 → 退出码 1"


def test_bats_reference_browser_extension_and_clipboard() -> None:
    """两个 bat 必须：调用 browser-extension；用剪贴板引导（内部页不能靠命令行打开）。"""
    for name in ("更新浏览器扩展.bat", "安装浏览器扩展.bat"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8", errors="replace")
        assert "browser-extension" in text, name
        assert "chrome://extensions/" in text and "quark://extensions/" in text, name
        assert "clip" in text, f"{name} 需把扩展页地址复制到剪贴板"
        assert "start \"\" " in text, name
