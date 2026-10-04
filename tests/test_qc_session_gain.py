"""QC / W1 验收、登录态增益实测、新增 CLI 子命令、并发锁与 policy 口径注释（全部离线）。"""

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

from pxb7 import cli  # noqa: E402
from pxb7 import config as cfg  # noqa: E402
from pxb7 import db  # noqa: E402
from pxb7 import pipeline as PL  # noqa: E402
from pxb7 import qc  # noqa: E402
from pxb7 import risk as R  # noqa: E402
from pxb7 import session_gain as SG  # noqa: E402
from tests.test_cli_notify import LIST_HTML  # noqa: E402
from tests.test_crawl_refusal import _tmp_settings_file  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> cfg.Settings:
    base = cfg.load_settings()
    return dataclasses.replace(base, paths=dataclasses.replace(
        base.paths, db=tmp_path / "qc.duckdb", raw_root=tmp_path / "raw",
        state_dir=tmp_path / "state", runs=tmp_path / "runs",
        risk_state=tmp_path / "state" / "risk.json"))


# --------------------------------------------------------------------------- #
# W1 指标聚合
# --------------------------------------------------------------------------- #
def test_evaluate_w1_aggregates_contract_metrics() -> None:
    summaries = [
        {"status": "completed", "pages": 2, "cards_seen": 10, "cards_parsed": 9,
         "extract_hit_rate": 0.8, "risk_trigger": None},
        {"status": "completed", "pages": 1, "cards_seen": 10, "cards_parsed": 9,
         "extract_hit_rate": 0.6, "risk_trigger": None},
    ]
    m = qc.evaluate_w1(summaries, days_covered=3)
    assert m.rounds_total == 2 and m.rounds_ok == 2 and m.days_covered == 3
    assert m.success_rate == 1.0
    assert m.parse_success_rate == pytest.approx(18 / 20)
    assert m.extract_hit_rate == pytest.approx((0.8 * 9 + 0.6 * 9) / 18)
    assert m.risk_triggers == 0
    assert m.all_pass is True


def test_evaluate_w1_flags_risk_and_failed_rounds() -> None:
    summaries = [
        {"status": "aborted", "pages": 0, "cards_seen": 0, "cards_parsed": 0,
         "extract_hit_rate": 0.0, "risk_trigger": "captcha"},
        {"status": "completed", "pages": 1, "cards_seen": 10, "cards_parsed": 5,
         "extract_hit_rate": 0.4, "risk_trigger": None},
    ]
    m = qc.evaluate_w1(summaries, days_covered=3)
    assert m.rounds_ok == 1 and m.success_rate == 0.5
    assert m.risk_triggers == 1
    by_name = {e["name"]: e for e in m.evaluations}
    assert by_name["采集成功率"]["pass"] is False
    assert by_name["解析成功率"]["pass"] is False          # 5/10 < 85%
    assert by_name["风控触发次数"]["pass"] is False        # 目标 0，实际 1
    assert m.all_pass is False


def test_evaluate_w1_empty_window_is_not_pass() -> None:
    """【回归】0 轮次时**全部**指标（含风控触发）都不得判通过（终审发现项 #11）。"""
    m = qc.evaluate_w1([])
    assert m.rounds_total == 0 and m.all_pass is False, "无数据不得判为通过"
    by_name = {e["name"]: e for e in m.evaluations}
    assert by_name["风控触发次数"]["pass"] is False, "0 风控触发 × 0 轮次 = 空样本空过，不允许"
    assert all("无轮次数据" in e["source"] for e in m.evaluations)


def test_evaluate_w1_requires_full_day_coverage() -> None:
    """【回归】「连续 3 天采集成功率 ≥95%」：覆盖天数不足 3 天时不得判通过（终审发现项 #36）。"""
    summaries = [
        {"status": "completed", "pages": 2, "cards_seen": 10, "cards_parsed": 10,
         "extract_hit_rate": 0.9, "risk_trigger": None},
    ]
    m = qc.evaluate_w1(summaries, days_covered=1)
    by_name = {e["name"]: e for e in m.evaluations}
    assert by_name["采集成功率"]["pass"] is False, "单日 100% 成功也不满足「连续 3 天」口径"
    assert "实际 1 天" in by_name["采集成功率"]["source"]
    assert by_name["解析成功率"]["pass"] is True and by_name["词表抽取命中率"]["pass"] is True
    assert m.all_pass is False


def test_evaluate_w1_zero_sample_metrics_fail() -> None:
    """【回归】有轮次但 0 卡片时，解析率/命中率不得判通过。"""
    summaries = [{"status": "completed", "pages": 1, "cards_seen": 0, "cards_parsed": 0,
                  "extract_hit_rate": 0.0, "risk_trigger": None}]
    m = qc.evaluate_w1(summaries, days_covered=3)
    by_name = {e["name"]: e for e in m.evaluations}
    assert by_name["解析成功率"]["pass"] is False
    assert by_name["词表抽取命中率"]["pass"] is False
    assert by_name["风控触发次数"]["pass"] is True


