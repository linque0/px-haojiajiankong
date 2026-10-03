"""风控状态机 / 限速器 / 采集前置闸门的离线测试（不访问真实站点）。"""

from __future__ import annotations

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


class Clock:
    """可推进的假时钟。"""

    def __init__(self, start: dt.datetime | None = None):
        self.t = start or dt.datetime(2026, 10, 2, 10, 0, 0)

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kwargs) -> dt.datetime:
        self.t = self.t + dt.timedelta(**kwargs)
        return self.t


class FixedRng:
    """确定性抖动替身：固定取区间中点（测试不引入随机源，断言可复现）。"""

    def uniform(self, lo: float, hi: float) -> float:
        return (lo + hi) / 2


@pytest.fixture()
def settings() -> cfg.Settings:
    return cfg.load_settings()


@pytest.fixture()
def machine(settings: cfg.Settings, tmp_path: Path) -> R.RiskMachine:
    return R.RiskMachine(settings, state_path=tmp_path / "risk_state.json", now=Clock())


def _load_state_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 状态机流转
# --------------------------------------------------------------------------- #
def test_initial_state_allows_run(machine: R.RiskMachine) -> None:
    d = machine.check_before_run("genshin_official")
    assert d.allow is True
    assert d.level == R.LEVEL_NORMAL
    assert d.frequency_factor == 1.0
    assert d.probe_mode is False


def test_captcha_pauses_task_24h_then_probes_at_quarter_frequency(
        machine: R.RiskMachine, settings: cfg.Settings) -> None:
    clock = machine._now
    machine.on_signal(R.SIGNAL_CAPTCHA, detail="滑块出现", task_id="genshin_official")

    entry = machine.state.task_state("genshin_official")
    assert entry.level == R.LEVEL_TASK_PAUSED
    assert entry.consecutive == 1
    assert entry.reason == "captcha"
    assert entry.backoff_until == clock() + dt.timedelta(hours=24)
    assert entry.frequency_factor == settings.rate_limit.probe_factor == 4.0, \
        "验证码后试探档 = 1/4（docs/01 §3.3）"

    # 退避未到期 → 直接终止
    d = machine.check_before_run("genshin_official")
    assert d.allow is False
    assert d.remaining_s == pytest.approx(24 * 3600)
    assert d.reason == "captcha"

    # 24h 后 → 1/4 频率试探
    clock.advance(hours=24, seconds=1)
    d2 = machine.check_before_run("genshin_official")
    assert d2.allow is True
    assert d2.probe_mode is True
    assert d2.frequency_factor == settings.rate_limit.probe_factor
    assert d2.level == R.LEVEL_WARN


def test_captcha_pause_is_per_task(machine: R.RiskMachine) -> None:
    """一个任务命中验证码不应让其它任务跟着停采（docs/01 §3.3「立即停采该任务」）。"""
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id="genshin_official")
    blocked = machine.check_before_run("genshin_official")
    other = machine.check_before_run("other_task")
    assert blocked.allow is False
    assert other.allow is True, "任务级停采不得外溢到其它任务"
    assert other.level == R.LEVEL_NORMAL

    machine.on_signal(R.SIGNAL_IP_BLOCKED, task_id="genshin_official")
    assert machine.check_before_run("other_task").allow is False, "IP 封禁是全局的"


def test_captcha_twice_stops_and_alerts(machine: R.RiskMachine) -> None:
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id="genshin_official")
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id="genshin_official")
    assert machine.state.consecutive == 2
    assert machine.state.level == R.LEVEL_STOPPED
    assert machine.state.reason == "captcha-consecutive"

    alerts = machine.drain_alerts()
    assert any(a["kind"] == "captcha-stop" for a in alerts), "连续 2 次应停采并告警"

    machine._now.advance(days=10)
    d = machine.check_before_run("genshin_official")
    assert d.allow is False, "停采状态不因时间流逝自动恢复（需人工介入）"


