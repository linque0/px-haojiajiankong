"""真实样本回归测试：2026-10-02 冒烟捕获的阿里云 WAF 滑块验证页。

样本来源与元数据见 tests/fixtures/README.md（游客态、HTTP 200、无商品数据）。
本文件只验证「识别正确 + 不伪造数据」，不访问任何站点。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import collector as C  # noqa: E402
from pxb7 import parser as P  # noqa: E402
from pxb7 import pipeline as PL  # noqa: E402
from pxb7 import risk as R  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
WAF_HTML = FIXTURES / "pxb7_waf_challenge_20261002.list.html"
WAF_META = FIXTURES / "pxb7_waf_challenge_20261002.list.meta.json"


@pytest.fixture(scope="module")
def waf_html() -> str:
    return WAF_HTML.read_text(encoding="utf-8", errors="replace")


def test_fixture_provenance() -> None:
    meta = json.loads(WAF_META.read_text(encoding="utf-8"))
    assert meta["url"] == "https://www.pxb7.com/buy/10026/1"
    assert meta["http_status"] == 200 and meta["ok"] is True
    assert meta["collected_via"] == "guest"
    assert meta["risk_signals"] == ["captcha"]
    assert meta["cards_seen"] == 0 and meta["cards_parsed"] == 0


def test_text_layer_detects_waf_challenge(waf_html: str) -> None:
    text = P.visible_text(waf_html)
    assert "访问验证" in text and "请按住滑块" in text
    assert R.detect_signals(text) == (R.SIGNAL_CAPTCHA,), \
        "可见文本层必须识别出滑块验证"


def test_dom_layer_detects_active_waf_component(waf_html: str) -> None:
    dom = R.detect_dom_signals(waf_html)
    assert any("waf-slider-active" in s for s in dom), dom
    assert any("aliyun-captcha-shown" in s for s in dom), dom
    assert R.combine_signals(R.detect_signals(P.visible_text(waf_html)), dom) == (R.SIGNAL_CAPTCHA,)


def test_visible_text_excludes_inline_script(waf_html: str) -> None:
    """可见文本口径必须剔除 script/style，否则内联脚本字样会造成误报。"""
    raw = waf_html
    visible = P.visible_text(raw)
    assert len(visible) < len(raw) / 50, "可见文本应远小于整页源码"
    assert "<script" not in visible and "function(" not in visible


def test_parser_yields_zero_cards_on_waf_page(waf_html: str) -> None:
    """WAF 页上必须 0 卡片：不得把热搜占位/页脚文字当作商品卡。"""
    res = P.parse_list_page(waf_html, url="https://www.pxb7.com/buy/10026/1")
    assert res.cards_seen == 0
    assert res.cards_parsed == 0
    assert res.parse_success_rate == 0.0
    assert res.card_selector_used is None
    assert "哥伦比娅" not in [c.title for c in res.cards if c.title]


def test_collector_page_signals_on_waf_page(waf_html: str) -> None:
    signals = C._collect_page_signals(waf_html)
    assert R.SIGNAL_CAPTCHA in signals
    assert R.SIGNAL_IP_BLOCKED not in signals


def _waf_run_dir(base: Path, *, strip_meta_signals: bool = False) -> Path:
    """把真实 WAF 样本布置成一个 raw run 目录（模拟 crawl 落盘结构）。"""
    import shutil

    run_dir = base / "raw" / "genshin_official" / "20261002" / "waf-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(WAF_HTML, run_dir / "list_p01.html")
    meta = json.loads(WAF_META.read_text(encoding="utf-8"))
    if strip_meta_signals:
        meta["risk_signals"] = []          # 模拟元数据缺失/失真：重放必须自己认出来
    (run_dir / "list_p01.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return run_dir


def _waf_settings(tmp_path: Path):
    import dataclasses

    from pxb7 import config as cfg

    base = cfg.load_settings()
    return dataclasses.replace(base, project_root=tmp_path, paths=dataclasses.replace(
        base.paths, db=tmp_path / "replay.duckdb", raw_root=tmp_path / "raw",
        risk_state=tmp_path / "risk.json"))


def test_parse_raw_replay_of_waf_page_never_loads(tmp_path) -> None:
    """离线重放拦截页：不崩、0 卡片、报风控、DB 一行不写（防止把拦截页当正常页入库）。"""
    from pxb7 import config as cfg
    from pxb7 import db

    run_dir = _waf_run_dir(tmp_path)
    settings = _waf_settings(tmp_path)
    db.init_db(settings.paths.db)
    task = cfg.load_tasks(settings=settings).by_id("genshin_official")

    result = PL.replay_raw_dir(settings, run_dir=run_dir, task=task)

    assert set(result.summary) == set(PL.SUMMARY_KEYS)
    assert result.summary["status"] == "aborted"
    assert result.summary["risk_trigger"] == R.SIGNAL_CAPTCHA
    assert result.summary["cards_seen"] == 0 and result.summary["cards_parsed"] == 0
    assert result.summary["parse_success_rate"] == 0.0, "拦截页不得被当作解析成功"
    assert result.summary["snapshots_inserted"] == 0 and result.summary["new_listings"] == 0
    assert result.listings == []

    conn = db.connect(settings.paths.db, read_only=True)
    try:
        counts = db.table_counts(conn)      # 静态 SQL 的一次性计数
        loaded = {name: counts[name] for name in (
            "fct_listing_snapshot", "fct_listing_keyword", "dim_listing",
            "fct_price_change", "fct_delist_event")}
    finally:
        conn.close()
    assert all(value == 0 for value in loaded.values()), loaded
    # 诊断报告仍生成（字段明细可用于确认「0 命中」）
    report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert all(row["hits"] == 0 for row in report["fields"])


def test_parse_raw_redetects_risk_even_without_meta_signals(tmp_path) -> None:
    """元数据里的 risk_signals 被清空时，重放仍必须从 HTML 自行识别出风控页面。"""
    from pxb7 import config as cfg
    from pxb7 import db

    run_dir = _waf_run_dir(tmp_path, strip_meta_signals=True)
    settings = _waf_settings(tmp_path)
    db.init_db(settings.paths.db)
    task = cfg.load_tasks(settings=settings).by_id("genshin_official")
    result = PL.replay_raw_dir(settings, run_dir=run_dir, task=task)
    assert result.summary["status"] == "aborted"
    assert result.summary["risk_trigger"] == R.SIGNAL_CAPTCHA
    assert result.list_result is not None
    assert result.list_result.pages[0].signals, "页级信号必须由 HTML 重新识别出来"


def test_cli_parse_raw_returns_21_for_waf_page(tmp_path, capsys) -> None:
    from pxb7 import cli
    from pxb7 import db

    run_dir = _waf_run_dir(tmp_path)
    settings = _waf_settings(tmp_path)
    db.init_db(settings.paths.db)
    exit_code = cli.main(["parse-raw", "--db", str(settings.paths.db),
                          "--run-dir", str(run_dir), "--task", "genshin_official"])
    assert exit_code == cli.EXIT_RISK_ABORT == 21
    assert "风控/拦截页面" in capsys.readouterr().err


def test_pipeline_aborts_and_writes_summary_on_waf_page(tmp_path, monkeypatch) -> None:
    """把真实 WAF 页喂进 pipeline：必须 aborted + 24h 退避 + 0 快照，且 summary 键名齐全。"""
    import dataclasses

    from pxb7 import config as cfg
    from pxb7 import db
    from tests.test_collector import FixedRng, StubSession, list_html  # noqa: F401

    base = cfg.load_settings()
    settings = dataclasses.replace(base, project_root=tmp_path, paths=dataclasses.replace(
        base.paths, db=tmp_path / "waf.duckdb", raw_root=tmp_path / "raw",
        risk_state=tmp_path / "risk.json"))
    db.init_db(settings.paths.db)
    conn = db.connect(settings.paths.db)
    try:
        from tests.test_pipeline import Clock
        clock = Clock()
        machine = R.RiskMachine(settings, state_path=settings.paths.risk_state, now=clock)
        session = StubSession([WAF_HTML.read_text(encoding="utf-8", errors="replace")])
        result = PL.run_once(
            settings=settings, task=cfg.load_tasks(settings=settings).by_id("genshin_official"),
            pages=1, session=session, machine=machine,
            limiter=R.RateLimiter(settings, machine=machine, sleeper=lambda s: None, rng=FixedRng()),
            conn=conn, run_id="waf", clock=clock, notify_result=False)

        assert set(result.summary) == set(PL.SUMMARY_KEYS)
        assert result.summary["status"] == "aborted"
        assert result.summary["risk_trigger"] == R.SIGNAL_CAPTCHA
        assert result.summary["snapshots_inserted"] == 0
        assert result.summary["cards_seen"] == 0
        assert machine.state.level == R.LEVEL_TASK_PAUSED
        assert machine.state.backoff_until == clock() + __import__("datetime").timedelta(hours=24)
        assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 0
        # 原始页面仍落盘（排障证据）
        raw_dir = Path(result.summary["raw_dir"])
        assert (raw_dir / "list_p01.html").is_file()
    finally:
        conn.close()
