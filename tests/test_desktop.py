"""桌面前端组件测试（离线：不启动服务、不打开窗口；只验证回环边界与命令构造）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import desktop as D  # noqa: E402


def test_loopback_guard_rejects_non_loopback() -> None:
    """【安全回归】SSRF 边界：只允许本机回环目标，其余一律拒绝。"""
    with pytest.raises(D.LoopbackOnlyError):
        D.assert_loopback("192.168.1.1", 8765)
    with pytest.raises(D.LoopbackOnlyError):
        D.assert_loopback("example.com", 8765)
    with pytest.raises(D.LoopbackOnlyError):
        D.assert_loopback("169.254.169.254", 80)
    with pytest.raises(D.LoopbackOnlyError):
        D.assert_loopback("127.0.0.1", 70000)
    D.assert_loopback("127.0.0.1", 8765)          # 本机回环放行
    D.assert_loopback("localhost", 8765)

    with pytest.raises(D.LoopbackOnlyError):
        D.gateway_alive("example.com", 8765)      # 探活同样过守卫
    with pytest.raises(D.LoopbackOnlyError):
        D.stop_gateway("10.0.0.1", 8765)


def test_find_browser_env_override_and_candidates(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "mybrowser.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("PXB7_BROWSER", str(fake))
    assert D.find_browser(candidates=()) == str(fake), "环境变量覆盖优先"

    monkeypatch.delenv("PXB7_BROWSER")
    real = tmp_path / "edge.exe"
    real.write_bytes(b"MZ")
    assert D.find_browser(candidates=(str(real),)) == str(real)
    assert D.find_browser(candidates=(str(tmp_path / "missing.exe"),)) is None


def test_gateway_alive_false_on_closed_port() -> None:
    assert D.gateway_alive("127.0.0.1", 59999, timeout=0.3) is False


def test_stop_gateway_when_not_running_is_noop() -> None:
    assert D.stop_gateway("127.0.0.1", 59998, timeout=0.3) == {
        "ok": True, "already_stopped": True}


def test_shortcut_command_shape(tmp_path: Path) -> None:
    ps = D.build_shortcut_ps(tmp_path, name="pxb7采集看板",
                             python_exe=str(tmp_path / "python.exe"))
    assert "CreateShortcut" in ps and str(tmp_path) in ps
    assert "dashboard" in ps and "pxb7采集看板.lnk" in ps
    assert "Desktop" in ps


def test_bats_and_shortcut_flow_assets_exist() -> None:
    for name in ("采集看板.bat", "停止采集.bat", "创建桌面快捷方式.bat"):
        bat = PROJECT_ROOT / name
        assert bat.is_file(), f"缺少双击入口：{name}"
        text = bat.read_text(encoding="utf-8", errors="replace")
        assert "run.py" in text and ".venv" in text
