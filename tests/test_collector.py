"""采集器离线测试：用替身 page/session 驱动，验证 raw 落盘、统计与风控联动（不访问真实站点）。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import collector as C  # noqa: E402
from pxb7 import config as cfg  # noqa: E402
from pxb7 import risk as R  # noqa: E402
from pxb7.browser import GuardStats, NavigationResult  # noqa: E402

CARD = """
<li class="product-card" data-listing-id="100000000%d">
  <div class="card-price">¥ %d</div>
  <div class="card-time">21分钟内发布</div>
  <ul>
    <li class="attr-level">等级 60</li>
    <li class="attr-yellow">黄数 45</li>
    <li class="attr-star-char">五星角色 12</li>
    <li class="attr-star-weapon">五星武器 5</li>
    <li class="attr-server">官服</li>
    <li class="attr-mail">邮箱出售</li>
  </ul>
  <div class="featured-chars"><span>钟离</span></div>
</li>
"""


def list_html(n_cards: int, *, base_id: int = 1, price: int = 1200) -> str:
    cards = "".join(CARD % (base_id + i, price + i) for i in range(n_cards))
    return f"<html><body><ul class='product-list'>{cards}</ul></body></html>"


EMPTY_HTML = "<html><body><div class='empty'>暂无相关账号</div></body></html>"


class StubPage:
    def __init__(self) -> None:
        self._html = ""
        self.closed = False

    def content(self) -> str:
        return self._html

    def close(self) -> None:
        self.closed = True


class StubSession:
    """替身会话：同一个 page 对象按导航顺序依次取预置 HTML（与真实翻页一致）。"""

    def __init__(self, pages_html: list[str], *, slot: str = "guest") -> None:
        self.slot = slot
        self.collected_via = "guest" if slot == "guest" else "login"
        self.frequency_factor = 4.0 if slot == "guest" else 1.0
        self.storage_state_path = None
        self.guard = GuardStats()
        self._htmls = list(pages_html)
        self.navigated: list[str] = []
        self.pages_opened: list[StubPage] = []

    def new_page(self) -> StubPage:
        page = StubPage()
        self.pages_opened.append(page)
        return page

    def goto(self, page, url: str, **_kwargs) -> NavigationResult:
        self.navigated.append(url)
        page._html = self._htmls.pop(0) if self._htmls else EMPTY_HTML
        return NavigationResult(url=url, status=200, ok=True, elapsed_s=0.01,
                                body_text_len=len(page._html))


class FixedRng:
    def uniform(self, lo: float, hi: float) -> float:
        return (lo + hi) / 2


@pytest.fixture()
def settings(tmp_path: Path) -> cfg.Settings:
    base = cfg.load_settings()
    return dataclasses.replace(base,
                               paths=dataclasses.replace(base.paths, raw_root=tmp_path / "raw"))


@pytest.fixture()
def task(settings: cfg.Settings) -> cfg.Task:
    return cfg.load_tasks(settings=settings).by_id("genshin_official")


def _machine(settings: cfg.Settings, tmp_path: Path, clock=None) -> R.RiskMachine:
    return R.RiskMachine(settings, state_path=tmp_path / "risk.json", now=clock)


def _limiter(settings: cfg.Settings, machine=None) -> tuple[R.RateLimiter, list[float]]:
    slept: list[float] = []
    limiter = R.RateLimiter(settings, machine=machine, sleeper=slept.append, rng=FixedRng())
    return limiter, slept


# --------------------------------------------------------------------------- #
# 列表页采集
# --------------------------------------------------------------------------- #
def test_collect_list_writes_raw_and_stats(settings: cfg.Settings, task: cfg.Task,
                                           tmp_path: Path) -> None:
    machine = _machine(settings, tmp_path)
    limiter, slept = _limiter(settings, machine)
    session = StubSession([list_html(3), list_html(2, base_id=10, price=2000)])
    run_id = C.new_run_id(task.task_id, now=dt.datetime(2026, 10, 2, 10, 0, 0), suffix="test")

    res = C.collect_list(task, settings=settings, session=session, machine=machine,
                         limiter=limiter, run_id=run_id, pages=2,
                         clock=lambda: dt.datetime(2026, 10, 2, 10, 0, 0))

    assert res.pages_collected == 2
    assert res.cards_seen == 5 and res.cards_parsed == 5
    assert res.rejection_count == 0 and res.natural_end_count == 0
    assert res.empty_response_rate == 0.0
    assert res.aborted_by_risk is False
    assert res.collected_via == "guest", "没有登录态文件时必须标注 guest"
    assert session.navigated == [
        "https://www.pxb7.com/buy/10026/1",
        "https://www.pxb7.com/buy/10026/1?page=2",
    ]
    assert len(slept) == 1 and 3.0 <= slept[0] <= 6.0 * session.frequency_factor, \
        "每页之间按 3–6s 随机 × 频率系数等待"
    assert machine.state.level == R.LEVEL_NORMAL, "正常轮次应回到常规档"

    raw_dir = Path(res.raw_dir)
    assert raw_dir == Path(settings.paths.raw_root) / "genshin_official" / "20261002" / run_id
    for name in ("list_p01.html", "list_p01.json", "list_p02.html", "list_p02.json"):
        assert (raw_dir / name).is_file(), f"raw 应落盘 {name}"

    meta = json.loads((raw_dir / "list_p01.json").read_text(encoding="utf-8"))
    for key in ("url", "page_type", "collected_at", "http_status", "collected_via",
                "parser_version", "cards_seen"):
        assert key in meta, f"raw 元数据缺 {key}"
    assert meta["page_type"] == "list"
    assert meta["http_status"] == 200
    assert meta["url"] == "https://www.pxb7.com/buy/10026/1"
    assert meta["cards_seen"] == 3
    assert meta["collected_via"] == "guest"

    fields = res.summary_fields()
    assert fields["cards_seen"] == 5 and fields["pages"] == 2
    assert fields["risk_trigger"] is None
    assert fields["parse_success_rate"] == pytest.approx(1.0)


def test_natural_last_page_is_not_an_empty_response(settings: cfg.Settings, task: cfg.Task,
                                                    tmp_path: Path) -> None:
    """【回归】翻到自然末页（HTTP 200、列表本来就没有更多）不得判成空响应风控触发。

    旧实现把自然末页计入空响应率 → 正常轮次被误判为 >30% 并退避 6h（评审发现的缺陷）。
    """
    clock_holder = [dt.datetime(2026, 10, 2, 10, 0, 0)]
    machine = _machine(settings, tmp_path, clock=lambda: clock_holder[0])
    limiter, _ = _limiter(settings, machine)
    session = StubSession([list_html(2), EMPTY_HTML])       # 第 2 页是末页

    res = C.collect_list(task, settings=settings, session=session, machine=machine,
                         limiter=limiter, run_id="r-end", pages=2,
                         clock=lambda: clock_holder[0])

    assert res.natural_end_count == 1
    assert res.rejection_count == 0 and res.blank_response_count == 0
    assert res.empty_response_rate == 0.0, "自然末页不计入空响应率"
    assert res.aborted_by_risk is False
    assert machine.state.level == R.LEVEL_NORMAL, "正常轮次不得被推进到退避档"
    assert machine.state.backoff_until is None
    assert res.cards_seen == 2
    # 末页 raw 仍落盘（排障）
    assert (Path(res.raw_dir) / "list_p02.html").is_file()


def test_blank_response_triggers_empty_rate_backoff(settings: cfg.Settings, task: cfg.Task,
                                                    tmp_path: Path) -> None:
    """真正的内容为空（取回但几乎无内容）才计入空响应率并退避 6h。"""
    clock_holder = [dt.datetime(2026, 10, 2, 10, 0, 0)]
    machine = _machine(settings, tmp_path, clock=lambda: clock_holder[0])
    limiter, _ = _limiter(settings, machine)
    session = StubSession(["<html><body></body></html>"])

    res = C.collect_list(task, settings=settings, session=session, machine=machine,
                         limiter=limiter, run_id="r-blank", pages=1,
                         clock=lambda: clock_holder[0])

    assert res.blank_response_count == 1
    assert res.empty_response_rate == 1.0
    assert res.aborted_by_risk is True
    assert res.risk_signal == R.SIGNAL_EMPTY_RATE
    assert machine.state.level == R.LEVEL_BACKOFF
    assert machine.state.backoff_until == clock_holder[0] + dt.timedelta(hours=6)


def test_collect_list_stops_on_captcha_signal(settings: cfg.Settings, task: cfg.Task,
                                             tmp_path: Path) -> None:
    clock_holder = [dt.datetime(2026, 10, 2, 10, 0, 0)]
    machine = _machine(settings, tmp_path, clock=lambda: clock_holder[0])
    limiter, _ = _limiter(settings, machine)
    captcha_html = "<html><body><div>请拖动滑块完成安全验证</div></body></html>"
    session = StubSession([captcha_html, list_html(3)])

    res = C.collect_list(task, settings=settings, session=session, machine=machine,
                         limiter=limiter, run_id="r-captcha", pages=5,
                         clock=lambda: clock_holder[0])

    assert res.aborted_by_risk is True
    assert res.risk_signal == R.SIGNAL_CAPTCHA
    assert machine.state.level == R.LEVEL_TASK_PAUSED
    assert machine.state.backoff_until == clock_holder[0] + dt.timedelta(hours=24)
    assert len(session.navigated) == 1, "命中验证码后必须立即停采，不再翻页"


def test_collect_list_respects_max_pages_cap(settings: cfg.Settings, task: cfg.Task,
                                             tmp_path: Path) -> None:
    machine = _machine(settings, tmp_path)
    limiter, _ = _limiter(settings, machine)
    # 每页首卡 id 不同，避免触发「翻页未生效」保护
    session = StubSession([list_html(1, base_id=100 * (i + 1)) for i in range(10)])
    res = C.collect_list(task, settings=settings, session=session, machine=machine,
                         limiter=limiter, run_id="r-cap", pages=99)
    assert len(session.navigated) == settings.rate_limit.max_pages_per_run == 5
    assert res.pages_collected == 5


def test_collect_list_stops_when_pagination_stalls(settings: cfg.Settings, task: cfg.Task,
                                                   tmp_path: Path) -> None:
    """翻页未生效（首卡 id 不变）时必须停止，避免同一批卡片被重复计入。"""
    machine = _machine(settings, tmp_path)
    limiter, _ = _limiter(settings, machine)
    same_page = list_html(3, base_id=1)
    session = StubSession([same_page, same_page, same_page])
    res = C.collect_list(task, settings=settings, session=session, machine=machine,
                         limiter=limiter, run_id="r-stall", pages=5)
    assert len(session.navigated) == 2, "第 2 页内容未变即停止"
    assert res.pagination_stalled is True
    assert len(res.pages) == 1, "重复内容的页不应计入本轮结果"
    assert res.cards_seen == 3, "同一批卡片不得重复计入"


# --------------------------------------------------------------------------- #
# 详情页采集
# --------------------------------------------------------------------------- #
def test_collect_detail_reads_viewers_and_favorites(settings: cfg.Settings, tmp_path: Path) -> None:
    html = ("<html><body><div class='viewer-count'>1234人正在浏览</div>"
            "<div class='collect-count'>56人已收藏</div></body></html>")
    session = StubSession([html])
    res = C.collect_detail("1000000001", settings=settings, session=session,
                           task_id="genshin_official", run_id="r-detail",
                           now=lambda: dt.datetime(2026, 10, 2, 10, 0, 0))
    assert res.ok is True
    assert res.viewers_masked == 1234 and res.viewers_visible is True
    assert res.favorites_cnt == 56 and res.favorites_visible is True
    assert res.url == "https://www.pxb7.com/product/1000000001/1"
    raw_dir = Path(settings.paths.raw_root) / "genshin_official" / "20261002" / "r-detail"
    meta = json.loads((raw_dir / "detail_1000000001.json").read_text(encoding="utf-8"))
    assert meta["page_type"] == "detail"
    assert meta["viewers_masked"] == 1234
    assert meta["favorites_cnt"] == 56
    assert meta["collected_via"] == "guest"


def test_collect_detail_reports_masking_honestly(settings: cfg.Settings, tmp_path: Path) -> None:
    html = "<html><body><div class='viewer-count'>登录后可见</div></body></html>"
    session = StubSession([html])
    res = C.collect_detail("1000000002", settings=settings, session=session,
                           task_id="genshin_official", run_id="r-masked",
                           now=lambda: dt.datetime(2026, 10, 2, 11, 0, 0))
    assert res.viewers_masked is None, "打码时不得填 0/猜测值"
    assert res.viewers_visible is False
    assert res.viewers_mask_detected is True
    assert res.collected_via == "guest"
    assert res.favorites_cnt is None


def test_detail_rejects_unsafe_listing_id(settings: cfg.Settings, tmp_path: Path) -> None:
    session = StubSession([EMPTY_HTML])
    with pytest.raises(C.CollectorError):
        C.collect_detail("../../etc/passwd", settings=settings, session=session,
                         task_id="genshin_official", run_id="r1")


# --------------------------------------------------------------------------- #
# 终审发现项回归（#12 CSS 死代码 / #15 打码标记误报 / #8 详情页登录降级链）
# --------------------------------------------------------------------------- #
def test_detail_first_number_css_layer_works_on_html(settings: cfg.Settings) -> None:
    """【回归】_first_number 的 CSS 层必须真的生效（终审发现项 #12：传纯文本导致死代码）。"""
    html = ("<html><body><div class='viewer-count'>1234人正在浏览</div>"
            "<p>本页共有 9999 个字</p></body></html>")
    value, strategy = C._first_number(html, C.DETAIL_VIEWER_SELECTORS, C.DETAIL_VIEWER_PATTERNS)
    assert value == 1234
    assert strategy is not None and strategy.startswith("css:"), f"CSS 层未生效：{strategy}"


