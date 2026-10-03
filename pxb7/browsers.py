"""Chromium 系浏览器扩展管理入口：发现本机浏览器 + 生成逐浏览器更新/安装指引。

背景：本扩展以**已解压目录**方式安装（`extension/pxb7-extension`），代码更新后浏览器
不会自动重读——必须在**每个装了它的浏览器**里点一次「重新加载」（浏览器重启也会从
磁盘重读）。本模块负责：

1. 发现本机已安装的 Chromium 系浏览器（注册表 + 固定候选路径），覆盖
   Edge / Chrome / 夸克 / Brave / Vivaldi / QQ 等；
2. 给出各自**扩展管理页**（edge://extensions、chrome://extensions、quark://extensions…）
   与逐浏览器操作步骤（含夸克「开发者模式/夸克实验室」特殊说明）；
3. 标注哪些是「标准安装位置」（bat 会代为打开扩展页），哪些是自定义位置（需手动打开）。

边界说明：本模块**不启动任何进程**（浏览器由 `更新浏览器扩展.bat` / `安装浏览器扩展.bat`
以固定字面量路径打开）；只读取注册表与文件系统判断"装没装、装在哪"。
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# 浏览器白名单：exe 小写名 → (键, 显示名, 扩展管理页, 备注)
_KNOWN_BROWSERS: dict[str, tuple[str, str, str, str]] = {
    "msedge.exe": ("edge", "Microsoft Edge", "edge://extensions/", ""),
    "chrome.exe": ("chrome", "Google Chrome", "chrome://extensions/", ""),
    "quark.exe": ("quark", "夸克浏览器", "quark://extensions/",
                  "若无「开发者模式」开关：菜单→关于夸克→连点版本号 7 次→夸克实验室→"
                  "开启「扩展支持」实验功能（需 v6.9+ 原生 PC 版）"),
    "brave.exe": ("brave", "Brave", "brave://extensions/", ""),
    "vivaldi.exe": ("vivaldi", "Vivaldi", "vivaldi://extensions/", ""),
    "launcher.exe": ("opera", "Opera", "opera://extensions/", ""),
    "qqbrowser.exe": ("qq", "QQ 浏览器", "qqbrowser://extensions/",
                      "若打不开扩展页，请在地址栏手动输入 qqbrowser://extensions"),
    "360se.exe": ("360se", "360 安全浏览器", "chrome://extensions/",
                  "若打不开扩展页，请从浏览器菜单进入「扩展管理」"),
    "360chrome.exe": ("360chrome", "360 极速浏览器", "chrome://extensions/",
                      "若打不开扩展页，请从浏览器菜单进入「扩展管理」"),
}

# 标准安装位置（bat 会按这些字面量路径代开扩展页；其余为自定义位置，给出命令由用户打开）
_STANDARD_PATHS: tuple[str, ...] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Quark\Quark.exe",
    r"C:\Program Files (x86)\Quark\Quark.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
    r"C:\Program Files\Tencent\QQBrowser\QQBrowser.exe",
    r"C:\Program Files (x86)\Tencent\QQBrowser\QQBrowser.exe",
)

# 优先级：日常更新时按此顺序展示（Chrome/Edge 最常见）
_ORDER = {"chrome": 0, "edge": 1, "quark": 2}


@dataclass(frozen=True)
class Browser:
    key: str
    name: str
    exe: Path
    extensions_url: str
    note: str = ""

    @property
    def standard_location(self) -> bool:
        """是否位于标准安装位置（bat 会代为打开扩展页）。"""
        return any(Path(p).resolve() == self.exe.resolve() for p in _STANDARD_PATHS)

    def as_dict(self) -> dict[str, object]:
        return {"key": self.key, "name": self.name, "exe": str(self.exe),
                "extensions_url": self.extensions_url, "note": self.note,
                "standard_location": self.standard_location}


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #
def identify(exe: str | os.PathLike[str]) -> Browser | None:
    """按可执行文件名识别浏览器；未知 exe 返回 None（不猜）。"""
    path = Path(exe)
    info = _KNOWN_BROWSERS.get(path.name.lower())
    if info is None:
        return None
    key, name, url, note = info
    return Browser(key=key, name=name, exe=path, extensions_url=url, note=note)


def _registry_candidates() -> list[Path]:
    """从注册表 StartMenuInternet / App Paths 枚举浏览器 exe（Windows；只读）。"""
    if os.name != "nt":
        return []
    import winreg

    found: list[Path] = []

    def _read(hive: int, sub: str, value_name: str | None = None) -> str | None:
        try:
            with winreg.OpenKey(hive, sub) as key:
                return str(winreg.QueryValueEx(key, value_name)[0]) if value_name \
                    else str(winreg.QueryValue(key, None))
        except OSError:
            return None

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for base in (r"SOFTWARE\Clients\StartMenuInternet",
                     r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"):
            try:
                with winreg.OpenKey(hive, base) as root_key:
                    count = winreg.QueryInfoKey(root_key)[0]
                    for idx in range(count):
                        client = winreg.EnumKey(root_key, idx)
                        cmd = _read(hive, base + "\\" + client + r"\shell\open\command")
                        if not cmd:
                            continue
                        try:
                            tokens = shlex.split(cmd, posix=False)
                        except ValueError:
                            continue
                        if tokens:
                            found.append(Path(tokens[0].strip('"')))
            except OSError:
                continue
    # App Paths 兜底（Chrome/Edge/夸克等都会注册）
    for exe_name in ("chrome.exe", "msedge.exe", "quark.exe"):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            raw = _read(hive,
                        rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}")
            if raw:
                found.append(Path(raw.strip('"')))
    return found


def discover_browsers(*, candidates: Iterable[str | os.PathLike[str]] | None = None,
                      registry: bool = True) -> list[Browser]:
    """发现本机已安装的 Chromium 系浏览器（去重、按优先级排序）。

    candidates 给定时只在这些路径里找（测试/特殊安装位置用）；否则注册表 + 固定候选表。
    """
    if candidates is None:
        paths = list(_STANDARD_PATHS)
        if registry:
            paths.extend(str(p) for p in _registry_candidates())
    else:
        paths = [str(p) for p in candidates]
    seen: set[str] = set()
    out: list[Browser] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        resolved = str(path.resolve()).lower()
        if resolved in seen:
            continue
        seen.add(resolved)
        browser = identify(path)
        if browser is not None:
            out.append(browser)
    return sorted(out, key=lambda b: (_ORDER.get(b.key, 9), b.name))


# --------------------------------------------------------------------------- #
# 指引文本
# --------------------------------------------------------------------------- #
UPDATE_STEPS = (
    "在**每个已安装本扩展**的浏览器里打开上面的扩展页：找到「pxb7 采集助手」卡片 → 点「重新加载」。",
    "注意：Chrome/Edge/夸克 都会忽略命令行传入的 chrome:// / edge:// / quark:// 内部页"
    "（已实测），所以扩展页要靠地址栏进入——bat 会把地址复制到剪贴板，浏览器里 Ctrl+V 回车即可。",
    "扩展升级到 v0.3.0 后支持自更新：仓库代码更新时扩展会按磁盘目录自动重载，"
    "以后不必再手动「重新加载」（本次升级仍需手动一次，因为旧版没有自更新能力）。",
    "多个浏览器可同时安装本扩展（共用同一个本机网关）；网关数据不区分浏览器来源。",
)

INSTALL_STEPS = (
    "在每个浏览器的扩展页：开启右上角「开发者模式」→ 点「加载已解压的扩展程序」。",
    "选择本项目的 extension\\pxb7-extension 目录（目录根下必须有 manifest.json；"
    "不要选压缩包或子目录）。",
    "加载后把「pxb7 采集助手」固定到工具栏；采集前确保本机服务在运行（桌面「pxb7采集看板」）。",
)

# 已装 Tampermonkey 的浏览器（如夸克）可走免维护的用户脚本通道
USERSCRIPT_HINT = (
    "替代方案（零维护）：这些浏览器若装有 Tampermonkey，可直接加载 v0.5.0 用户脚本"
    "（看板「安装/状态」区的安装链接）——脚本的 @updateURL 指向本机网关，服务在跑时会"
    "自动检查更新，不必手动「重新加载」。"
)


def guide_lines(action: str, found: list[Browser]) -> list[str]:
    """生成逐浏览器指引（action: update / install）。"""
    steps = UPDATE_STEPS if action == "update" else INSTALL_STEPS
    lines: list[str] = []
    if not found:
        lines.append("未检测到 Chromium 系浏览器（Edge/Chrome/夸克…）。")
        return lines
    lines.append(f"检测到 {len(found)} 个 Chromium 系浏览器"
                 f"（✔=标准安装位置，bat 会把它列入引导流程；•=自定义位置，需手动打开）：")
    for browser in found:
        mark = "✔" if browser.standard_location else "•"
        lines.append(f"  {mark} {browser.name}：{browser.exe}")
        lines.append(f"      扩展页：{browser.extensions_url}")
        if browser.note:
            lines.append(f"      ⚠ {browser.note}")
        if not browser.standard_location:
            lines.append(f"      手动打开：双击上面的 exe 启动浏览器，地址栏输入 "
                         f"{browser.extensions_url}")
    lines.append("")
    for idx, step in enumerate(steps, start=1):
        lines.append(f"{idx}. {step}")
    lines.append("")
    lines.append(USERSCRIPT_HINT)
    return lines
