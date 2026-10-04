"""Native Messaging 宿主：让浏览器扩展一键启停本机网关（2026-10-04 用户指令）。

协议（Chromium Native Messaging，stdio）：4 字节小端 uint32 长度 + UTF-8 JSON，逐条收发。
由浏览器按 NativeMessagingHosts 注册表登记的启动器（run-host.bat）拉起本脚本；
扩展端用 chrome.runtime.sendNativeMessage("com.pxb7.gateway", …) 通信。

安全边界（docs/10 §7.4，硬性）：
- 命令白名单只有 status / start / stop 三个，全部**只作用于项目自带的本机回环网关**
  （127.0.0.1:8765），复用 pxb7.desktop 的回环 SSRF 纪律（白名单 + 解析复核）；
- 请求里的任何字段都不会成为地址、路径或命令；不做白名单之外的任何事；
- 不读 Cookie/登录态，不落盘任何认证信息。
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # extension/native-host/ 之上两级 = 仓库根
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import desktop  # noqa: E402
from pxb7.config import load_settings  # noqa: E402

HOST_NAME = "com.pxb7.gateway"
_COMMANDS = ("status", "start", "stop")
_START_WAIT_SECONDS = 15.0


def handle(request, *, settings=None) -> dict:
    """处理一条扩展请求并返回应答；只做白名单内的本机回环网关操作。"""
    cmd = request.get("cmd") if isinstance(request, dict) else None
    if cmd not in _COMMANDS:
        return {"ok": False, "error": f"未知命令：{cmd!r}（允许：{'、'.join(_COMMANDS)}）"}
    host, port = desktop.GATEWAY_HOST, desktop.GATEWAY_PORT
    if cmd == "status":
        return {"ok": True, "running": desktop.gateway_alive(host, port)}
    if cmd == "start":
        if desktop.gateway_alive(host, port):
            return {"ok": True, "running": True, "started": False, "note": "网关已在运行"}
        try:
            pid = desktop.spawn_gateway(settings or load_settings(), host=host, port=port)
        except OSError as exc:
            return {"ok": False, "error": f"拉起网关失败：{exc}"}
        if not desktop.wait_gateway(host, port, _START_WAIT_SECONDS):
            return {"ok": False, "error": "网关启动超时，请查看 data/logs/gateway.log", "pid": pid}
        return {"ok": True, "running": True, "started": True, "pid": pid}
    # stop
    if not desktop.gateway_alive(host, port):
        return {"ok": True, "running": False, "stopped": False, "note": "网关本就未运行"}
    result = desktop.stop_gateway(host, port)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "停止失败")}
    return {"ok": True, "running": False, "stopped": result.get("stopped", True)}


def _read_message(stdin):
    head = stdin.buffer.read(4)
    if len(head) < 4:
        return None                      # 浏览器关闭端口（或一次性消息结束）
    (length,) = struct.unpack("<I", head)
    if not 0 < length <= 1_048_576:      # 1 MiB 上限：协议自防，不读超大报文
        return None
    raw = stdin.buffer.read(length)
    if len(raw) < length:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}                        # 无法解析的报文按"未知命令"应答，不让浏览器侧悬挂


def _reply(message: dict) -> None:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


def main() -> int:
    while True:
        request = _read_message(sys.stdin)
        if request is None:
            return 0
        try:
            _reply(handle(request))
        except Exception as exc:         # 兜底：任何异常都回 JSON，不让扩展侧无限等待
            _reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    sys.exit(main())
