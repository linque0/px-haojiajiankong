"""桌面前端：把采集看板做成「双击即用」的独立窗口程序（Windows）。

组成（全部零新增依赖）：
- ``gateway_alive``：本机网关回环探活（127.0.0.1，项目自带服务）；
- ``spawn_gateway``：未运行时以**分离进程**（pythonw + DETACHED_PROCESS，无控制台窗口）
  拉起 ``run.py serve``，日志写 ``data/logs/gateway.log``；
- ``launch_dashboard``：用系统自带 Edge/Chrome 的 ``--app`` 模式打开无地址栏应用窗口
  （看起来就是一个独立软件）；找不到浏览器退回默认浏览器；
- ``stop_gateway``：调本机网关 ``/shutdown`` 优雅停止；
- ``install_shortcut``：在桌面创建「pxb7采集看板」快捷方式（PowerShell -EncodedCommand，
  中文路径安全）。

SSRF 边界（安全约束，硬性）：本模块**只允许访问项目自带的本机回环网关**——
- host 必须在回环白名单（127.0.0.1 / localhost），且用 ``ipaddress`` 复核解析结果
  必须全部为回环地址（纵深防御，防参数被注入指向内网/云元数据地址）；
- 仅 http；不跟随重定向（重定向到内网是最常见的 SSRF 绕过手法）；
- 不访问任何外部地址。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import PROJECT_ROOT, Settings

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 8765
SHORTCUT_NAME = "pxb7采集看板"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000

_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


# --------------------------------------------------------------------------- #
# SSRF 边界：只允许回环
# --------------------------------------------------------------------------- #
class LoopbackOnlyError(ValueError):
    """目标不在本机回环白名单内。"""


def assert_loopback(host: str, port: int) -> None:
    """校验目标为**本机回环**网关：白名单 host + 解析结果全部为回环地址。

    本模块只服务项目自带的本机网关（绑定 127.0.0.1）；任何非回环目标一律拒绝，
    不解析、不连接、不重定向跟随。
    """
    if host not in _LOOPBACK_HOSTS:
        raise LoopbackOnlyError(f"仅允许本机回环地址（{sorted(_LOOPBACK_HOSTS)}），实际 {host!r}")
    if not (0 < int(port) < 65536):
        raise LoopbackOnlyError(f"端口非法：{port!r}")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise LoopbackOnlyError(f"回环地址解析失败：{host}（{exc}）") from exc
    for info in infos:
        resolved = ipaddress.ip_address(info[4][0])
        if not resolved.is_loopback:
            raise LoopbackOnlyError(f"解析结果非回环：{host} -> {resolved}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向：3xx 一律不跟随（防重定向到内网/元数据地址）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _loopback_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


# --------------------------------------------------------------------------- #
# 探活 / 启动 / 停止
# --------------------------------------------------------------------------- #
def gateway_alive(host: str = GATEWAY_HOST, port: int = GATEWAY_PORT,
                  timeout: float = 0.6) -> bool:
    """回环探活：能建立 TCP 连接即认为网关在运行（非回环目标直接拒绝）。"""
    assert_loopback(host, port)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_gateway(host: str = GATEWAY_HOST, port: int = GATEWAY_PORT,
                 seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if gateway_alive(host, port):
            return True
        time.sleep(0.3)
    return False


def find_browser(*, candidates: tuple[str, ...] | None = None) -> str | None:
    """找系统自带的 Edge/Chrome（可用环境变量 PXB7_BROWSER 覆盖）。"""
    env = os.environ.get("PXB7_BROWSER")
    if env and Path(env).is_file():
        return env
    for path in (candidates if candidates is not None else _BROWSER_CANDIDATES):
        if Path(path).is_file():
            return path
    return None


def spawn_gateway(settings: Settings, *, host: str = GATEWAY_HOST, port: int = GATEWAY_PORT,
                  python_exe: str | None = None) -> int:
    """把网关作为**分离后台进程**拉起（无控制台窗口；随用户进程独立存活）。"""
    assert_loopback(host, port)
    exe = Path(python_exe) if python_exe else Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    runner = windowless if windowless.is_file() else exe
    log_path = Path(settings.paths.log_dir) / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = (_DETACHED_PROCESS | _CREATE_NO_WINDOW) if os.name == "nt" else 0
    with open(log_path, "ab") as fh:
        proc = subprocess.Popen(
            [str(runner), str(PROJECT_ROOT / "run.py"), "serve",
             "--host", host, "--port", str(port), "--log-file", str(log_path)],
            cwd=str(PROJECT_ROOT), stdout=fh, stderr=fh,
            creationflags=flags, close_fds=True)
    return proc.pid


def launch_dashboard(settings: Settings, *, host: str = GATEWAY_HOST, port: int = GATEWAY_PORT,
                     wait_seconds: float = 10.0) -> dict:
    """确保网关在跑，然后用应用窗口（或默认浏览器）打开看板。"""
    assert_loopback(host, port)          # 非回环目标在启动前即拒绝
    started = False
    if not gateway_alive(host, port):
        pid = spawn_gateway(settings, host=host, port=port)
        if not wait_gateway(host, port, wait_seconds):
            return {"ok": False, "error": "网关启动超时，请查看 data/logs/gateway.log",
                    "pid": pid}
        started = True
    url = f"http://{host}:{port}/"
    browser = find_browser()
    if browser:
        subprocess.Popen([browser, f"--app={url}", "--window-size=1280,920"],
                         close_fds=True)
        return {"ok": True, "opened": "app-window", "browser": browser, "url": url,
                "started_gateway": started}
    import webbrowser
    webbrowser.open(url)
    return {"ok": True, "opened": "default-browser", "url": url,
            "started_gateway": started,
            "note": "未找到 Edge/Chrome，已用默认浏览器打开（无应用窗口模式）"}


def stop_gateway(host: str = GATEWAY_HOST, port: int = GATEWAY_PORT,
                 timeout: float = 3.0) -> dict:
    """优雅停止本机网关（POST /shutdown；仅回环、不跟随重定向）。"""
    assert_loopback(host, port)
    if not gateway_alive(host, port):
        return {"ok": True, "already_stopped": True}
    request = urllib.request.Request(
        f"http://{host}:{port}/shutdown", method="POST", data=b"{}",
        headers={"Content-Type": "application/json"})
    try:
        with _loopback_opener().open(request, timeout=timeout) as response:
            ok = response.status == 200
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    for _ in range(20):                        # 等待端口真正释放
        if not gateway_alive(host, port):
            break
        time.sleep(0.2)
    return {"ok": ok, "stopped": not gateway_alive(host, port)}


_ALLOWED_GATEWAY_PATHS = frozenset({"/", "/status", "/stats", "/config", "/dashboard"})
_ALLOWED_GATEWAY_POST_PATHS = frozenset({"/config"})   # 只允许网关自己的配置端点


def gateway_json(path: str = "/status", *, host: str = GATEWAY_HOST,
                 port: int = GATEWAY_PORT, timeout: float = 3.0) -> dict:
    """读取本机网关的 JSON（仅回环 + 仅网关固定路由 + 不跟随重定向）。"""
    assert_loopback(host, port)
    if path not in _ALLOWED_GATEWAY_PATHS:
        raise LoopbackOnlyError(f"仅允许网关固定路由：{path!r}")
    request = urllib.request.Request(f"http://{host}:{port}{path}", method="GET")
    with _loopback_opener().open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def gateway_post_json(path: str, payload: dict, *, host: str = GATEWAY_HOST,
                      port: int = GATEWAY_PORT, timeout: float = 5.0) -> dict:
    """向本机网关 POST 一个 JSON 对象（仅回环 + 固定路由白名单 + 不跟随重定向）。

    与 gateway_json 同一套 SSRF 边界：host 必须在回环白名单、端口合法、解析结果
    全部为回环地址，路径只能是网关自己的配置端点——不接受任意 URL 或任意路径。
    供工具脚本（如 tools/e2e_extension.py）在测试期间调整插件配置后用原值恢复。
    """
    assert_loopback(host, port)
    if path not in _ALLOWED_GATEWAY_POST_PATHS:
        raise LoopbackOnlyError(f"仅允许网关固定路由：{path!r}")
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://{host}:{port}{path}", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with _loopback_opener().open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# 桌面快捷方式
# --------------------------------------------------------------------------- #
def build_shortcut_ps(project_root: Path = PROJECT_ROOT, *, name: str = SHORTCUT_NAME,
                      python_exe: str | None = None) -> str:
    """构造创建桌面快捷方式的 PowerShell 脚本（中文路径经 -EncodedCommand 传递）。"""
    exe = Path(python_exe) if python_exe else Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    target = windowless if windowless.is_file() else exe
    args = f'"{project_root / "run.py"}" dashboard'
    return (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$lnk = $ws.CreateShortcut(([Environment]::GetFolderPath('Desktop')) + '\\{name}.lnk'); "
        f"$lnk.TargetPath = '{target}'; "
        f"$lnk.Arguments = '{args}'; "
        f"$lnk.WorkingDirectory = '{project_root}'; "
        "$lnk.IconLocation = '%SystemRoot%\\System32\\SHELL32.dll,220'; "
        "$lnk.Description = 'pxb7 采集看板（前端 B：本地采集通道）'; "
        "$lnk.Save()"
    )


def _decode_console(raw: bytes | None) -> str:
    """容错解码控制台输出（Windows PowerShell 可能按 UTF-8/GBK 混排）。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)[:200]
        except UnicodeDecodeError:
            continue
    return repr(raw[:200])


def install_shortcut(project_root: Path = PROJECT_ROOT) -> dict:
    """在桌面创建快捷方式（Windows；经 PowerShell 执行）。"""
    if os.name != "nt":
        return {"ok": False, "error": "仅支持 Windows"}
    script = build_shortcut_ps(project_root)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=30)          # 字节捕获：控制台编码不一，勿按文本解码
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"ok": proc.returncode == 0, "stdout": _decode_console(proc.stdout),
            "stderr": _decode_console(proc.stderr)}