def test_task_stop_does_not_leak_to_other_tasks(machine: R.RiskMachine) -> None:
    """【回归】任务级 captcha-consecutive 停采不得经顶层镜像外溢（2026-10-03 试运行发现：
    顶层 level 是「最近操作任务」的镜像，只有 global_stop（IP 封禁/状态文件不可读）才拦所有任务）。"""
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id="genshin_official")
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id="genshin_official")   # 连续 2 次 → 任务停采
    assert machine.state.level == R.LEVEL_STOPPED, "顶层镜像反映最近任务档位（展示语义）"
    assert machine.state.global_stop is False, "任务级停采不是全局停采"
    assert machine.check_before_run("genshin_official").allow is False, "本任务停采"
    assert machine.check_before_run("other_task").allow is True, "其它任务不受影响"


def test_empty_response_rate_over_threshold_backs_off_6h(machine: R.RiskMachine) -> None:
    over, rate = R.evaluate_empty_response_rate(4, 10, 0.30)
    assert over is True and rate == pytest.approx(0.4)
    over_eq, rate_eq = R.evaluate_empty_response_rate(3, 10, 0.30)
    assert over_eq is False, "恰为 30% 不越线（阈值口径为 >30%）"

    clock = machine._now
    machine.on_signal(R.SIGNAL_EMPTY_RATE, detail="40.00%", task_id="genshin_official")
    assert machine.state.level == R.LEVEL_BACKOFF
    assert machine.state.backoff_until == clock() + dt.timedelta(hours=6)

    d = machine.check_before_run("genshin_official")
    assert d.allow is False and d.remaining_s == pytest.approx(6 * 3600)

    clock.advance(hours=6, seconds=1)
    d2 = machine.check_before_run("genshin_official")
    assert d2.allow is True
    assert d2.probe_mode is False, "空响应退避到期后恢复常规档，不走 1/4 试探"
    assert d2.frequency_factor == 1.0


def test_ip_blocked_stops_for_manual_intervention(machine: R.RiskMachine) -> None:
    machine.on_signal(R.SIGNAL_IP_BLOCKED, detail="整站不可达", task_id="genshin_official")
    assert machine.state.level == R.LEVEL_STOPPED
    assert machine.state.reason == "ip-blocked"
    assert machine.check_before_run("genshin_official").allow is False
    assert any(a["kind"] == "ip-blocked" for a in machine.drain_alerts())


def test_login_kicked_switches_to_backup_half_frequency(
        machine: R.RiskMachine, settings: cfg.Settings) -> None:
    machine.on_signal(R.SIGNAL_LOGIN_KICKED, detail="登录态失效", task_id="genshin_official")
    assert machine.state.session_slot == R.SLOT_BACKUP
    assert machine.state.frequency_factor == settings.rate_limit.backup_state_factor  # 1/2
    assert machine.state.level in (R.LEVEL_NORMAL, R.LEVEL_WARN)
    assert machine.state.backoff_until is None, "切备用不产生退避，只降频"


def test_both_sessions_invalid_degrades_to_guest_with_72h_observation(
        machine: R.RiskMachine, settings: cfg.Settings) -> None:
    machine.on_signal(R.SIGNAL_LOGIN_KICKED, task_id="genshin_official")
    machine.on_signal(R.SIGNAL_LOGIN_BOTH_INVALID, detail="备用亦失效", task_id="genshin_official")
    state = machine.state
    assert state.session_slot == R.SLOT_GUEST
    assert state.observe_until == machine._now() + dt.timedelta(hours=72)
    assert state.frequency_factor == settings.rate_limit.guest_factor
    assert state.level == R.LEVEL_WARN
    assert machine.check_before_run("genshin_official").allow is True, "观察期仍可游客态采集"


def test_parse_rate_low_continues_but_raw_only(machine: R.RiskMachine) -> None:
    low, rate = R.evaluate_parse_rate(7, 10, 0.80)
    assert low is True and rate == pytest.approx(0.7)
    assert R.evaluate_parse_rate(8, 10, 0.80)[0] is False, "恰为 80% 不算改版信号"

    machine.on_signal(R.SIGNAL_PARSE_RATE_LOW, detail="70.00%", task_id="genshin_official")
    assert machine.state.raw_only is True
    assert machine.state.backoff_until is None
    assert machine.check_before_run("genshin_official").allow is True, "解析率低不停止采集"


