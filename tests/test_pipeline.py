"""编排层离线测试：增量合并、幂等、风控终止、raw-only、summary 契约键名。

全部用替身 session + 假 HTML，不访问真实站点；DB 落在 tmp_path。
"""

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

from pxb7 import config as cfg  # noqa: E402
from pxb7 import db  # noqa: E402
from pxb7 import extract as X  # noqa: E402
from pxb7 import pipeline as PL  # noqa: E402
from pxb7 import risk as R  # noqa: E402
from tests.test_collector import (  # noqa: E402
    EMPTY_HTML, FixedRng, StubSession, list_html,
)

CONTRACT_KEYS = {
    "run_id", "task_id", "status", "collected_via", "pages", "cards_seen", "cards_parsed",
    "parse_success_rate", "extract_hit_rate", "snapshots_inserted", "new_listings",
    "price_changes", "delist_events", "detail_fetched", "risk_trigger", "raw_dir", "duration_s",
}


class Clock:
    def __init__(self, start: dt.datetime | None = None):
        self.t = start or dt.datetime(2026, 10, 2, 10, 7, 0)

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw) -> dt.datetime:
        self.t += dt.timedelta(**kw)
        return self.t


@pytest.fixture()
def settings(tmp_path: Path) -> cfg.Settings:
    base = cfg.load_settings()
    return dataclasses.replace(
        base, paths=dataclasses.replace(
            base.paths, db=tmp_path / "t.duckdb", raw_root=tmp_path / "raw",
            state_dir=tmp_path / "state", runs=tmp_path / "runs",
            risk_state=tmp_path / "risk.json"))


@pytest.fixture()
def task(settings: cfg.Settings) -> cfg.Task:
    return cfg.load_tasks(settings=settings).by_id("genshin_official")


@pytest.fixture()
def conn(settings: cfg.Settings):
    db.init_db(settings.paths.db)
    connection = db.connect(settings.paths.db)
    yield connection
    connection.close()


@pytest.fixture()
def keywords() -> list[X.Keyword]:
    # 本文件的任务固定为 genshin_official → 取原神画像（与 gateway.py:572 的
    # `seed_keywords(game_id=task.game_id)` 同源口径）。2026-10-03 种子新增鸣潮画像后，
    # 不带 game_id 会把 wuwa 词表一并装载，鸣潮特征串进原神任务断言（docs/02 §7 代码口径风险）。
    return X.seed_keywords(game_id=10026)


def _limiter(settings: cfg.Settings, machine: R.RiskMachine) -> R.RateLimiter:
    return R.RateLimiter(settings, machine=machine, sleeper=lambda s: None, rng=FixedRng())


def _run(settings, task, conn, keywords, *, session, clock, run_id, pages=2, machine=None,
         detail=0):
    machine = machine or R.RiskMachine(settings, state_path=settings.paths.risk_state, now=clock)
    return PL.run_once(settings=settings, task=task, pages=pages, detail=detail,
                       session=session, machine=machine, limiter=_limiter(settings, machine),
                       keywords=keywords, conn=conn, run_id=run_id, clock=clock,
                       notify_result=False)


# 三张卡片：满命/精5/原石（命中），两张普通卡
RICH_CARD = """
<li class="product-card" data-listing-id="1000000001">
  <div class="card-title">原神 官服 满命钟离 精5专武 双爆毕业 原石 32000 纠缠之源 120</div>
  <div class="card-price">¥ 12000</div>
  <div class="card-time">21分钟内发布</div>
  <ul>
    <li class="attr-level">等级 60</li><li class="attr-yellow">黄数 45</li>
    <li class="attr-star-char">五星角色 12</li><li class="attr-star-weapon">五星武器 5</li>
    <li class="attr-server">官服</li><li class="attr-mail">邮箱出售</li>
  </ul>
  <div class="featured-chars"><span>钟离</span></div>
</li>
"""


def page_html(cards: list[tuple[str, int]], *, title_suffix: str = "") -> str:
    blocks = []
    for listing_id, price in cards:
        blocks.append(f"""
<li class="product-card" data-listing-id="{listing_id}">
  <div class="card-title">原神 官服 冒险等级60 黄数45 {title_suffix}</div>
  <div class="card-price">¥ {price}</div>
  <div class="card-time">21分钟内发布</div>
  <ul>
    <li class="attr-level">等级 60</li><li class="attr-yellow">黄数 45</li>
    <li class="attr-star-char">五星角色 12</li><li class="attr-star-weapon">五星武器 5</li>
    <li class="attr-server">官服</li><li class="attr-mail">邮箱出售</li>
  </ul>
</li>""")
    return f"<html><body><ul class='product-list'>{''.join(blocks)}</ul></body></html>"


