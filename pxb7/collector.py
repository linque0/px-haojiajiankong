"""列表页 / 详情页采集：渲染等待、翻页、限速、raw 落盘、空响应统计。

依据 docs/01 §2（URL 规律）/ §3.1（限速与轮次）/ §3.3（风控）/ §4（raw 层）；
契约 rateLimits：单页 3–6s 随机、任务间 ≥60s、每任务每轮 ≤5 页；登录态不放松限速。

raw 落盘（bronze，可重放）：``data/raw/pxb7/{task_id}/{YYYYMMDD}/{run_id}/``
- ``list_p{n:02d}.html`` + ``list_p{n:02d}.json``
- ``detail_{listing_id}.html`` + ``.json``
元数据 JSON 含：url、页类型、采集时间、HTTP 状态、collected_via、parser_version、
风控信号、guard 统计快照（契约 openQuestions：raw dump 格式需 .html + 同名元数据 json）。

合规：每次导航前过 URL 守卫；/_nuxt/、/assets/ 不作为页面采集对象（robots 禁抓）；
命中验证码/滑块或整站不可达立即停采，不做任何绕过。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from . import parser as P
from .config import Settings, Task
from .risk import (
    SIGNAL_CAPTCHA,
    SIGNAL_EMPTY_RATE,
    SIGNAL_IP_BLOCKED,
    SIGNAL_LOGIN_BOTH_INVALID,
    SIGNAL_LOGIN_KICKED,
    SIGNAL_OK,
    SIGNAL_PARSE_RATE_LOW,
    RiskMachine,
    RateLimiter,
    combine_signals,
    detect_dom_signals,
    detect_login_wall,
    detect_signals,
    evaluate_empty_response_rate,
    evaluate_parse_rate,
)
from .urlguard import assert_page_url, is_robots_disallowed

PAGE_TYPE_LIST = "list"
PAGE_TYPE_DETAIL = "detail"

# 详情页「正在浏览」候选模式（2026-10-03 真实 DOM 校准）：
# 实测详情页 DOM 中**没有**数值型浏览计数字段，只有实时动态流「**8054 正在浏览这个商品」
# （打码用户 ID + 行为）。旧模式 `(\d+)\s*人?\s*正在浏览` 会把打码 ID 当浏览数——
# 已收紧为必须带「人」字（喂流无「人」），另留「浏览人数」标签位；匹配不到就如实 NULL。
DETAIL_VIEWER_SELECTORS: tuple[str, ...] = (
    "[class*='viewer'], [class*='browsing'], [class*='look'], [class*='visit']",
)
DETAIL_VIEWER_PATTERNS: tuple[str, ...] = (
    r"(\d{1,6})\s*人\s*正在浏览",
    r"浏览人数\s*[:：]?\s*(\d{1,6})",
    r"(\d{1,6})\s*人在看",
)
# 收藏数候选模式（2026-10-03 校准）：真实锚点是「6人已收藏」（与列表卡 collectcount 一致）；
# 旧模式 `(\d+)\s*人?\s*收藏` 会把动态流时间戳（00:41:36 收藏了…→"36 收藏"）当收藏数——已移除。
DETAIL_FAVORITE_SELECTORS: tuple[str, ...] = (
    "[class*='collect'], [class*='favorite'], [class*='favourite'], [class*='want']",
)
DETAIL_FAVORITE_PATTERNS: tuple[str, ...] = (
    r"(\d{1,6})\s*人已收藏",
    r"(\d{1,6})\s*人\s*想要",
)
# 游客态打码标记（登录后可见 / 星号遮蔽）。
# 不收录「--」这类通用串：任何页面文本都可能出现，会把与打码无关的页误标为打码
# （终审发现项 #15）；打码判定宁可漏报也不误报。
MASK_MARKERS: tuple[str, ...] = (
    "登录后可见", "登录查看", "***", "＊＊＊", "****",
)

_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class CollectorError(RuntimeError):
    pass


def aborted_round(task: Task, *, settings: Settings, run_id: str,
                  decision, now=None, collected_via: str = "guest",
                  session_slot: str | None = None) -> ListRunResult:
    """构造「前置闸门拒绝」的空轮次结果（调用方保证：不启动浏览器、不发起任何请求）。"""
    clock = now or _dt.datetime.now
    started = clock()
    result = ListRunResult(
        task_id=task.task_id, run_id=run_id,
        collected_via=collected_via,
        session_slot=session_slot or decision.session_slot or "guest",
        frequency_factor=decision.frequency_factor, raw_dir=None,
        started_at=started, finished_at=started,
        pagination_strategy=settings.collect_opts.pagination.strategy)
    result.aborted_by_risk = True
    result.decision = decision.as_dict()
    result.risk_signal = decision.reason or decision.level      # 触发原因（captcha …）
    result.risk_level = decision.level                          # 状态机级别（task_paused …）
    result.risk_reason = (f"{decision.level}"
                          + (f"；退避至 {decision.backoff_until.isoformat(timespec='seconds')}"
                             if decision.backoff_until else "")
                          + f"；剩余 {decision.remaining_s:.0f}s")
    return result


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass
class RawDump:
    html_path: str | None
    meta_path: str | None
    page_type: str
    skipped_reason: str | None = None


@dataclass
class ListPageResult:
    page_no: int
    url: str
    status: int | None
    ok: bool
    cards_seen: int
    cards_parsed: int
    parse_success_rate: float
    empty: bool
    error: str | None
    signals: tuple[str, ...]
    raw: RawDump | None
    parse: P.PageParseResult | None = None
    elapsed_s: float = 0.0
    pagination_used: str = "url"        # url | click | url-fallback
    first_card_id: str | None = None    # 用于识别「翻页未生效」
    card_wait_selector: str | None = None   # 渲染等待命中的卡片选择器
    blank: bool = False                 # 取回但内容为空（空响应），与「自然末页」区分
    text_len: int = 0                   # 可见文本长度（判空与排障）


@dataclass
class DetailResult:
    listing_id: str
    url: str
    status: int | None
    ok: bool
    viewers_masked: int | None          # 「正在浏览」数值（游客态打码 → None）
    viewers_visible: bool               # 是否取到可见数值
    viewers_mask_detected: bool         # 是否检测到打码标记
    favorites_cnt: int | None
    favorites_visible: bool
    collected_via: str
    signals: tuple[str, ...]
    raw: RawDump | None
    error: str | None = None
    elapsed_s: float = 0.0


@dataclass
class ListRunResult:
    task_id: str
    run_id: str
    collected_via: str
    session_slot: str
    frequency_factor: float
    raw_dir: str | None
    pages: list[ListPageResult] = _dc_field(default_factory=list)
    aborted_by_risk: bool = False
    risk_signal: str | None = None      # 触发信号/reason（captcha / empty-response-rate / ip-blocked …）
    risk_level: str | None = None       # 状态机级别（task_paused / backoff / stopped / warn）
    risk_reason: str | None = None
    decision: dict[str, Any] | None = None
    rejection_count: int = 0            # 请求失败/HTTP 错误页数（「被拒」）
    blank_response_count: int = 0       # 取回但内容为空的页数（「空响应」）
    natural_end_count: int = 0          # HTTP 200 但列表为空 = 翻到自然末页（**不算**空响应）
    pages_attempted: int = 0            # 实际发起过导航的页数（空响应率分母）
    pagination_stalled: bool = False    # 翻页未生效（该页已丢弃，不计入指标）
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
    duration_s: float = 0.0
    guard: dict[str, Any] = _dc_field(default_factory=dict)
    pagination_strategy: str = "url"

    # -- 汇总指标（契约 summary 字段口径） ------------------------------ #
    @property
    def pages_collected(self) -> int:
        return sum(1 for p in self.pages if p.ok and not p.empty)

    @property
    def cards_seen(self) -> int:
        return sum(p.cards_seen for p in self.pages)

    @property
    def cards_parsed(self) -> int:
        return sum(p.cards_parsed for p in self.pages)

    @property
    def parse_success_rate(self) -> float:
        seen = self.cards_seen
        return (self.cards_parsed / seen) if seen else 0.0

    @property
    def empty_response_rate(self) -> float:
        """空响应率 = (请求被拒 + 内容为空) / 实际请求页数。

        口径（docs/01 §3.3「请求被拒/空响应率 >30%」）：**只有被拒或内容为空才算**；
        翻到自然末页（HTTP 200、列表本来就没有更多）与翻页未生效都不计入——
        否则正常轮次会被误判成风控触发。
        """
        total = self.pages_attempted or len(self.pages)
        if not total:
            return 0.0
        return (self.rejection_count + self.blank_response_count) / total

    @property
    def raw_dir_actual(self) -> str | None:
        """真实落盘目录：只有确实写出过 raw 文件时才返回路径，否则 None（不虚报目录）。"""
        for page in self.pages:
            if page.raw and page.raw.meta_path:
                return str(Path(page.raw.meta_path).parent)
        return None

    def summary_fields(self) -> dict[str, Any]:
        """可直接并入 summary.json 的字段（口径见契约 GateMetrics）。"""
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "collected_via": self.collected_via,
            "pages": self.pages_collected,
            "cards_seen": self.cards_seen,
            "cards_parsed": self.cards_parsed,
            "parse_success_rate": round(self.parse_success_rate, 4),
            "risk_trigger": self.risk_signal,
            "risk_level": self.risk_level,
            "risk_reason": self.risk_reason,
            "aborted_by_risk": self.aborted_by_risk,
            "empty_response_rate": round(self.empty_response_rate, 4),
            "rejection_count": self.rejection_count,
            "blank_response_count": self.blank_response_count,
            "natural_end_count": self.natural_end_count,
            "pages_attempted": self.pages_attempted or len(self.pages),
            "raw_dir": self.raw_dir_actual,
            "duration_s": round(self.duration_s, 1),
            "pagination_strategy": self.pagination_strategy,
        }


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def new_run_id(task_id: str, *, now: _dt.datetime | None = None,
               suffix: str | None = None) -> str:
    """run_id：{task}-{YYYYMMDDTHHMMSS}-{4 位后缀}（后缀用于同秒内区分）。"""
    now = now or _dt.datetime.now()
    tail = suffix or _dt.datetime.now().strftime("%f")[:4]
    return f"{task_id}-{now.strftime('%Y%m%dT%H%M%S')}-{tail}"


def listing_page_url(settings: Settings, task: Task, page_no: int) -> str:
    """按 settings.collect.pagination 生成第 page_no 页 URL（默认第 1 页不带参数）。"""
    base = settings.site.listing_url(task.game_id, task.biz_prod)
    pag = settings.collect_opts.pagination
    param = pag.first_page_param if (page_no == 1 and pag.first_page_param) else pag.page_param
    if page_no == 1 and not pag.first_page_param:
        return base
    parts = urlsplit(base)
    query = parse_qs(parts.query, keep_blank_values=True)
    query[param] = [str(page_no)]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                      urlencode(query, doseq=True), parts.fragment))
    return assert_page_url(url, allowed_hosts=settings.site.allowed_hosts, require_https=True)


def raw_dir_for(settings: Settings, task_id: str, run_id: str,
                *, now: _dt.datetime | None = None) -> Path:
    _check_component(task_id, "task_id")
    _check_component(run_id, "run_id")
    day = (now or _dt.datetime.now()).strftime("%Y%m%d")
    return Path(settings.paths.raw_root) / task_id / day / run_id


def _check_component(value: str, name: str) -> None:
    if not _SAFE_COMPONENT_RE.match(str(value or "")):
        raise CollectorError(f"{name} 含非法字符（仅字母数字._-，≤64）：{value!r}")


def _write_raw(out_dir: Path, stem: str, *, html: str | None, meta: Mapping[str, Any],
               dump_html: bool = True) -> RawDump:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / f"{stem}.json"
    meta_path.write_text(json.dumps(dict(meta), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    html_path = None
    if dump_html and html is not None:
        html_path = out_dir / f"{stem}.html"
        html_path.write_text(html, encoding="utf-8")
    return RawDump(html_path=str(html_path) if html_path else None,
                   meta_path=str(meta_path),
                   page_type=str(meta.get("page_type") or ""))


def _page_text(html: str, limit: int = 20000) -> str:
    """保留旧名以兼容调用方：改用 parser.visible_text（剔除 script/style，避免误报）。"""
    return P.visible_text(html, limit=limit)


def _is_login_session(session) -> bool:
    return str(getattr(session, "collected_via", "guest")) == "login"


def _collect_page_signals(html: str, *, logged_in: bool = False) -> tuple[str, ...]:
    """风控信号 = 可见文本标记 ∪ DOM 级 WAF 组件激活特征 ∪（登录态时）登录墙标记。"""
    text = _page_text(html)
    text_signals = detect_signals(text)
    dom_signals = detect_dom_signals(html)
    extra: tuple[str, ...] = ()
    if logged_in and detect_login_wall(text):
        extra = (SIGNAL_LOGIN_KICKED,)      # 登录态看到登录墙 ⇒ 该登录态已失效
    return combine_signals(text_signals, dom_signals, extra)


def _first_number(source: str, selectors: Sequence[str],
                  patterns: Sequence[str]) -> tuple[int | None, str | None]:
    """按 CSS 选择器 → 可见文本模式 两级取值，返回 (数值, 命中策略)。

    ``source`` 接受**原始 HTML**（两级都生效）或纯文本（只有文本层生效）。
    此前调用方传的是纯文本，CSS 层永远选不到节点、形同死代码（终审发现项 #12/#31）；
    现统一传 HTML：CSS 层先在 DOM 上找语义节点，文本层只对可见文本跑正则
    （避免命中 script/style 里的数字）。
    """
    soup = None
    for selector in selectors:
        try:
            soup = soup or BeautifulSoup(source, "html.parser")
            nodes = soup.select(selector)
        except Exception:
            continue
        for node in nodes:
            chunk = node.get_text(" ", strip=True)
            for pattern in patterns:
                m = re.search(pattern, chunk)
                if m:
                    return int(m.group(1)), f"css:{selector}|{pattern}"
    text = P.visible_text(source) if ("<" in source and ">" in source) else source
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return int(m.group(1)), f"text:{pattern}"
    return None, None


# --------------------------------------------------------------------------- #
# 列表页采集
# --------------------------------------------------------------------------- #
def collect_list(task: Task, *, settings: Settings, session, machine: RiskMachine | None = None,
                 limiter: RateLimiter | None = None, run_id: str,
                 pages: int | None = None, clock=None,
                 decision=None) -> ListRunResult:
    """采集一轮列表页（≤pages_per_run 页，受 settings 硬上限约束）。

    - 开始前先做风控前置检查（退避未到期 → 直接终止并在结果里写明原因）；
      调用方若已在更早处检查过（如 pipeline 在启动浏览器之前），可传入 decision 复用，
      避免状态机被推进两次；
    - 每页之间按 3–6s × 频率系数随机等待；
    - 页面 HTML 与元数据落 raw；统计空响应/被拒页数；
    - 命中验证码/整站不可达立即停采（不做任何绕过）。
    """
    now = clock or _dt.datetime.now
    started = now()
    limiter = limiter or RateLimiter(settings, machine=machine)

    if decision is None and machine is not None:
        decision = machine.check_before_run(task.task_id)
    if decision is not None and not decision.allow:
        result = aborted_round(task, settings=settings, run_id=run_id,
                               decision=decision, now=now)
        result.started_at = started
        result.duration_s = (now() - started).total_seconds()
        return result

    raw_dir = raw_dir_for(settings, task.task_id, run_id, now=started)
    result = ListRunResult(
        task_id=task.task_id,
        run_id=run_id,
        collected_via=getattr(session, "collected_via", "guest"),
        session_slot=getattr(session, "slot", "guest"),
        frequency_factor=getattr(session, "frequency_factor", 1.0),
        raw_dir=str(raw_dir),
        started_at=started,
        pagination_strategy=settings.collect_opts.pagination.strategy,
    )
    if decision is not None:
        result.decision = decision.as_dict()
        result.frequency_factor = decision.frequency_factor

    page_limit = limiter.max_pages(pages if pages is not None else task.pages_per_run)
    pagination = settings.collect_opts.pagination
    page = session.new_page()
    prev_first_id: str | None = None
    try:
        for page_no in range(1, page_limit + 1):
            if page_no > 1:
                limiter.wait_between_pages(page_no=page_no, factor=result.frequency_factor)

            strategy_used = "url"
            if page_no > 1 and pagination.strategy == "click":
                clicked = _click_next_button(page, pagination)
                if clicked:
                    strategy_used = "click"
                    page.wait_for_timeout(pagination.page_settle_ms)
                    nav = _inplace_nav(page)
                else:
                    strategy_used = "url-fallback"   # click 全部候选失败 → 显式回退并记录
                    nav = session.goto(page, listing_page_url(settings, task, page_no))
            else:
                nav = session.goto(page, listing_page_url(settings, task, page_no))

            page_result = _collect_one_list_page(
                page, task=task, settings=settings, session=session, nav=nav,
                page_no=page_no, run_id=run_id, raw_dir=raw_dir, now=now,
                pagination_used=strategy_used)
            result.pages_attempted += 1
            result.pages.append(page_result)

            if not page_result.ok:
                result.rejection_count += 1
                break                      # 请求失败不再继续翻页
            if page_result.blank:
                result.blank_response_count += 1
                break                      # 取回但内容为空：按「空响应」计入并停止翻页
            if SIGNAL_LOGIN_KICKED in page_result.signals:
                # 登录态看到登录墙 ⇒ 该登录态已失效：切备用（再失效→游客态），本轮到此为止，
                # 下一轮用新槽位（浏览器上下文里的 storage_state 无法中途替换）
                handle_login_invalid(machine, session, task_id=task.task_id,
                                     reason="登录态页面出现登录墙标记")
                result.aborted_by_risk = True
                result.risk_signal = SIGNAL_LOGIN_KICKED
                result.risk_level = getattr(machine.state, "level", None) if machine else None
                result.risk_reason = (f"登录态失效 → 已切至 {getattr(session, 'slot', 'guest')}"
                                      f"（collected_via={getattr(session, 'collected_via', 'guest')}）")
                break
            if SIGNAL_CAPTCHA in page_result.signals or SIGNAL_IP_BLOCKED in page_result.signals:
                # 风控信号优先于「空页」判定：验证码页面通常没有卡片，但语义是停采而非空响应
                _apply_risk_signals(machine, page_result.signals, page_result=page_result,
                                    task_id=task.task_id, settings=settings,
                                    limiter=limiter, session=session)
                result.aborted_by_risk = True
                result.risk_signal = (SIGNAL_IP_BLOCKED if SIGNAL_IP_BLOCKED in page_result.signals
                                      else SIGNAL_CAPTCHA)
                result.risk_reason = result.risk_signal
                break
            if page_result.empty:
                # HTTP 200 且列表为空 = 翻到自然末页（或该切片确无在售）：不是风控信号
                result.natural_end_count += 1
                break
            if page_no > 1 and page_result.first_card_id and \
                    page_result.first_card_id == prev_first_id:
                # 翻页未生效（页面内容与上一页相同）：丢弃该页，避免同一批卡片重复计入指标；
                # 计数上既不算「被拒」也不算「空响应」，另记 pagination_stalled
                page_result.error = "pagination-stalled"
                result.pages.pop()
                result.pagination_stalled = True
                break
            prev_first_id = page_result.first_card_id or prev_first_id
    finally:
        try:
            page.close()
        except Exception:
            pass

    # 空响应率越线 → 当轮终止 + 退避 6h（只统计「被拒 + 内容为空」，自然末页不计）
    if machine is not None and not result.aborted_by_risk and result.pages_attempted:
        over, rate = evaluate_empty_response_rate(
            result.rejection_count + result.blank_response_count,
            result.pages_attempted,
            settings.risk_control.empty_response_ratio)
        if over:
            machine.on_signal(SIGNAL_EMPTY_RATE, detail=f"{rate:.2%}",
                              task_id=task.task_id)
            result.aborted_by_risk = True
            result.risk_signal = SIGNAL_EMPTY_RATE
            result.risk_reason = f"empty-response-rate={rate:.2%}"

    # 解析成功率 <80% → 继续但只入 raw 层（不停止）
    if machine is not None and result.pages and not result.aborted_by_risk:
        low, rate = evaluate_parse_rate(result.cards_parsed, result.cards_seen,
                                        settings.risk_control.parse_success_ratio)
        if low:
            machine.on_signal(SIGNAL_PARSE_RATE_LOW, detail=f"{rate:.2%}", task_id=task.task_id)
            result.risk_signal = SIGNAL_PARSE_RATE_LOW
            result.risk_reason = f"parse-rate-low={rate:.2%}"
        else:
            machine.on_signal(SIGNAL_OK, task_id=task.task_id)

    result.finished_at = now()
    result.duration_s = (result.finished_at - started).total_seconds()
    result.guard = _guard_snapshot(session)
    if limiter is not None:
        limiter.note_task_finished()
    return result


def _wait_for_cards_rendered(page, settings: Settings, session) -> str | None:
    """等 SPA 渲染完成：先 networkidle（超时即继续），再按候选选择器轮询卡片。

    返回命中的选择器；全部未命中返回 None（由解析结果判断是否空页/改版）。
    只等待，不触碰任何接口，不做任何绕过动作。
    """
    try:
        page.wait_for_load_state("networkidle",
                                 timeout=settings.collect_opts.navigation_timeout_ms)
    except Exception:
        pass                                    # 长连接/轮询站点可能永不 idle，继续即可
    per_selector_ms = max(2000, settings.collect_opts.selector_timeout_ms // 5)
    try:
        return session.wait_for_cards(page, P.CARD_SELECTORS, timeout_ms=per_selector_ms)
    except Exception:
        return None


def _inplace_nav(page) -> "NavigationResult":
    """点击翻页后没有整页导航：用当前 URL 构造一个『页面内更新』结果。"""
    from .browser import NavigationResult
    try:
        current = page.url
    except Exception:
        current = ""
    return NavigationResult(url=current, status=None, ok=True, elapsed_s=0.0)


def _click_next_button(page, pagination) -> str | None:
    """click 翻页策略：按候选选择器逐个尝试点『下一页』，返回命中的选择器。"""
    for selector in pagination.next_button_selectors:
        try:
            page.click(selector, timeout=2500)
            return selector
        except Exception:
            continue
    return None


def _collect_one_list_page(page, *, task: Task, settings: Settings, session, nav,
                           page_no: int, run_id: str, raw_dir: Path, now,
                           pagination_used: str = "url") -> ListPageResult:
    url = nav.url
    signals: tuple[str, ...] = ()
    html = ""
    card_wait_selector: str | None = None
    if nav.ok:
        # SPA（Nuxt）列表卡片是客户端渲染：等网络空闲 + 轮询卡片选择器，再取 HTML
        card_wait_selector = _wait_for_cards_rendered(page, settings, session)
        try:
            html = page.content()
        except Exception as exc:
            nav = type(nav)(url=url, status=nav.status, ok=False, elapsed_s=nav.elapsed_s,
                            error=f"content() 失败：{exc}")
    parse_result = None
    cards_seen = cards_parsed = 0
    rate = 0.0
    empty = False
    first_card_id = None
    text_len = 0
    if nav.ok:
        parse_result = P.parse_list_page(html, url=url, parser_version=settings.parser_version)
        cards_seen, cards_parsed = parse_result.cards_seen, parse_result.cards_parsed
        rate = parse_result.parse_success_rate
        empty = cards_seen == 0
        first_card_id = next((c.listing_id for c in parse_result.cards if c.listing_id), None)
        text = _page_text(html)
        text_len = len(text)
        signals = _collect_page_signals(html, logged_in=_is_login_session(session))
    # 「空响应」= 取回但**没有任何可见文本**（服务端返回空壳/被截断）；
    # 有可见文本但列表为空的页面属「自然末页」，不算空响应（docs/01 §3.3）
    blank = bool(nav.ok) and text_len == 0

    # robots 禁抓路径判定（settings.site.respect_robots 实际消费该配置；
    # 关闭时会在配置加载期产生显式告警，不建议关闭）
    robots_hit = is_robots_disallowed(url) if settings.site.respect_robots else False
    meta = {
        "url": url,
        "page_type": PAGE_TYPE_LIST,
        "page_no": page_no,
        "task_id": task.task_id,
        "run_id": run_id,
        "collected_at": now().isoformat(timespec="seconds"),
        "http_status": nav.status,
        "ok": nav.ok,
        "error": nav.error,
        "collected_via": getattr(session, "collected_via", "guest"),
        "session_slot": getattr(session, "slot", "guest"),
        "storage_state": getattr(session, "storage_state_path", None),
        "parser_version": P.PARSER_VERSION,
        "cards_seen": cards_seen,
        "cards_parsed": cards_parsed,
        "first_card_id": first_card_id,
        "card_wait_selector": card_wait_selector,
        "card_selector_used": (parse_result.card_selector_used if parse_result else None),
        "risk_signals": list(signals),
        "guard": _guard_snapshot(session),
        "robots_disallowed_path": robots_hit,
        "pagination_strategy": settings.collect_opts.pagination.strategy,
        "pagination_used": pagination_used,
    }
    dump: RawDump | None = None
    if robots_hit:
        # robots 禁抓路径：不作为页面采集对象 → 只记元数据，不落 HTML、不解析
        dump = _write_raw(raw_dir, f"list_p{page_no:02d}", html=None, meta=meta,
                          dump_html=False)
        dump.skipped_reason = "robots-disallowed"
    elif settings.collect_opts.dump_html:
        dump = _write_raw(raw_dir, f"list_p{page_no:02d}", html=html, meta=meta, dump_html=True)
    else:
        dump = _write_raw(raw_dir, f"list_p{page_no:02d}", html=None, meta=meta, dump_html=False)

    return ListPageResult(
        page_no=page_no, url=url, status=nav.status, ok=nav.ok,
        cards_seen=cards_seen, cards_parsed=cards_parsed, parse_success_rate=rate,
        empty=empty, error=nav.error, signals=signals, raw=dump, parse=parse_result,
        elapsed_s=nav.elapsed_s, pagination_used=pagination_used, first_card_id=first_card_id,
        card_wait_selector=card_wait_selector, blank=blank, text_len=text_len,
    )


# --------------------------------------------------------------------------- #
# 详情页采集
# --------------------------------------------------------------------------- #
def collect_detail(listing_id: str, *, settings: Settings, session, task_id: str, run_id: str,
                   raw_dir: Path | None = None, now=None,
                   store_raw: bool | None = None) -> DetailResult:
    """采集详情页需求侧字段（M5）：正在浏览 + 收藏数。

    游客态被打码时：viewers_masked=None、viewers_mask_detected=True、collected_via=guest（如实标注）。
    """
    now = now or _dt.datetime.now
    _check_component(str(listing_id), "listing_id")
    url = settings.site.detail_url(listing_id)
    started = now()
    page = session.new_page()
    store = settings.collect_opts.detail_store_raw if store_raw is None else store_raw
    try:
        nav = session.goto(page, url)
        html = ""
        if nav.ok:
            try:
                page.wait_for_load_state("networkidle",
                                         timeout=settings.collect_opts.navigation_timeout_ms)
            except Exception:
                pass
            try:
                html = page.content()
            except Exception as exc:
                nav = type(nav)(url=url, status=nav.status, ok=False, elapsed_s=nav.elapsed_s,
                                error=f"content() 失败：{exc}")
        viewers = favorites = None
        via_viewer = via_fav = None
        mask_detected = False
        signals: tuple[str, ...] = ()
        if nav.ok:
            text = _page_text(html, limit=50000)
            signals = _collect_page_signals(html, logged_in=_is_login_session(session))
            # 传原始 HTML：CSS 层（语义类名）+ 可见文本层两级都要能生效
            viewers, via_viewer = _first_number(html, DETAIL_VIEWER_SELECTORS,
                                                DETAIL_VIEWER_PATTERNS)
            favorites, via_fav = _first_number(html, DETAIL_FAVORITE_SELECTORS,
                                               DETAIL_FAVORITE_PATTERNS)
            mask_detected = any(marker in text for marker in MASK_MARKERS) and viewers is None

        out_dir = raw_dir or raw_dir_for(settings, task_id, run_id, now=started)
        meta = {
            "url": url,
            "page_type": PAGE_TYPE_DETAIL,
            "listing_id": str(listing_id),
            "task_id": task_id,
            "run_id": run_id,
            "collected_at": now().isoformat(timespec="seconds"),
            "http_status": nav.status,
            "ok": nav.ok,
            "error": nav.error,
            "collected_via": getattr(session, "collected_via", "guest"),
            "session_slot": getattr(session, "slot", "guest"),
            "parser_version": P.PARSER_VERSION,
            "viewers_masked": viewers,
            "viewers_visible": viewers is not None,
            "viewers_mask_detected": mask_detected,
            "viewers_strategy": via_viewer,
            "favorites_cnt": favorites,
            "favorites_visible": favorites is not None,
            "favorites_strategy": via_fav,
            "risk_signals": list(signals),
            "guard": _guard_snapshot(session),
        }
        dump = _write_raw(out_dir, f"detail_{listing_id}", html=html if store else None,
                          meta=meta, dump_html=store)
        return DetailResult(
            listing_id=str(listing_id), url=url, status=nav.status, ok=nav.ok,
            viewers_masked=viewers, viewers_visible=viewers is not None,
            viewers_mask_detected=mask_detected, favorites_cnt=favorites,
            favorites_visible=favorites is not None,
            collected_via=getattr(session, "collected_via", "guest"),
            signals=signals, raw=dump, error=nav.error,
            elapsed_s=(now() - started).total_seconds())
    finally:
        try:
            page.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 风控联动
# --------------------------------------------------------------------------- #
def _apply_risk_signals(machine: RiskMachine | None, signals: Iterable[str], *,
                        page_result: ListPageResult | None = None, task_id: str,
                        settings: Settings, limiter: RateLimiter | None = None,
                        session=None) -> list[str]:
    """把页面信号喂给状态机；登录态失效时按 §3.3 逐级降级。返回已处理的信号。"""
    if machine is None:
        return []
    handled: list[str] = []
    detail = f"url={page_result.url}" if page_result else None
    for signal in signals:
        if signal == SIGNAL_IP_BLOCKED:
            machine.on_signal(SIGNAL_IP_BLOCKED, detail=detail, task_id=task_id)
            handled.append(signal)
        elif signal == SIGNAL_CAPTCHA:
            machine.on_signal(SIGNAL_CAPTCHA, detail=detail, task_id=task_id)
            handled.append(signal)
    return handled


def handle_login_invalid(machine: RiskMachine | None, session, *,
                         task_id: str, reason: str = "") -> str:
    """登录态失效处理：主 → 备 → 游客（72h 观察），返回新的 collected_via。"""
    if session is not None:
        if getattr(session, "slot", None) == "primary":
            session.downgrade_to_backup(reason or "主登录态被风控/踢出")
        else:
            session.downgrade_to_guest(reason or "备用登录态亦失效")
    if machine is not None:
        if getattr(session, "slot", None) == "backup":
            machine.on_signal(SIGNAL_LOGIN_KICKED, detail=reason, task_id=task_id)
        else:
            machine.on_signal(SIGNAL_LOGIN_BOTH_INVALID, detail=reason, task_id=task_id)
    return getattr(session, "collected_via", "guest")


def _guard_snapshot(session) -> dict[str, Any]:
    guard = getattr(session, "guard", None)
    if guard is None:
        return {}
    return {
        "requests_seen": guard.requests_seen,
        "requests_allowed": guard.requests_allowed,
        "requests_blocked": guard.requests_blocked,
        "robots_skipped": guard.robots_skipped,
        "block_reasons": dict(guard.block_reasons),
    }
