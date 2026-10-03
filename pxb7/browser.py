"""Playwright 会话管理（登录态优先 v1.3，docs/01 §3.1/§3.3）。

职责：
1. **登录态优先**：按 主登录态 storage_state → 备登录态 → 游客态 的顺序选择会话；
   主备文件都不存在时直接用游客态，collected_via 如实标注。
   - 主登录态被踢/失效 → 切备用，频率降至 1/2（factor 2.0）；
   - 备用亦失效 → 降级游客态 + 72h 观察（游客态与登录态同档；验证码后试探恢复才用 1/4 档）；
   - 主备切换与降级由 RiskMachine 决定，本模块只执行并回传 slot；
   - **不并发轮换、不连续重试登录**（同一登录态不做多任务并发）。
2. **请求守卫**：导航前用 urlguard.assert_page_url 校验（http/https + host 白名单 + 拒绝
   私有/回环/保留地址），不通过则**不发起请求**；页面子资源用 route 拦截做同样判定。
3. **robots 合规**：/_nuxt/ 与 /assets/ 不作为页面采集对象（不落 raw、不解析），
   渲染需要时仍放行加载。

本模块不做任何「指纹伪造 / 代理池 / 打码」动作（docs/01 §3.2 红线）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .risk import SLOT_BACKUP, SLOT_GUEST, SLOT_PRIMARY
from .urlguard import UnsafeUrlError, assert_page_url, classify_request_url, is_robots_disallowed

COLLECTED_VIA_LOGIN = "login"
COLLECTED_VIA_GUEST = "guest"


class BrowserUnavailable(RuntimeError):
    """Playwright 不可用（未安装/未安装浏览器内核）。"""


@dataclass
class GuardStats:
    """请求守卫与导航统计（进 summary / 排障）。"""
    requests_seen: int = 0
    requests_allowed: int = 0
    requests_blocked: int = 0
    robots_skipped: int = 0
    denied_urls: list[str] = _dc_field(default_factory=list)
    block_reasons: dict[str, int] = _dc_field(default_factory=dict)

    def note_block(self, url: str, reason: str) -> None:
        self.requests_blocked += 1
        self.block_reasons[reason] = self.block_reasons.get(reason, 0) + 1
        if len(self.denied_urls) < 50:
            self.denied_urls.append(f"{reason} :: {url[:200]}")


@dataclass
class SessionChoice:
    slot: str                 # primary / backup / guest
    storage_state: Path | None
    collected_via: str        # login / guest
    reason: str


@dataclass
class NavigationResult:
    url: str
    status: int | None
    ok: bool
    elapsed_s: float
    error: str | None = None
    body_text_len: int = 0


def validate_storage_state(path: str | Path) -> tuple[bool, str]:
    """校验登录态文件是否**真的可用**（文件存在 ≠ 已登录）。

    只认「含 cookie/origins」的 storage_state；空 cookie 的文件说明从未登录成功或已被清空，
    此时若仍标 collected_via=login 会让数据来源失真。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return (False, "文件不存在")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (False, f"登录态文件不可读/非 JSON：{type(exc).__name__}")
    if not isinstance(data, dict):
        return (False, "登录态文件结构异常（非对象）")
    cookies = data.get("cookies") or []
    origins = data.get("origins") or []
    if not cookies and not origins:
        return (False, "文件存在但不含 cookie/origins（未真正登录或已失效）")
    return (True, f"ok（{len(cookies)} cookies / {len(origins)} origins）")