def test_first_round_writes_snapshots_and_summary_contract(settings, task, conn, keywords,
                                                           tmp_path) -> None:
    clock = Clock()
    session = StubSession([page_html([("1000000001", 12000), ("1000000002", 800)]),
                           page_html([("1000000003", 500)], title_suffix="满命")])
    summary_path = tmp_path / "runs" / "r1.json"
    machine = R.RiskMachine(settings, state_path=settings.paths.risk_state, now=clock)
    result = PL.run_once(settings=settings, task=task, pages=2, session=session,
                         machine=machine, limiter=_limiter(settings, machine),
                         keywords=keywords, conn=conn, run_id="r1", clock=clock,
                         summary_path=summary_path, notify_result=False)

    assert set(result.summary) == CONTRACT_KEYS == set(PL.SUMMARY_KEYS)
    s = result.summary
    assert s["status"] == "completed"
    assert s["collected_via"] == "guest", "无登录态文件时如实标注 guest"
    assert s["pages"] == 2
    assert s["cards_seen"] == 3 and s["cards_parsed"] == 3
    assert s["parse_success_rate"] == pytest.approx(1.0)
    # 契约口径（终审发现项 #13 修正后）：只有第 3 张卡标题含「满命」→ 文本命中 1/3；
    # 普通卡片无标题词命中，卡片字段（原石/纠缠）不参与该口径
    assert s["extract_hit_rate"] == pytest.approx(1 / 3, abs=1e-3)   # summary 保留 4 位小数
    assert s["snapshots_inserted"] == 3
    assert s["new_listings"] == 3
    assert s["price_changes"] == 0, "首轮无上一轮可比"
    assert s["delist_events"] == 0
    assert s["detail_fetched"] == 0
    assert s["risk_trigger"] is None
    assert s["raw_dir"] and Path(s["raw_dir"]).is_dir()
    assert s["duration_s"] >= 0
    # summary.json 落盘且键名一致
    written = json.loads(summary_path.read_text(encoding="utf-8"))
    assert set(written) == CONTRACT_KEYS
    # DB 侧核对
    assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM dim_listing").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM fct_listing_keyword").fetchone()[0] >= 1
    row = conn.execute("SELECT extracted_features, collected_via, parser_version"
                       " FROM fct_listing_snapshot WHERE listing_id='1000000003'").fetchone()
    features = json.loads(row[0])
    assert features["constellation_cnt"] == 6, "标题含『满命』→ 命座特征"
    assert row[1] == "guest" and row[2] == "v0.2.0"
    # 无词表命中的 listing：extracted_features 必须为 NULL（不用空对象/0 冒充）
    none_row = conn.execute("SELECT extracted_features FROM fct_listing_snapshot"
                            " WHERE listing_id='1000000001'").fetchone()
    assert none_row[0] is None


def test_second_round_price_change_and_delist(settings, task, conn, keywords) -> None:
    clock = Clock()
    kw = keywords
    _run(settings, task, conn, kw, session=StubSession(
        [page_html([("1000000001", 12000), ("1000000002", 800)])]),
        clock=clock, run_id="r1", pages=1)

    # 第二轮：listing 1 降价，listing 2 消失；时间推进超过在售窗口 → 推断下架。
    # 必须**完整覆盖切片**（翻到自然末页）才允许推断，故第 2 页给空列表。
    clock.advance(days=3, hours=1)
    res2 = _run(settings, task, conn, kw, session=StubSession(
        [page_html([("1000000001", 10000)]), EMPTY_HTML]), clock=clock, run_id="r2", pages=2)

    s = res2.summary
    assert s["price_changes"] == 1
    assert s["delist_events"] == 1, "完整覆盖 + 超过在售窗口 → 下架推断"
    assert res2.load_stats.delist_inferred is True
    assert s["new_listings"] == 0, "已有 listing 不重复计新"
    change = conn.execute(
        "SELECT old_price, new_price, pct FROM fct_price_change").fetchone()
    assert float(change[0]) == 12000 and float(change[1]) == 10000
    assert change[2] == pytest.approx(-1 / 6)
    delist = conn.execute(
        "SELECT listing_id, days_on_market FROM fct_delist_event").fetchone()
    assert delist[0] == "1000000002"
    assert float(delist[1]) >= 0.0, "在售天数按 last_seen-first_seen 计算（本轮首见即消失 → 0 天）"
    inactive = conn.execute(
        "SELECT is_active FROM dim_listing WHERE listing_id='1000000002'").fetchone()[0]
    assert inactive is False


