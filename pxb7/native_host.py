"""Native Messaging 宿主注册：让扩展弹窗一键启停本机网关（2026-10-04 用户指令）。

组成：
- ``extension_id_for_path``：按 Chromium 通用规则推导**解包扩展**的扩展 ID
  （SHA256(路径 UTF-16LE) 前 32 个十六进制位映射到 a–p）。规则与浏览器实现若不一致
  （浏览器版本/路径形态差异），可用 ``--ext-id`` 传扩展页显示的 ID 覆盖；
- ``build_host_manifest``：生成宿主清单 ``com.pxb7.gateway.json``（绝对路径启动器 +
  allowed_origins 只登记本扩展，不开放通配）；
- ``registry_roots``：Chromium 系浏览器 HKCU 的 NativeMessagingHosts 键
  （Chrome/Edge/Chromium 为官方路径；夸克为尽力而为，未验证其键名）；
- ``install`` / ``uninstall`` / ``check``：写/删/查（HKCU 无需管理员）。

安全边界：宿主（pxb7_gateway_host.py）只接受 status/start/stop 三个命令，只作用于
本机回环网关（pxb7.desktop 回环纪律）；清单 allowed_origins 精确到本扩展 ID。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, Settings

HOST_NAME = "com.pxb7.gateway"
HOST_DIR = PROJECT_ROOT / "extension" / "native-host"
HOST_SCRIPT = "pxb7_gateway_host.py"
HOST_LAUNCHER = "run-host.bat"
MANIFEST_FILENAME = "com.pxb7.gateway.json"

_EXT_ID_RE = re.compile(r"^[a-p]{32}$")

# HKCU 键（无管理员权限）；夸克为 Chromium 系但键名未实测，尽力而为并如实标注
_REGISTRY_ROOTS: tuple[tuple[str, str], ...] = (
    ("Chrome", r"Software\Google\Chrome\NativeMessagingHosts"),
    ("Edge", r"Software\Microsoft\Edge\NativeMessagingHosts"),
    ("Chromium", r"Software\Chromium\NativeMessagingHosts"),
    ("夸克（键名未验证，尽力而为）", r"Software\Quark\Quark\NativeMessagingHosts"),
)


class NativeHostError(ValueError):
    """注册/注销参数不合法。"""


def extension_id_for_path(ext_dir: Path | None = None) -> str:
    """按 Chromium 通用规则由**解包扩展目录的绝对路径**推导扩展 ID（a–p 32 位）。"""
    path = (ext_dir or (PROJECT_ROOT / "extension" / "pxb7-extension")).resolve()
    digest = hashlib.sha256(str(path).encode("utf-16-le")).hexdigest()[:32]
    return "".join(chr(int(c, 16) + ord("a")) for c in digest)


def normalize_ext_id(ext_id: str | None, ext_dir: Path | None = None) -> str:
    """校验/推导扩展 ID：不给（None）按路径推导；给了就必须是 32 位 a–p（含空白即报错）。"""
    if ext_id is None:
        return extension_id_for_path(ext_dir)
    cleaned = str(ext_id).strip()
    if not _EXT_ID_RE.match(cleaned):
        raise NativeHostError(
            f"扩展 ID 必须是 32 位 a–p 字符（扩展管理页可复制），实际 {ext_id!r}")
    return cleaned


def build_host_manifest(ext_id: str, ext_dir: Path | None = None) -> dict[str, Any]:
    """宿主清单：绝对路径启动器 + allowed_origins 精确登记本扩展（不开放通配）。"""
    launcher = (ext_dir or HOST_DIR).resolve() / HOST_LAUNCHER
    if not launcher.is_file():
        raise NativeHostError(f"宿主启动器缺失：{launcher}")
    return {
        "name": HOST_NAME,
        "description": "pxb7 采集网关本机启停（仅 status/start/start-stop 本项目回环网关）",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }


def manifest_path(ext_dir: Path | None = None) -> Path:
    return (ext_dir or HOST_DIR) / MANIFEST_FILENAME


def _write_registry(roots: tuple[tuple[str, str], ...], manifest: Path) -> list[tuple[str, bool]]:
    if sys.platform != "win32":
        return [("非 Windows 平台跳过注册表", False)]
    import winreg
    results: list[tuple[str, bool]] = []
    for label, root in roots:
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{root}\\{HOST_NAME}") as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(manifest))
            results.append((label, True))
        except OSError as exc:
            results.append((label, f"{type(exc).__name__}: {exc}"))
    return results


def _delete_registry(roots: tuple[tuple[str, str], ...]) -> list[tuple[str, bool]]:
    if sys.platform != "win32":
        return [("非 Windows 平台跳过注册表", False)]
    import winreg
    results: list[tuple[str, bool]] = []
    for label, root in roots:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{root}\\{HOST_NAME}")
            results.append((label, True))
        except FileNotFoundError:
            results.append((label, "本就未登记"))
        except OSError as exc:
            results.append((label, f"{type(exc).__name__}: {exc}"))
    return results


def install(settings: Settings, *, ext_id: str | None = None,
            ext_dir: Path | None = None) -> dict[str, Any]:
    """写宿主清单 + 登记 HKCU 注册表（幂等：重复执行覆盖为最新路径/ID）。"""
    resolved = normalize_ext_id(ext_id, ext_dir)
    manifest = build_host_manifest(resolved, ext_dir)
    path = manifest_path(ext_dir)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    roots = _write_registry(_REGISTRY_ROOTS, path)
    return {"ok": True, "action": "install", "ext_id": resolved, "ext_id_source":
            "override" if (ext_id and str(ext_id).strip()) else "auto(路径推导)",
            "manifest": str(path), "launcher": manifest["path"], "registry": roots,
            "note": "浏览器内需重载扩展（或等自更新）后，弹窗「启动网关」才可用"}


def uninstall(*, ext_dir: Path | None = None) -> dict[str, Any]:
    """注销注册表并删除宿主清单文件（注册表值不存在不算失败）。"""
    roots = _delete_registry(_REGISTRY_ROOTS)
    path = manifest_path(ext_dir)
    removed = False
    if path.is_file():
        path.unlink()
        removed = True
    return {"ok": True, "action": "uninstall", "registry": roots, "manifest_removed": removed}


def check(settings: Settings, *, ext_dir: Path | None = None) -> dict[str, Any]:
    """只读检查：清单是否存在、注册表是否指向它、网关是否在运行。"""
    from . import desktop
    path = manifest_path(ext_dir)
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    registered: list[tuple[str, bool]] = []
    if sys.platform == "win32":
        import winreg
        for label, root in _REGISTRY_ROOTS:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{root}\\{HOST_NAME}") as key:
                    value, _ = winreg.QueryValueEx(key, None)
                registered.append((label, str(value) == str(path)))
            except OSError:
                registered.append((label, False))
    return {"ok": True, "manifest_path": str(path), "manifest_exists": path.is_file(),
            "manifest": manifest, "registry": registered,
            "gateway_running": desktop.gateway_alive()}