def test_mask_marker_dash_no_longer_false_positives(settings: cfg.Settings) -> None:
    """【回归】「--」不是打码标记（终审发现项 #15）：普通文本不得误标 viewers_mask_detected。"""
    html = "<html><body><div>价格区间 -- 100 到 200 元</div></body></html>"
    session = StubSession([html])
    res = C.collect_detail("1000000003", settings=settings, session=session,
                           task_id="genshin_official", run_id="r-dash",
                           now=lambda: dt.datetime(2026, 10, 2, 12, 0, 0))
    assert res.viewers_mask_detected is False
    assert res.viewers_masked is None


def test_detail_login_kicked_wired_to_downgrade_chain(settings: cfg.Settings,
                                                      tmp_path: Path) -> None:
    """【回归】详情页识别出登录墙时必须能走 主→备→游客 降级链（终审发现项 #8 的联动基础）。"""
    from pxb7.browser import BrowserSession

    base = cfg.load_settings()
    primary = tmp_path / "storage_primary.json"
    backup = tmp_path / "storage_backup.json"
    usable = ('{"cookies": [{"name": "s", "value": "v", "domain": "www.pxb7.com",'
              ' "path": "/"}], "origins": []}')
    primary.write_text(usable, encoding="utf-8")
    backup.write_text(usable, encoding="utf-8")
    base = dataclasses.replace(base, paths=dataclasses.replace(
        base.paths, raw_root=tmp_path / "raw", storage_primary=primary,
        storage_backup=backup, risk_state=tmp_path / "risk.json"))
    machine = _machine(base, tmp_path)
    session = BrowserSession(base)          # primary 登录态
    assert session.collected_via == "login"

    via = C.handle_login_invalid(machine, session, task_id="genshin_official",
                                 reason="详情页出现登录墙标记")
    assert session.slot == "backup" and via == "login"
    assert machine.state.session_slot == R.SLOT_BACKUP
    via2 = C.handle_login_invalid(machine, session, task_id="genshin_official",
                                  reason="备用亦失效")
    assert session.slot == "guest" and via2 == "guest"
    assert machine.state.observe_until is not None
