"""按游戏分类 schema 测试（db.ensure_game_schemas，2026-10-05 用户指令）。

覆盖：有数据的游戏建 game_<id> schema 与 listings/keyword_hits 视图、行数与主视图
该游戏行数一致、幂等可重跑、无数据游戏的残留 schema 被清理、非 game_<数字> 的
schema 不动、未登记游戏如实列出（不建错名）。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import db  # noqa: E402

from tests.test_analysis import seeded, settings  # noqa: E402,F401


def test_game_schemas_created_with_matching_rows(seeded) -> None:  # noqa: F811
    with db.connect(seeded.paths.db) as conn:
        result = db.ensure_game_schemas(conn)
    # 夹具有原神(10026)两号 + 鸣潮(10302)一号
    assert result["schemas"] == {"game_10026": 2, "game_10302": 1}
    assert result["unregistered"] == []
    with db.connect(seeded.paths.db, read_only=True) as conn:
        # 视图存在且行数与主视图该游戏行一致
        assert conn.execute(
            'SELECT count(*) FROM "game_10026".listings').fetchone()[0] == 2
        assert conn.execute(
            'SELECT count(*) FROM "game_10302".listings').fetchone()[0] == 1
        # 视图不是复制：内容随主视图口径（含 game_id 列且全部是该游戏）
        assert conn.execute(
            'SELECT count(*) FROM "game_10026".listings'
            " WHERE game_id <> 10026").fetchone()[0] == 0
        # 词表命中长表视图存在且非空（夹具给 W3/A1 命中过词）
        assert conn.execute(
            'SELECT count(*) FROM "game_10026".keyword_hits').fetchone()[0] >= 1


def test_game_schemas_idempotent(seeded) -> None:  # noqa: F811
    with db.connect(seeded.paths.db) as conn:
        first = db.ensure_game_schemas(conn)
        second = db.ensure_game_schemas(conn)
    assert first == second


def test_game_schemas_drop_stale_and_keep_foreign(seeded) -> None:  # noqa: F811
    with db.connect(seeded.paths.db) as conn:
        # 已登记游戏（10032）的残留 schema 应被其 DROP 分支清理；
        # 非 game_<数字> 的 schema 一律不动；未登记游戏的残留不自动清理
        # （动态 DDL 被门禁禁止，见 docs/05 诚实边界），函数也不因此报错。
        conn.execute('CREATE SCHEMA IF NOT EXISTS "game_10032"')
        conn.execute('CREATE TABLE IF NOT EXISTS "game_10032".t (x INTEGER)')
        conn.execute('CREATE SCHEMA IF NOT EXISTS "game_99999"')
        conn.execute('CREATE SCHEMA IF NOT EXISTS "game_其他"')
        conn.execute('CREATE TABLE IF NOT EXISTS "game_其他".t (x INTEGER)')
        db.ensure_game_schemas(conn)
        leftovers = {r[0] for r in conn.execute(
            "SELECT schema_name FROM information_schema.schemata").fetchall()}
        assert "game_10032" not in leftovers, "已登记但无数据的游戏 schema 被清理"
        assert "game_其他" in leftovers, "非 game_<数字> 命名空间一律不动"
        assert "game_99999" in leftovers, "未登记残留不自动清理（无动态 DDL）"


def test_game_schemas_reports_unregistered(seeded) -> None:  # noqa: F811
    with db.connect(seeded.paths.db) as conn:
        # 夹具里塞一个有数据但未登记 schema 的游戏
        conn.execute(
            "INSERT INTO dim_listing (listing_id, game_id, title, first_seen,"
            " last_seen, is_active) VALUES ('X1', 99999, '未登记游戏测试号',"
            " now(), now(), TRUE)")
        conn.execute(
            "INSERT INTO fct_listing_snapshot (snapshot_at, listing_id, price_yuan,"
            " collected_via) VALUES (now(), 'X1', 10.0, 'login')")
        result = db.ensure_game_schemas(conn)
    assert result["unregistered"] == [99999], "未登记游戏如实列出，不建错名"