def test_ok_signal_resets_probe_and_counter(machine: R.RiskMachine) -> None:
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id="genshin_official")
    machine._now.advance(hours=25)
    machine.check_before_run("genshin_official")           # 进入试探
    assert machine.state.probe_mode is True
    machine.on_signal(R.SIGNAL_OK, task_id="genshin_official")
    assert machine.state.probe_mode is False
    assert machine.state.consecutive == 0
    assert machine.state.level == R.LEVEL_NORMAL
    assert machine.state.frequency_factor == 1.0


def test_state_persists_required_fields(settings: cfg.Settings, tmp_path: Path) -> None:
    path = tmp_path / "risk_state.json"
    clock = Clock()
    m1 = R.RiskMachine(settings, state_path=path, now=clock)
    m1.on_signal(R.SIGNAL_CAPTCHA, detail="滑块", task_id="genshin_official")

    raw = _load_state_file(path)
    for key in ("level", "reason", "backoff_until", "last_trigger", "consecutive"):
        assert key in raw, f"risk_state.json 必须含 {key}"
    assert raw["level"] == R.LEVEL_TASK_PAUSED
    assert raw["consecutive"] == 1

    # 重新加载（模拟进程重启）后状态一致
    m2 = R.RiskMachine(settings, state_path=path, now=clock)
    assert m2.state.level == R.LEVEL_TASK_PAUSED
    assert m2.state.backoff_until == m1.state.backoff_until
    assert m2.check_before_run("genshin_official").allow is False


def test_corrupt_state_file_fails_closed(settings: cfg.Settings, tmp_path: Path) -> None:
    """【回归】状态文件损坏时**不得**按初始态放行（终审发现项 #7：fail-open 会丢停采纪律）。"""
    path = tmp_path / "risk_state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    m = R.RiskMachine(settings, state_path=path, now=Clock())
    assert m.state.level == R.LEVEL_STOPPED
    assert "state-file-unreadable" in (m.state.reason or "")
    d = m.check_before_run("t")
    assert d.allow is False, "状态不可读 ⇒ 保守停采，等人工检查"
    assert any(a["kind"] == "state-unreadable" for a in m.drain_alerts()), "必须产生 A4 告警"


def test_missing_state_file_is_normal(settings: cfg.Settings, tmp_path: Path) -> None:
    """文件不存在 ≠ 损坏：全新部署仍可正常运行。"""
    m = R.RiskMachine(settings, state_path=tmp_path / "absent.json", now=Clock())
    assert m.state.level == R.LEVEL_NORMAL
    assert m.check_before_run("t").allow is True


def test_detect_signals_offline() -> None:
    assert R.detect_signals("请拖动滑块完成安全验证") == (R.SIGNAL_CAPTCHA,)
    assert R.SIGNAL_IP_BLOCKED in R.detect_signals("您的访问过于频繁，请稍后再试")
    assert R.detect_signals("原神 官服 满命 钟离") == ()
    assert R.detect_signals("") == ()


def test_signals_from_round_prioritises_page_signals() -> None:
    signals = R.signals_from_round(rejected=9, total=10, cards_seen=10, cards_parsed=1,
                                   text="请求过于频繁", threshold_empty=0.30,
                                   threshold_parse=0.80)
    assert R.SIGNAL_IP_BLOCKED in signals
    assert R.SIGNAL_EMPTY_RATE in signals
    assert R.SIGNAL_PARSE_RATE_LOW in signals


# --------------------------------------------------------------------------- #
# 限速器
# --------------------------------------------------------------------------- #
def test_rate_limiter_page_interval_within_docs_range(settings: cfg.Settings) -> None:
    slept: list[float] = []
    limiter = R.RateLimiter(settings, sleeper=slept.append, rng=FixedRng())
    for page_no in range(2, 12):
        seconds = limiter.wait_between_pages(page_no=page_no)
        assert settings.rate_limit.page_interval_sec[0] <= seconds \
            <= settings.rate_limit.page_interval_sec[1]
    assert len(slept) == 10 and abs(sum(slept) - sum_seconds(limiter)) < 1e-6


def sum_seconds(limiter: R.RateLimiter) -> float:
    return limiter.slept_seconds


