"""扩展一键启停网关：Native Messaging 宿主 + 注册安装器测试（2026-10-04 用户指令）。

覆盖：扩展 ID 推导格式与确定性、宿主清单契约（allowed_origins 精确登记本扩展、不开放
通配）、宿主命令白名单与启停行为（monkeypatch desktop，不真启进程）、弹窗静态契约
（按钮/宿主名/权限/版本一致性）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from pxb7 import native_host as NH  # noqa: E402

EXT = PROJECT_ROOT / "extension" / "pxb7-extension"
HOST_SCRIPT = PROJECT_ROOT / "extension" / "native-host" / "pxb7_gateway_host.py"


def _load_host_module():
    spec = importlib.util.spec_from_file_location("pxb7_gateway_host", HOST_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 安装器（纯逻辑部分；注册表真写不进测试）
# --------------------------------------------------------------------------- #
def test_extension_id_format_and_determinism() -> None:
    first = NH.extension_id_for_path()
    again = NH.extension_id_for_path()
    assert first == again and len(first) == 32
    assert all("a" <= c <= "p" for c in first), "Chromium 扩展 ID 只含 a–p"
    other_dir = PROJECT_ROOT / "extension" / "native-host"
    assert NH.extension_id_for_path(other_dir) != first, "不同路径必须推出不同 ID"


def test_normalize_ext_id_rejects_bad_override() -> None:
    with pytest.raises(NH.NativeHostError):
        NH.normalize_ext_id("zzz-not-an-id")            # 非 a–p/长度不对
    with pytest.raises(NH.NativeHostError):
        NH.normalize_ext_id("  ")
    auto = NH.normalize_ext_id(None)
    assert len(auto) == 32


def test_build_host_manifest_contract(tmp_path: Path) -> None:
    launcher = tmp_path / NH.HOST_LAUNCHER
    launcher.write_bytes(b"@echo off\r\n")
    manifest = NH.build_host_manifest("a" * 32, ext_dir=tmp_path)
    assert manifest["name"] == NH.HOST_NAME == "com.pxb7.gateway"
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == [f"chrome-extension://{'a' * 32}/"], \
        "allowed_origins 必须精确登记本扩展，不开放通配"
    assert Path(manifest["path"]).is_absolute() and Path(manifest["path"]) == launcher
    with pytest.raises(NH.NativeHostError):
        NH.build_host_manifest("a" * 32, ext_dir=tmp_path / "nope")   # 启动器缺失即报错


# --------------------------------------------------------------------------- #
# 宿主 handle()：命令白名单 + 启停行为（monkeypatch desktop，不真启进程）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def host_module():
    return _load_host_module()


def test_host_rejects_unknown_and_malformed(host_module) -> None:
    assert host_module.handle({"cmd": "rm -rf"})["ok"] is False
    assert host_module.handle({"cmd": "start; shutdown"})["ok"] is False
    assert host_module.handle("start")["ok"] is False          # 非 dict
    assert host_module.handle({})["ok"] is False               # 缺 cmd
    assert host_module.handle(None)["ok"] is False


def test_host_status_and_start_already_running(host_module, monkeypatch) -> None:
    monkeypatch.setattr(host_module.desktop, "gateway_alive", lambda *a, **k: True)
    monkeypatch.setattr(host_module.desktop, "spawn_gateway",
                        lambda *a, **k: pytest.fail("已在运行时不得重复拉起"))
    assert host_module.handle({"cmd": "status"}) == {"ok": True, "running": True}
    reply = host_module.handle({"cmd": "start"})
    assert reply["ok"] is True and reply["running"] is True and reply["started"] is False


def test_host_start_spawns_and_waits(host_module, monkeypatch) -> None:
    alive = {"n": False}
    monkeypatch.setattr(host_module.desktop, "gateway_alive",
                        lambda *a, **k: alive["n"])
    monkeypatch.setattr(host_module.desktop, "spawn_gateway",
                        lambda settings, host, port: alive.update(n=True) or 4242)
    monkeypatch.setattr(host_module.desktop, "wait_gateway",
                        lambda host, port, seconds: alive["n"])
    reply = host_module.handle({"cmd": "start"}, settings=object())
    assert reply == {"ok": True, "running": True, "started": True, "pid": 4242}


def test_host_start_timeout_reports_log_hint(host_module, monkeypatch) -> None:
    monkeypatch.setattr(host_module.desktop, "gateway_alive", lambda *a, **k: False)
    monkeypatch.setattr(host_module.desktop, "spawn_gateway", lambda settings, host, port: 1)
    monkeypatch.setattr(host_module.desktop, "wait_gateway", lambda host, port, s: False)
    reply = host_module.handle({"cmd": "start"}, settings=object())
    assert reply["ok"] is False and "gateway.log" in reply["error"]


def test_host_stop_paths(host_module, monkeypatch) -> None:
    monkeypatch.setattr(host_module.desktop, "gateway_alive", lambda *a, **k: False)
    monkeypatch.setattr(host_module.desktop, "stop_gateway",
                        lambda *a, **k: pytest.fail("未运行时不得调用停止"))
    assert host_module.handle({"cmd": "stop"})["stopped"] is False
    monkeypatch.setattr(host_module.desktop, "gateway_alive", lambda *a, **k: True)
    monkeypatch.setattr(host_module.desktop, "stop_gateway",
                        lambda *a, **k: {"ok": True, "stopped": True})
    reply = host_module.handle({"cmd": "stop"})
    assert reply == {"ok": True, "running": False, "stopped": True}


# --------------------------------------------------------------------------- #
# 静态契约：弹窗 UI / manifest / 启动器三处一致
# --------------------------------------------------------------------------- #
def test_popup_native_start_contract() -> None:
    popup_html = (EXT / "popup.html").read_text(encoding="utf-8")
    popup_js = (EXT / "popup.js").read_text(encoding="utf-8")
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert 'id="gw-start"' in popup_html, "弹窗必须有「启动网关」按钮"
    assert 'sendNativeMessage(NATIVE_HOST' in popup_js
    assert 'const NATIVE_HOST = "com.pxb7.gateway";' in popup_js
    assert "nativeMessaging" in manifest["permissions"], "启停宿主需要 nativeMessaging 权限"
    bat = (PROJECT_ROOT / "extension" / "native-host" / NH.HOST_LAUNCHER).read_bytes()
    assert b"pxb7_gateway_host.py" in bat, "启动器必须指向宿主脚本"
    assert b"\r\n" in bat, ".bat 按仓库约定为 CRLF"