def test_find_summaries_picks_up_cli_summary_files(settings) -> None:
    """【回归】CLI --summary-json 写出的文件（任意文件名）也必须被 QC 收集（终审发现项 #10）；
    extraction_report 等非 summary 文件不得混入。"""
    runs_dir = Path(settings.paths.runs)
    runs_dir.mkdir(parents=True)
    (runs_dir / "smoke.json").write_text(json.dumps({
        "run_id": "r-smoke", "task_id": "genshin_official", "status": "aborted",
        "cards_seen": 0, "cards_parsed": 0, "parse_success_rate": 0.0,
        "extract_hit_rate": 0.0, "pages": 0, "risk_trigger": "captcha",
        "collected_at": dt.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False),
        encoding="utf-8")
    (runs_dir / "extraction_report_x.json").write_text(
        json.dumps({"run_id": "r-x", "extract": {}}), encoding="utf-8")
    found = qc.find_summaries(settings)
    assert [payload["run_id"] for _, payload in found] == ["r-smoke"]


# --------------------------------------------------------------------------- #
# QC 检查项
# --------------------------------------------------------------------------- #
def test_qc_checks_on_clean_round(settings: cfg.Settings) -> None:
    db.init_db(settings.paths.db)
    conn = db.connect(settings.paths.db)
    try:
        now = dt.datetime.now().replace(microsecond=0)
        db.write_snapshot_round(conn, now, [
            {"listing_id": "1", "price_yuan": 100.0, "collected_via": "guest",
             "parser_version": "v0.1.0", "level": 60, "yellow_cnt": 10,
             "five_star_chars": 5, "five_star_weapons": 2, "server": "官服",
             "mail_status": "邮箱出售", "featured_chars": '["钟离"]',
             "publish_time_text": "21分钟内发布"},
        ])
        report = qc.run_qc(settings, conn=conn, now=now + dt.timedelta(hours=1))
    finally:
        conn.close()
    by_name = {c.name: c for c in report.checks}
    assert by_name["价格非负"].status == qc.STATUS_PASS
    assert by_name["快照断档"].status == qc.STATUS_PASS      # 1h < 36h
    assert by_name["listing_id 去重"].status == qc.STATUS_PASS
    assert by_name["关键字段缺失率"].status == qc.STATUS_INFO, "文档未给阈值 → 只报告不判定"
    assert by_name["卡片解析成功率"].status == qc.STATUS_SKIP, "窗口内无轮次 summary → 不评估"


def test_qc_flags_negative_price_and_gap(settings: cfg.Settings) -> None:
    db.init_db(settings.paths.db)
    conn = db.connect(settings.paths.db)
    try:
        now = dt.datetime.now().replace(microsecond=0)
        db.write_snapshot_round(conn, now - dt.timedelta(hours=50), [
            {"listing_id": "1", "price_yuan": -5.0, "collected_via": "guest"},
        ])
        report = qc.run_qc(settings, conn=conn, now=now)
    finally:
        conn.close()
    by_name = {c.name: c for c in report.checks}
    assert by_name["价格非负"].status == qc.STATUS_FAIL
    assert by_name["快照断档"].status == qc.STATUS_FAIL, "50h > 36h"
    assert report.ok is False


def test_qc_parse_rate_check_uses_summaries(settings: cfg.Settings) -> None:
    check = qc._check_parse_rate([{"cards_seen": 10, "cards_parsed": 7}])
    assert check.status == qc.STATUS_FAIL and check.value == 0.7
    assert qc._check_parse_rate([{"cards_seen": 10, "cards_parsed": 9}]).status == qc.STATUS_PASS


def test_cli_qc_exit_codes(settings, tmp_path, capsys) -> None:
    settings_file = _tmp_settings_file(tmp_path)
    db.init_db(settings.paths.db)
    # 空库 → 快照断档判失败 → 退出码 22
    assert cli.main(["qc", "--settings", str(settings_file), "--db", str(settings.paths.db)]) \
        == cli.EXIT_QC_FAIL
    out = capsys.readouterr().out
    assert "快照断档" in out and "W1 验收" in out
    report_path = tmp_path / "qc.json"
    assert cli.main(["qc", "--settings", str(settings_file), "--db", str(settings.paths.db),
                     "--out", str(report_path), "--json"]) == cli.EXIT_QC_FAIL
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert {c["name"] for c in payload["checks"]} >= {"价格非负", "快照断档", "listing_id 去重",
                                                     "关键字段缺失率", "卡片解析成功率"}
    assert payload["w1"]["all_pass"] is False


# --------------------------------------------------------------------------- #
# 登录态增益实测
# --------------------------------------------------------------------------- #
def _fake_result(*, via: str, cards: int, detail_fetched: int, viewers_visible: int,
                 favorites_visible: int, run_id: str) -> PL.PipelineResult:
    details = [C_Detail(viewers_visible=bool(viewers_visible), favorites_visible=bool(favorites_visible))
               for _ in range(detail_fetched)]
    list_result = C_ListResult(cards=cards, pages=1, via=via)
    return PL.PipelineResult(summary={
        **{k: None for k in PL.SUMMARY_KEYS},
        "run_id": run_id, "task_id": "genshin_official", "status": "completed",
        "collected_via": via, "pages": 1, "cards_seen": cards, "cards_parsed": cards,
        "parse_success_rate": 1.0, "extract_hit_rate": 0.0, "snapshots_inserted": cards,
        "new_listings": cards, "price_changes": 0, "delist_events": 0,
        "detail_fetched": detail_fetched, "risk_trigger": None, "raw_dir": None,
        "duration_s": 1.0,
    }, list_result=list_result, details=details)


class C_Detail:
    def __init__(self, *, viewers_visible: bool, favorites_visible: bool) -> None:
        self.viewers_visible = viewers_visible
        self.favorites_visible = favorites_visible


class C_ListResult:
    def __init__(self, *, cards: int, pages: int, via: str) -> None:
        self.session_slot = "guest" if via == "guest" else "primary"
        self.cards_seen = cards
        self.pages = [type("P", (), {"cards_seen": cards})() for _ in range(pages)]


def test_session_gain_skips_without_login_state(settings, task=None) -> None:
    """两次都跑成 guest（无登录态）→ skipped，绝不编造增益。"""
    calls: list[str] = []

    def runner(**kwargs):
        calls.append(kwargs.get("mode"))
        return _fake_result(via="guest", cards=20, detail_fetched=1, viewers_visible=0,
                            favorites_visible=0, run_id=f"gain-{len(calls)}")

    report = SG.run_session_gain(settings, runner=runner)
    assert calls == ["guest", None]
    assert report.status == SG.STATUS_SKIPPED
    assert "无可用登录态" in (report.reason or "")
    assert report.gains == {}, "无登录态时不得给出任何增益结论"


def test_session_gain_measures_when_login_available(settings) -> None:
    seq = [
        _fake_result(via="guest", cards=20, detail_fetched=1, viewers_visible=0,
                     favorites_visible=0, run_id="g"),
        _fake_result(via="login", cards=30, detail_fetched=1, viewers_visible=1,
                     favorites_visible=1, run_id="l"),
    ]
    report = SG.run_session_gain(settings, runner=lambda **kw: seq.pop(0))
    assert report.status == SG.STATUS_MEASURED
    assert report.gains["max_cards_per_page"]["delta"] == 10
    assert report.gains["viewers_visible_ratio"]["login"] == 1.0
    assert report.gains["favorites_visible_ratio"]["guest"] == 0.0
    lines = SG.format_report(report)
    assert any("单页最多=30" in line for line in lines)


def test_cli_session_gain_exit_24_without_login(settings, tmp_path) -> None:
    settings_file = _tmp_settings_file(tmp_path)
    import pxb7.session_gain as sg
    original = sg.run_session_gain
    try:
        sg.run_session_gain = lambda *a, **kw: SG.SessionGainReport(
            task_id="genshin_official", status=SG.STATUS_SKIPPED,
            reason="无可用登录态（该轮实际以游客态运行）→ 无法测量增益，未做任何绕过",
            measured_at=dt.datetime.now())
        assert cli.main(["session-gain", "--settings", str(settings_file)]) == cli.EXIT_NO_LOGIN
    finally:
        sg.run_session_gain = original


# --------------------------------------------------------------------------- #
# CLI：extract / load
# --------------------------------------------------------------------------- #
def test_cli_extract_writes_keywords(settings, tmp_path, capsys) -> None:
    settings_file = _tmp_settings_file(tmp_path)
    db.init_db(settings.paths.db)
    assert cli.main(["extract", "--settings", str(settings_file), "--db", str(settings.paths.db),
                     "--dry-run"]) == cli.EXIT_OK
    assert "未写入数据库" in capsys.readouterr().out
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM dim_keyword").fetchone()[0] == 0
    finally:
        conn.close()

    assert cli.main(["extract", "--settings", str(settings_file), "--db", str(settings.paths.db)]) \
        == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "写入 75 条" in out, "原神 18 + 鸣潮 25 + 三角洲 32（docs/02 §4.A1/§4.A4/§4.B）"
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        total, enabled = conn.execute(
            "SELECT count(*), sum(CASE WHEN enabled THEN 1 ELSE 0 END) FROM dim_keyword"
        ).fetchone()
    finally:
        conn.close()
    assert total == 75 and enabled == 74, \
        "75 条种子（原神 18 + 鸣潮 25 + 三角洲 32）中 1 条为 disabled 占位"


def test_cli_load_is_parse_raw_alias(settings, tmp_path, capsys) -> None:
    settings_file = _tmp_settings_file(tmp_path)
    db.init_db(settings.paths.db)
    run_dir = settings.paths.raw_root / "genshin_official" / "20261002" / "run-load"
    run_dir.mkdir(parents=True)
    (run_dir / "list_p01.html").write_text(LIST_HTML, encoding="utf-8")
    (run_dir / "list_p01.json").write_text(json.dumps({
        "url": "https://www.pxb7.com/buy/10026/1", "page_type": "list", "page_no": 1,
        "collected_at": "2026-10-02T10:07:00", "http_status": 200, "ok": True,
        "collected_via": "guest"}, ensure_ascii=False), encoding="utf-8")
    code = cli.main(["load", "--settings", str(settings_file), "--db", str(settings.paths.db),
                     "--run-dir", str(run_dir), "--task", "genshin_official"])
    assert code == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "等价于 parse-raw" in captured.err
    assert "snapshots=1" in captured.out


# --------------------------------------------------------------------------- #
# 并发锁 与 policy 口径注释
# --------------------------------------------------------------------------- #
def test_run_lock_blocks_second_runner(settings) -> None:
    with PL.run_lock(settings) as lock_path:
        assert lock_path and Path(lock_path).is_file()
        with pytest.raises(PL.PipelineError) as exc:
            with PL.run_lock(settings):
                pass
        assert "另一个采集进程" in str(exc.value)
    assert not Path(lock_path).exists(), "退出后必须释放锁"


def test_run_lock_skipped_when_concurrency_allowed(settings) -> None:
    allowed = dataclasses.replace(
        settings, risk_control=dataclasses.replace(settings.risk_control,
                                                   allow_concurrent_tasks=True))
    with PL.run_lock(allowed) as path1:
        with PL.run_lock(allowed) as path2:
            assert path1 is None and path2 is None


def test_run_lock_takes_over_dead_pid_lock(settings) -> None:
    """【回归】残留锁的 pid 已死亡时立即接管，不等 6h（终审发现项 #18：无人值守空窗）。"""
    lock_path = Path(settings.paths.state_dir) / "crawl.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid=999999999 at=2026-01-01T00:00:00\n", encoding="utf-8")
    with PL.run_lock(settings) as path:
        assert path and Path(path).is_file(), "死 pid 的残留锁应被接管并重建"
    assert not Path(lock_path).exists()