def test_rate_limiter_factors_follow_docs(settings: cfg.Settings) -> None:
    """docs/01 §3.3：登录态与游客态同档；切备用 1/2；验证码试探 1/4。"""
    limiter = R.RateLimiter(settings, sleeper=lambda s: None, rng=FixedRng())
    base = limiter.page_interval(factor=1.0)
    backup = limiter.page_interval(factor=settings.rate_limit.backup_state_factor)
    guest = limiter.page_interval(factor=settings.rate_limit.guest_factor)
    probe = limiter.page_interval(factor=settings.rate_limit.probe_factor)
    assert base <= settings.rate_limit.page_interval_sec[1]
    assert guest == pytest.approx(base), "游客态不得比登录态慢（登录只解锁可见性，不加压）"
    assert settings.rate_limit.guest_factor == settings.rate_limit.login_factor
    assert backup == pytest.approx(base * 2.0), "切备用 = 1/2 频率"
    assert probe == pytest.approx(base * 4.0), "验证码后试探 = 1/4 频率"


def test_guest_session_runs_at_same_tier_as_login(settings: cfg.Settings,
                                                  tmp_path: Path) -> None:
    """【回归】测试自身不得写生产 risk_state（终审发现项 #6：曾把测试任务写进真实状态文件）。"""
    machine = R.RiskMachine(settings, state_path=tmp_path / "risk_state.json")
    machine.sync_session_slot(R.SLOT_GUEST)
    d = machine.check_before_run("t")
    assert d.allow is True
    assert d.frequency_factor == settings.rate_limit.login_factor, \
        "游客态正常轮次与登录态同档（此前的 4× 是配置错误）"
    limiter = R.RateLimiter(settings, machine=machine, sleeper=lambda s: None, rng=FixedRng())
    assert limiter.page_interval() == pytest.approx(4.5)


def test_rate_limiter_max_pages_clamped_to_five(settings: cfg.Settings) -> None:
    limiter = R.RateLimiter(settings, sleeper=lambda s: None)
    assert limiter.max_pages(99) == settings.rate_limit.max_pages_per_run == 5
    assert limiter.max_pages(3) == 3
    assert limiter.max_pages(None) == 5


def test_task_interval_enforces_60s_gap(settings: cfg.Settings, tmp_path: Path) -> None:
    clock = Clock()
    machine = R.RiskMachine(settings, state_path=tmp_path / "risk.json", now=clock)
    slept: list[float] = []
    limiter = R.RateLimiter(settings, machine=machine, sleeper=slept.append)

    assert limiter.task_interval() == 0.0, "从未跑过任务时无需等待"
    limiter.note_task_finished()
    assert limiter.task_interval() == pytest.approx(60.0)
    waited = limiter.wait_between_tasks()
    assert waited == pytest.approx(60.0)
    assert slept == [pytest.approx(60.0)]

    clock.advance(seconds=75)
    assert limiter.task_interval() == 0.0, "已超 60s 不再补等"


def test_describe_rules_matches_settings(settings: cfg.Settings) -> None:
    rules = R.describe_rules(settings)
    assert rules["page_interval_sec"] == [3.0, 6.0]
    assert rules["task_interval_sec"] >= 60
    assert rules["max_pages_per_run"] == 5
    assert rules["captcha"]["probe_after_hours"] == 24
    assert rules["empty_response"]["backoff_hours"] == 6
    assert rules["login_state"]["observe_hours_after_both_invalid"] == 72


# --------------------------------------------------------------------------- #
# 采集前置闸门（不启动浏览器：退避未到期时不得创建页面）
# --------------------------------------------------------------------------- #
class _StubSession:
    """最小会话替身：用于验证「退避未到期直接终止」。"""

    def __init__(self, slot: str = "guest") -> None:
        self.slot = slot
        self.collected_via = "guest" if slot == "guest" else "login"
        self.frequency_factor = 1.0

        class _G:
            requests_seen = requests_allowed = requests_blocked = robots_skipped = 0
            block_reasons: dict = {}
        self.guard = _G()
        self.storage_state_path = None

    def new_page(self):      # pragma: no cover - 被调用即测试失败
        raise AssertionError("退避未到期时不应创建页面/发起请求")