def test_truncated_round_does_not_infer_delist(settings, task, conn, keywords) -> None:
    """【回归】页数被上限截断（切片远大于 5 页窗口）时不得推断下架。

    旧实现只看「48h 未见」→ 会把滑出前 5 页窗口的在售账号批量写成 delist（数据污染）。
    """
    clock = Clock()
    _run(settings, task, conn, keywords, session=StubSession(
        [page_html([("1000000001", 12000), ("1000000002", 800)])]),
        clock=clock, run_id="t1", pages=1)

    clock.advance(days=3)
    pages = [page_html([(f"10000009{i:02d}", 500 + i)]) for i in range(5)]   # 每页都有卡片
    res = _run(settings, task, conn, keywords, session=StubSession(pages),
               clock=clock, run_id="t2", pages=5)

    assert res.summary["delist_events"] == 0, "被页数上限截断 ⇒ 覆盖不完整 ⇒ 不得推断下架"
    assert res.load_stats.delist_inferred is False
    assert (res.load_stats.delist_skip_reason or "").startswith("page-cap-reached")
    # 未见的老 listing 仍保持 is_active（没被误判成下架）
    still_active = conn.execute(
        "SELECT is_active FROM dim_listing WHERE listing_id='1000000002'").fetchone()[0]
    assert still_active is True


def test_same_round_rerun_is_idempotent(settings, task, conn, keywords) -> None:
    clock = Clock()
    html = page_html([("1000000001", 12000), ("1000000002", 800)])
    r1 = _run(settings, task, conn, keywords, session=StubSession([html]), clock=clock,
              run_id="same", pages=1)
    r2 = _run(settings, task, conn, keywords, session=StubSession([html]), clock=clock,
              run_id="same", pages=1)
    assert r1.summary["snapshots_inserted"] == 2
    assert r2.summary["snapshots_inserted"] == 2
    assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 2, \
        "同轮重跑先删后插，不产生重复行"
    assert conn.execute("SELECT count(DISTINCT snapshot_at) FROM fct_listing_snapshot"
                        ).fetchone()[0] == 1


def test_risk_gate_aborts_round_and_still_writes_summary(settings, task, conn, keywords,
                                                        tmp_path) -> None:
    clock = Clock()
    machine = R.RiskMachine(settings, state_path=settings.paths.risk_state, now=clock)
    machine.on_signal(R.SIGNAL_CAPTCHA, task_id=task.task_id)
    session = StubSession([page_html([("1", 100)])])
    result = PL.run_once(settings=settings, task=task, pages=1, session=session, machine=machine,
                         limiter=_limiter(settings, machine), keywords=keywords, conn=conn,
                         run_id="r-risk", clock=clock, notify_result=False)
    s = result.summary
    assert set(s) == CONTRACT_KEYS
    assert s["status"] == "aborted"
    assert s["risk_trigger"] == "captcha", "risk_trigger 记触发原因"
    assert s["raw_dir"] is None, "前置闸门拦截的那一轮不落盘 raw，不得虚报目录"
    assert s["cards_seen"] == 0 and s["snapshots_inserted"] == 0
    assert session.navigated == [], "退避未到期不得发起任何请求"
    assert result.risk_detail is not None
    assert result.risk_detail["requests_issued"] is False
    assert result.risk_detail["backoff_remaining_s"] > 0


def test_low_parse_rate_goes_raw_only_and_skips_db_writes(settings, task, conn, keywords) -> None:
    """解析成功率 <80% → 采集继续但只入 raw 层（docs/01 §3.3），不写快照。"""
    clock = Clock()
    machine = R.RiskMachine(settings, state_path=settings.paths.risk_state, now=clock)
    # 5 张卡片里只有 2 张有价格 → 解析成功率 40% < 80%
    html = page_html([("1000000011", 300), ("1000000012", 400)])
    extra = "".join(f"""
<li class="product-card" data-listing-id="10000000{20 + i}">
  <div class="card-title">解析不了的卡片 {i}</div>
</li>""" for i in range(3))
    head, sep, tail = html.rpartition("</ul>")      # 只在最外层列表末尾追加坏卡片
    html = head + extra + sep + tail
    session = StubSession([html])
    result = PL.run_once(settings=settings, task=task, pages=1, session=session, machine=machine,
                         limiter=_limiter(settings, machine), keywords=keywords, conn=conn,
                         run_id="r-raw", clock=clock, notify_result=False)

    assert result.list_result is not None
    assert result.list_result.parse_success_rate == pytest.approx(2 / 5)
    assert machine.state.raw_only is True, "解析成功率低必须置 raw_only（告警而不停采）"
    assert result.summary["risk_trigger"] == R.SIGNAL_PARSE_RATE_LOW
    assert result.summary["status"] == "partial"
    assert result.summary["snapshots_inserted"] == 0
    assert result.load_stats.skipped_reason == "raw-only"
    assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 0
    assert Path(result.summary["raw_dir"]).is_dir(), "raw 仍要落盘（只入 raw 层）"


