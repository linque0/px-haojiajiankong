"""数据层与配置的可执行自检（pytest）。

覆盖：
- init_db 幂等（连跑两次结果一致、表齐全、预置行齐全）
- 轮次幂等写入（同轮重跑先删后插；不同轮次追加）
- truncate_to_round 边界
- URL 守卫拒绝 localhost/私有/保留地址与非 http(s)
- settings 内部路径以项目根为基准（不依赖 CWD）

运行：pxb7-price-monitor/.venv/Scripts/python.exe -m pytest pxb7-price-monitor/tests -q
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import db  # noqa: E402
from pxb7 import config as cfg  # noqa: E402
from pxb7.urlguard import UnsafeUrlError, assert_safe_url, is_safe_url  # noqa: E402


# --------------------------------------------------------------------------- #
# 数据层
# --------------------------------------------------------------------------- #
def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.duckdb"

    first = db.init_db(db_path)
    second = db.init_db(db_path)

    assert first["created_tables"], "首次建库应当有新建表"
    assert second["created_tables"] == [], "重复建库不应再创建表"

    names = set(second["row_counts"])
    assert names == set(db.TABLES), f"业务表不齐：缺 {set(db.TABLES) - names}"
    assert second["row_counts"]["dim_game"] == len(db.PRESET_GAMES)
    assert second["row_counts"]["meta_column_comments"] == len(db.COLUMN_COMMENTS)
    # 预置任务来自 config/tasks.yaml（至少 genshin_official）
    assert second["row_counts"]["dim_task"] >= 1


def test_preset_genshin_row(tmp_path: Path) -> None:
    db_path = tmp_path / "t.duckdb"
    db.init_db(db_path)
    conn = db.connect(db_path, read_only=True)
    try:
        row = conn.execute(
            "SELECT game_name, biz_prod, genre, keyword_profile, enabled FROM dim_game"
            " WHERE game_id = 10026 AND biz_prod = 1").fetchone()
        task = conn.execute(
            "SELECT game_id, biz_prod, task_name, pages_per_run, runs_per_day, keyword_filter"
            " FROM dim_task WHERE task_id = 'genshin_official'").fetchone()
    finally:
        conn.close()

    assert row is not None, "dim_game 应预置 原神 10026/1"
    assert row[0] == "原神" and row[1] == 1 and row[4] is True
    assert task is not None, "dim_task 应预置 genshin_official"
    assert task[0] == 10026 and task[1] == 1 and task[2] == "原神-官服"
    assert task[3] == 5 and 2 <= task[4] <= 4
    assert task[5] == [], "keyword_filter 应为空数组"


def test_snapshot_round_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.duckdb"
    db.init_db(db_path)
    conn = db.connect(db_path)

    def rows(n: int, price: float) -> list[dict]:
        return [{"listing_id": f"L{i}", "price_yuan": price, "collected_via": "login",
                 "parser_version": "v0.1.0"} for i in range(n)]

    try:
        ts = dt.datetime(2026, 10, 2, 10, 7, 33)
        r1 = db.write_snapshot_round(conn, ts, rows(3, 100.0),
                                     keyword_rows=[{"listing_id": "L0", "keyword_id": "k1",
                                                    "hit_text": "满命"}])
        # 同一轮重跑：先删后插，行数不翻倍
        db.write_snapshot_round(conn, ts, rows(3, 90.0))
        after_same_round = conn.execute(
            "SELECT count(*), min(price_yuan) FROM fct_listing_snapshot").fetchone()
        hits = conn.execute("SELECT count(*) FROM fct_listing_keyword").fetchone()[0]
        # 新轮次：追加
        ts2 = dt.datetime(2026, 10, 2, 10, 45, 0)
        db.write_snapshot_round(conn, ts2, rows(2, 50.0))
        total = conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0]
        rounds = conn.execute(
            "SELECT count(DISTINCT snapshot_at) FROM fct_listing_snapshot").fetchone()[0]
    finally:
        conn.close()

    assert r1["snapshot_at"] == dt.datetime(2026, 10, 2, 10, 0), "应截断到 30 分钟轮次桶"
    assert after_same_round[0] == 3, "同轮重跑应当先删后插，不产生重复行"
    assert float(after_same_round[1]) == 90.0, "同轮数据应被新一批覆盖"
    assert hits == 0, "同轮快照重写时关键词命中应同轮共删"
    assert total == 5 and rounds == 2


def test_write_helpers_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.duckdb"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        seen = dt.datetime(2026, 10, 2, 10, 0)
        db.upsert_dim_listings(conn, [{"listing_id": "L1", "game_id": 10026, "title": "A",
                                       "seen_at": seen}])
        db.upsert_dim_listings(conn, [{"listing_id": "L1", "game_id": 10026, "title": None,
                                       "seen_at": seen + dt.timedelta(hours=3)}])
        listing = conn.execute(
            "SELECT title, first_seen, last_seen, is_active FROM dim_listing").fetchone()
        assert listing[0] == "A" and listing[3] is True
        assert listing[2] - listing[1] == dt.timedelta(hours=3)

        pc = [{"listing_id": "L1", "ts": seen, "old_price": 100.0, "new_price": 90.0}]
        db.write_price_changes(conn, pc)
        db.write_price_changes(conn, pc)
        assert conn.execute("SELECT count(*) FROM fct_price_change").fetchone()[0] == 1
        assert conn.execute("SELECT pct FROM fct_price_change").fetchone()[0] == pytest.approx(-0.1)

        delist = [{"listing_id": "L1", "first_seen": seen,
                   "last_seen": seen + dt.timedelta(days=2), "last_price": 90.0}]
        db.write_delist_events(conn, delist)
        db.write_delist_events(conn, delist)
        row = conn.execute("SELECT days_on_market FROM fct_delist_event").fetchone()
        assert row[0] == pytest.approx(2.0)
        assert conn.execute("SELECT is_active FROM dim_listing").fetchone()[0] is False

        ev = [{"event_id": "E1", "event_date": dt.date(2026, 10, 1), "event_type": "banner",
               "game_id": 10026, "title": "卡池", "source_url": "https://example.invalid/x"}]
        db.write_events(conn, ev)
        db.write_events(conn, ev)
        assert conn.execute("SELECT count(*) FROM fct_event").fetchone()[0] == 1
    finally:
        conn.close()


def test_snapshot_rejects_unknown_collected_via(tmp_path: Path) -> None:
    db_path = tmp_path / "t.duckdb"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        with pytest.raises(ValueError):
            db.write_snapshot_round(conn, dt.datetime(2026, 10, 2, 10, 0),
                                    [{"listing_id": "L1", "collected_via": "proxy"}])
    finally:
        conn.close()


def test_truncate_to_round() -> None:
    assert db.truncate_to_round(dt.datetime(2026, 10, 2, 10, 59, 59)) == dt.datetime(2026, 10, 2, 10, 30)
    assert db.truncate_to_round(dt.datetime(2026, 10, 2, 0, 0, 0)) == dt.datetime(2026, 10, 2, 0, 0)
    assert db.truncate_to_round(dt.datetime(2026, 10, 2, 23, 59), minutes=15) == dt.datetime(2026, 10, 2, 23, 45)
    with pytest.raises(ValueError):
        db.truncate_to_round(dt.datetime(2026, 10, 2), minutes=7)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_settings_paths_are_project_root_based(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # 假装从别处调用
    settings = cfg.load_settings()
    assert settings.paths.db == (cfg.PROJECT_ROOT / "data" / "pxb7.duckdb")
    assert settings.paths.storage_primary == (cfg.PROJECT_ROOT / "data" / "state" / "storage_primary.json")
    assert settings.paths.storage_backup == (cfg.PROJECT_ROOT / "data" / "state" / "storage_backup.json")
    assert settings.paths.raw_root == (cfg.PROJECT_ROOT / "data" / "raw" / "pxb7")
    assert settings.paths.db.is_absolute()


def test_cli_path_is_cwd_based(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert cfg.resolve_cli_path("x.duckdb") == (tmp_path / "x.duckdb").resolve()


def test_rate_limit_matches_docs() -> None:
    settings = cfg.load_settings()
    assert settings.rate_limit.page_interval_sec == (3.0, 6.0)
    assert settings.rate_limit.task_interval_sec >= 60
    assert settings.rate_limit.max_pages_per_run <= 5
    assert settings.risk_control.empty_response_ratio == pytest.approx(0.30)
    assert settings.risk_control.parse_success_ratio == pytest.approx(0.80)
    assert settings.risk_control.captcha_probe_after_hours == 24
    assert settings.risk_control.observe_hours_after_both_invalid == 72
    assert settings.quality["snapshot_gap_max_hours"] == 36
    assert settings.notify.get("webhook_url") == ""


def test_tasks_load_and_validate() -> None:
    settings, tasks = cfg.load_config()
    task = tasks.by_id("genshin_official")
    assert task.game_id == 10026 and task.biz_prod == 1
    assert task.keyword_filter == ()
    assert task.pages_per_run <= settings.rate_limit.max_pages_per_run
    assert settings.site.listing_url(task.game_id, task.biz_prod) == \
        "https://www.pxb7.com/buy/10026/1"


def test_config_rejects_loosened_rate_limit(tmp_path: Path) -> None:
    src = (cfg.DEFAULT_SETTINGS_PATH).read_text(encoding="utf-8")
    bad = src.replace("page_interval_sec: [3, 6]", "page_interval_sec: [1, 2]")
    bad_path = tmp_path / "settings_bad.yaml"
    bad_path.write_text(bad, encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.load_settings(bad_path)


# --------------------------------------------------------------------------- #
# URL 守卫
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "http://localhost/admin",
    "http://127.0.0.1:8080/x",
    "http://[::1]/x",
    "http://10.0.0.5/x",
    "http://192.168.1.1/x",
    "http://172.16.9.9/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/x",
    "http://site.internal/x",
    "http://printer.local/x",
    "file:///C:/Windows/win.ini",
    "ftp://example.com/x",
    "https://user:pass@example.com/x",
    "javascript:alert(1)",
])
def test_urlguard_rejects(url: str) -> None:
    assert is_safe_url(url) is False
    with pytest.raises(UnsafeUrlError):
        assert_safe_url(url)


def test_urlguard_accepts_https_and_enforces_allowlist() -> None:
    url = "https://www.pxb7.com/buy/10026/1"
    assert assert_safe_url(url, allowed_hosts=("pxb7.com",), require_https=True) == url
    with pytest.raises(UnsafeUrlError):
        assert_safe_url(url, allowed_hosts=("example.com",))
    with pytest.raises(UnsafeUrlError):
        assert_safe_url("http://www.pxb7.com/buy/10026/1", require_https=True)