def _task(settings: cfg.Settings) -> cfg.Task:
    return cfg.load_tasks(settings=settings).by_id("genshin_official")


def test_collect_list_aborts_without_request_when_backoff_active(
        settings: cfg.Settings, tmp_path: Path) -> None:
    clock = Clock()
    machine = R.RiskMachine(settings, state_path=tmp_path / "risk.json", now=clock)
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id="genshin_official")

    res = C.collect_list(_task(settings), settings=settings, session=_StubSession(),
                         machine=machine, run_id=C.new_run_id("genshin_official", now=clock()),
                         clock=clock)
    assert res.aborted_by_risk is True
    assert res.risk_signal == "captcha", "risk_signal 记触发原因（不是状态级别）"
    assert res.risk_level == R.LEVEL_TASK_PAUSED
    assert "退避至" in res.risk_reason
    assert res.pages == [], "未到期不得采集任何页面"
    fields = res.summary_fields()
    assert fields["cards_seen"] == 0 and fields["pages"] == 0
    assert fields["risk_trigger"] == "captcha", "summary 必须如实写明风控原因"
    assert fields["raw_dir"] is None, "本轮未落盘 raw 时不得虚报目录"


def test_collect_list_aborts_when_stopped(settings: cfg.Settings, tmp_path: Path) -> None:
    machine = R.RiskMachine(settings, state_path=tmp_path / "risk.json", now=Clock())
    machine.on_signal(R.SIGNAL_IP_BLOCKED, task_id="genshin_official")
    res = C.collect_list(_task(settings), settings=settings, session=_StubSession(),
                         machine=machine, run_id="r1")
    assert res.aborted_by_risk is True
    assert res.risk_signal == "ip-blocked"
    assert res.risk_level == R.LEVEL_STOPPED
    assert res.pages == []


# --------------------------------------------------------------------------- #
# URL / 路径工具
# --------------------------------------------------------------------------- #
def test_listing_page_url_pagination(settings: cfg.Settings) -> None:
    task = _task(settings)
    assert C.listing_page_url(settings, task, 1) == "https://www.pxb7.com/buy/10026/1"
    assert C.listing_page_url(settings, task, 3) == "https://www.pxb7.com/buy/10026/1?page=3"


def test_raw_dir_layout_and_component_validation(settings: cfg.Settings) -> None:
    now = dt.datetime(2026, 10, 2, 9, 30)
    path = C.raw_dir_for(settings, "genshin_official", "genshin_official-20261002T093000-abcd",
                         now=now)
    assert path == Path(settings.paths.raw_root) / "genshin_official" / "20261002" \
        / "genshin_official-20261002T093000-abcd"
    with pytest.raises(C.CollectorError):
        C.raw_dir_for(settings, "../evil", "run", now=now)
    with pytest.raises(C.CollectorError):
        C.raw_dir_for(settings, "ok", "run/../../etc", now=now)


def test_robots_disallowed_paths() -> None:
    from pxb7.urlguard import is_robots_disallowed
    assert is_robots_disallowed("https://www.pxb7.com/_nuxt/abc.js") is True
    assert is_robots_disallowed("https://www.pxb7.com/assets/logo.png") is True
    assert is_robots_disallowed("https://www.pxb7.com/buy/10026/1") is False
    assert is_robots_disallowed("https://www.pxb7.com/product/123/1") is False


def test_request_guard_allows_public_and_blocks_private() -> None:
    from pxb7.urlguard import classify_request_url
    assert classify_request_url("https://www.pxb7.com/_nuxt/x.js")[0] is True
    assert classify_request_url("data:image/png;base64,AAAA")[0] is True
    assert classify_request_url("http://127.0.0.1:8080/x")[0] is False
    assert classify_request_url("http://10.1.2.3/x")[0] is False
    assert classify_request_url("http://192.168.0.10/x")[0] is False
    assert classify_request_url("http://169.254.169.254/latest/meta-data/")[0] is False
    assert classify_request_url("file:///C:/Windows/win.ini")[0] is False
    assert classify_request_url("https://cdn.example.com/app.js")[0] is True