def test_detail_pages_fill_m5_fields(settings, task, conn, keywords) -> None:
    clock = Clock()
    detail_html = ("<html><body><div class='viewer-count'>1234人正在浏览</div>"
                   "<div class='collect-count'>56人已收藏</div></body></html>")
    session = StubSession([page_html([("1000000001", 12000)])])
    # 详情页复用同一个 page 对象：第 2 次 goto 取第 2 份 HTML
    session._htmls = [page_html([("1000000001", 12000)]), detail_html]
    result = _run(settings, task, conn, keywords, session=session, clock=clock,
                  run_id="r-detail", pages=1, detail=1)
    assert result.summary["detail_fetched"] == 1
    row = conn.execute("SELECT viewers_masked, favorites_cnt FROM fct_listing_snapshot"
                       " WHERE listing_id='1000000001'").fetchone()
    assert row[0] == 1234 and row[1] == 56


def test_empty_slice_completes_without_fake_metrics(settings, task, conn, keywords) -> None:
    """切片确实没有在售（HTTP 200 空列表）时：轮次正常完成、指标如实记 0、不触发风控。

    旧实现把自然末页算作空响应 → 误判 6h 退避；此处按 docs/01 §3.3 修正后的口径。
    """
    clock = Clock()
    result = _run(settings, task, conn, keywords, session=StubSession([EMPTY_HTML]),
                  clock=clock, run_id="r-empty", pages=1)
    s = result.summary
    assert s["cards_seen"] == 0 and s["cards_parsed"] == 0
    assert s["parse_success_rate"] == 0.0 and s["extract_hit_rate"] == 0.0, "分母 0 记 0"
    assert s["status"] == "completed", "正常取回、列表为空 → 轮次完成（不是风控终止）"
    assert s["risk_trigger"] is None
    assert s["snapshots_inserted"] == 0 and s["new_listings"] == 0
    assert result.list_result is not None
    assert result.list_result.natural_end_count == 1
    assert result.list_result.empty_response_rate == 0.0
    assert result.list_result.raw_dir_actual is not None, "空列表页也要落 raw 供排障"


# --------------------------------------------------------------------------- #
# 会话槽位升级（2026-10-03 试运行发现：导出登录态后 crawl 仍以 guest 运行并被 WAF 拦截）
# --------------------------------------------------------------------------- #
_USABLE_STATE = ('{"cookies": [{"name": "t", "value": "v",'
                 ' "domain": "www.pxb7.com", "path": "/"}], "origins": []}')


def test_stale_guest_slot_upgrades_when_login_available(settings, task, conn, keywords,
                                                        tmp_path) -> None:
    """【回归】状态槽位 guest 仅为“当时无登录态”的遗留时，可用登录态出现必须升级回主登录态。"""
    primary = tmp_path / "storage_primary.json"
    primary.write_text(_USABLE_STATE, encoding="utf-8")
    settings2 = dataclasses.replace(settings, paths=dataclasses.replace(
        settings.paths, storage_primary=primary))
    clock = Clock()
    machine = R.RiskMachine(settings2, state_path=settings2.paths.risk_state, now=clock)
    machine.sync_session_slot(R.SLOT_GUEST)          # 遗留的 guest 槽位
    session = StubSession([page_html([("1000000001", 12000)])], slot="primary")
    res = _run(settings2, task, conn, keywords, session=session, clock=clock,
               run_id="r-upgrade", pages=1, machine=machine)
    assert machine.state.session_slot == R.SLOT_PRIMARY, "登录态可用时必须升级回主登录态"
    assert res.summary["collected_via"] == "login", "本轮必须以登录态运行"
    assert res.summary["status"] == "completed"


def test_no_slot_upgrade_during_observation_window(settings, task, conn, keywords) -> None:
    """【回归】主备均失效后的 72h 观察期内不得自动升级回登录态（docs/01 §3.3 观察纪律）。"""
    clock = Clock()
    machine = R.RiskMachine(settings, state_path=settings.paths.risk_state, now=clock)
    machine.sync_session_slot(R.SLOT_GUEST)
    machine.state.observe_until = clock() + dt.timedelta(hours=72)
    machine.store.save(machine.state)
    session = StubSession([page_html([("1000000002", 800)])])   # 默认 guest 槽位
    _run(settings, task, conn, keywords, session=session, clock=clock,
         run_id="r-observe", pages=1, machine=machine)
    assert machine.state.session_slot == R.SLOT_GUEST, "观察期内不得升级回登录态"