def choose_session(settings: Settings, *, slot: str | None = None,
                   mode: str | None = None) -> SessionChoice:
    """决定用哪个登录态；文件缺失或**内容不可用**都降级（不报错、不重试登录）。"""
    primary = Path(settings.paths.storage_primary)
    backup = Path(settings.paths.storage_backup)
    primary_ok, primary_why = validate_storage_state(primary)
    backup_ok, backup_why = validate_storage_state(backup)

    if mode == "guest":
        return SessionChoice(SLOT_GUEST, None, COLLECTED_VIA_GUEST, "显式指定 guest")
    wanted = slot or SLOT_PRIMARY
    if wanted == SLOT_PRIMARY:
        if primary_ok:
            return SessionChoice(SLOT_PRIMARY, primary, COLLECTED_VIA_LOGIN, "主登录态可用")
        if backup_ok:
            return SessionChoice(SLOT_BACKUP, backup, COLLECTED_VIA_LOGIN,
                                 f"主登录态不可用（{primary_why}），回落备用登录态")
        return SessionChoice(SLOT_GUEST, None, COLLECTED_VIA_GUEST,
                             f"主备登录态均不可用（主：{primary_why}；备：{backup_why}），使用游客态")
    if wanted == SLOT_BACKUP and backup_ok:
        return SessionChoice(SLOT_BACKUP, backup, COLLECTED_VIA_LOGIN, "备用登录态可用")
    if wanted == SLOT_GUEST:
        return SessionChoice(SLOT_GUEST, None, COLLECTED_VIA_GUEST, "已降级游客态")
    return SessionChoice(SLOT_GUEST, None, COLLECTED_VIA_GUEST,
                         f"备用登录态不可用（{backup_why}），降级游客态")