def test_policy_comments_present(settings) -> None:
    """契约要求必覆盖的口径：样本门槛 <30、断档 ≤36h、解析率 <80% vs ≥85%。"""
    db.init_db(settings.paths.db)
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        rows = dict(conn.execute(
            "SELECT column_name, comment FROM meta_column_comments"
            " WHERE table_name = 'policy'").fetchall())
        assert "min_cell_sample" in rows and "<30" in rows["min_cell_sample"]
        assert "snapshot_gap_max_hours" in rows and "36h" in rows["snapshot_gap_max_hours"]
        parse_comment = rows["parse_success_ratio"]
        assert "<80%" in parse_comment and "≥85%" in parse_comment
        parser_version = conn.execute(
            "SELECT comment FROM meta_column_comments WHERE table_name = 'fct_listing_snapshot'"
            " AND column_name = 'parser_version'").fetchone()[0]
        assert "85%" in parser_version
        price_comment = conn.execute(
            "SELECT comment FROM meta_column_comments WHERE table_name = 'fct_listing_snapshot'"
            " AND column_name = 'price_yuan'").fetchone()[0]
        assert "挂牌价" in price_comment and "成交价" in price_comment
    finally:
        conn.close()


def test_parse_ok_requires_listing_id() -> None:
    """【回归】无 listing_id 的卡片不可入库 → 不得算作解析成功（否则抬高 cards_parsed）。"""
    from pxb7 import parser as P
    html = """
    <li class="product-card">
      <div class="card-price">¥ 100</div>
      <div class="card-time">21分钟内发布</div>
      <ul><li>等级 60</li><li>黄数 10</li><li>五星角色 5</li><li>五星武器 2</li>
          <li>官服</li><li>邮箱出售</li></ul>
    </li>"""
    card = P.parse_card(html)
    assert card.listing_id is None
    assert card.parse_ok is False and card.fail_reason == "missing-listing-id"
    page = P.parse_list_page(f"<html><body><ul>{html}</ul></body></html>")
    assert page.cards_seen == 1 and page.cards_parsed == 0
    assert page.cards_without_id == 1
    assert page.parse_success_rate == 0.0