class BrowserSession:
    """Playwright chromium 会话（同步 API）。

    用法::

        with BrowserSession(settings) as s:
            page = s.new_page()
            res = s.goto(page, url)
    """

    def __init__(self, settings: Settings, *, slot: str | None = None, mode: str | None = None,
                 headless: bool | None = None,
                 logger: Callable[[str], None] | None = None):
        self.settings = settings
        self.opts = settings.collect_opts
        self.choice = choose_session(settings, slot=slot, mode=mode)
        self.headless = self.opts.headless if headless is None else headless
        self.log = logger or (lambda msg: None)
        self.guard = GuardStats()
        self._playwright = None
        self._browser = None
        self._context = None
        self.downgrades: list[str] = []

    # -- 属性 ------------------------------------------------------------ #
    @property
    def collected_via(self) -> str:
        return COLLECTED_VIA_LOGIN if self.choice.slot in (SLOT_PRIMARY, SLOT_BACKUP) \
            else COLLECTED_VIA_GUEST

    @property
    def slot(self) -> str:
        return self.choice.slot

    @property
    def frequency_factor(self) -> float:
        return self.settings.rate_limit.factor_for(self.choice.slot)

    @property
    def storage_state_path(self) -> str | None:
        return str(self.choice.storage_state) if self.choice.storage_state else None

    def session_info(self) -> dict[str, Any]:
        return {
            "slot": self.choice.slot,
            "collected_via": self.collected_via,
            "storage_state": self.storage_state_path,
            "reason": self.choice.reason,
            "frequency_factor": self.frequency_factor,
            "headless": self.headless,
            "downgrades": list(self.downgrades),
        }

    # -- 生命周期 -------------------------------------------------------- #
    def start(self) -> "BrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise BrowserUnavailable(
                "未安装 playwright；执行 .venv/Scripts/python.exe -m playwright install chromium"
            ) from exc
        self._playwright = sync_playwright().start()
        launch_args = {"headless": self.headless}
        try:
            self._browser = self._playwright.chromium.launch(**launch_args)
        except Exception as exc:  # pragma: no cover - 浏览器内核缺失等
            self._playwright.stop()
            self._playwright = None
            raise BrowserUnavailable(f"chromium 启动失败：{exc}") from exc

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
        }
        if self.choice.storage_state:
            try:
                self._context = self._browser.new_context(
                    storage_state=str(self.choice.storage_state), **context_kwargs)
            except Exception as exc:
                # 载入失败后新建的上下文**没有**任何登录态：collected_via 必须如实改标
                # guest——沿用 login 标注会让数据来源失真（终审发现项 #9）。
                self.log(f"[warn] 载入登录态失败（{exc}），按游客上下文继续并如实标注")
                self.choice = SessionChoice(
                    SLOT_GUEST, None, COLLECTED_VIA_GUEST,
                    f"storage_state 载入失败（{type(exc).__name__}），按游客上下文运行")
                self.downgrades.append("storage_state-load-failed -> guest")
                self._context = self._browser.new_context(**context_kwargs)
        else:
            self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(self.opts.selector_timeout_ms)
        self._context.set_default_navigation_timeout(self.opts.navigation_timeout_ms)
        self._install_guard(self._context)
        self.log(f"[session] slot={self.choice.slot} via={self.collected_via} "
                 f"factor={self.frequency_factor} ({self.choice.reason})")
        return self

    def stop(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._context = self._browser = self._playwright = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- 请求守卫 -------------------------------------------------------- #
    def _install_guard(self, context) -> None:
        context.route("**/*", self._route_handler)

    def _route_handler(self, route, request) -> None:  # pragma: no cover - 需浏览器
        self.guard.requests_seen += 1
        url = request.url
        allowed, reason = classify_request_url(url)
        if not allowed:
            self.guard.note_block(url, reason)
            self.log(f"[guard] 拒绝请求（{reason}）：{url[:120]}")
            try:
                route.abort()
            except Exception:
                pass
            return
        if is_robots_disallowed(url):
            # robots 禁抓路径：渲染需要则放行，但不作为页面采集对象
            self.guard.robots_skipped += 1
        self.guard.requests_allowed += 1
        try:
            route.continue_()
        except Exception:
            pass

    # -- 导航 ------------------------------------------------------------ #
    def new_page(self):
        if self._context is None:
            raise BrowserUnavailable("会话未启动（先 start() 或用 with 语句）")
        return self._context.new_page()

    def goto(self, page, url: str, *, wait_until: str = "domcontentloaded",
             guard_page: bool = True) -> NavigationResult:
        """导航到 url；**先过 URL 守卫**，不通过则直接返回失败且不发起请求。"""
        if guard_page:
            try:
                assert_page_url(url, allowed_hosts=self.settings.site.allowed_hosts,
                                require_https=True,
                                resolve_dns=self.settings.site.resolve_dns)
            except UnsafeUrlError as exc:
                self.guard.note_block(url, f"page-guard:{exc}")
                self.log(f"[guard] 导航被拒：{exc}")
                return NavigationResult(url=url, status=None, ok=False, elapsed_s=0.0,
                                        error=f"urlguard:{exc}")
        started = time.monotonic()
        try:
            response = page.goto(url, wait_until=wait_until,
                                 timeout=self.opts.navigation_timeout_ms)
            status = response.status if response is not None else None
            body_len = 0
            try:
                body_len = len(page.content())
            except Exception:
                pass
            return NavigationResult(url=url, status=status, ok=True,
                                    elapsed_s=time.monotonic() - started, body_text_len=body_len)
        except Exception as exc:
            return NavigationResult(url=url, status=None, ok=False,
                                    elapsed_s=time.monotonic() - started, error=str(exc)[:300])

    def wait_for_cards(self, page, selectors, *, timeout_ms: int | None = None) -> str | None:
        """等待卡片渲染：按候选选择器逐个尝试，返回命中的选择器（全部超时返回 None）。"""
        timeout = timeout_ms or self.opts.selector_timeout_ms
        for selector in selectors:
            try:
                page.wait_for_selector(selector, timeout=timeout)
                return selector
            except Exception:
                continue
        return None

    # -- 会话降级 -------------------------------------------------------- #
    def _downgrade(self, reason: str) -> None:
        order = {SLOT_PRIMARY: SLOT_BACKUP, SLOT_BACKUP: SLOT_GUEST, SLOT_GUEST: SLOT_GUEST}
        nxt = order.get(self.choice.slot, SLOT_GUEST)
        self.choice = choose_session(self.settings, slot=nxt)
        self.downgrades.append(f"{reason} -> {self.choice.slot}")
        self.log(f"[session] 降级：{reason} -> slot={self.choice.slot} "
                 f"via={self.collected_via} factor={self.frequency_factor}")

    def downgrade_to_backup(self, reason: str = "主登录态被风控/踢出") -> SessionChoice:
        """主登录态被踢 → 切备用（docs/01 §3.3）。"""
        self._downgrade(reason)
        return self.choice

    def downgrade_to_guest(self, reason: str = "备用登录态亦失效") -> SessionChoice:
        """主备均失效 → 游客态 + 72h 观察（由 RiskMachine 记录观察期）。"""
        self.choice = choose_session(self.settings, mode="guest")
        self.downgrades.append(f"{reason} -> guest")
        self.log(f"[session] 降级：{reason} -> guest（collected_via=guest）")
        return self.choice
