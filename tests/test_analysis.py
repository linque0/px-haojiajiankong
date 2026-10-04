"""分析就绪层测试：视图语义（最新轮次/质量标记/词表命中/样本门槛）+ 导出文件。

数据用参数化 INSERT 直接构造（本层考的是聚合与门槛口径，不是采集路径），
刻意覆盖：多轮 vs 单轮、天花板词 vs 折价词、价格缺失、黄数与五星数不自洽、
样本不足（unpublished）与门槛放开后的发布。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import analysis as A  # noqa: E402
from pxb7 import config as cfg  # noqa: E402
from pxb7 import db  # noqa: E402
from pxb7 import extract as X  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> cfg.Settings:
    base = cfg.load_settings()
    return dataclasses.replace(base, paths=dataclasses.replace(
        base.paths, db=tmp_path / "an.duckdb", raw_root=tmp_path / "raw",
        state_dir=tmp_path / "state", runs=tmp_path / "runs",
        risk_state=tmp_path / "risk.json"))


def _listing(conn, listing_id: str, game_id: int, title: str, first_seen: str) -> None:
    conn.execute(
        "INSERT INTO dim_listing (listing_id, game_id, title, first_seen, last_seen, is_active)"
        " VALUES (?, ?, ?, CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP), TRUE)",
        [listing_id, game_id, title, first_seen, first_seen])


def _snapshot(conn, listing_id: str, round_ts: str, price, *, level=None, yellow=None,
              five_chars=None, five_weapons=None, server="官服", mail=None,
              features=None, publish=None) -> None:
    conn.execute(
        "INSERT INTO fct_listing_snapshot (snapshot_at, listing_id, price_yuan, level,"
        " yellow_cnt, five_star_chars, five_star_weapons, server, mail_status,"
        " extracted_features, publish_time_text, collected_via)"
        " VALUES (CAST(? AS TIMESTAMP), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'login')",
        [round_ts, listing_id, price, level, yellow, five_chars, five_weapons, server,
         mail, features, publish])


def _hit(conn, listing_id: str, round_ts: str, keyword_id: str, hit_text: str) -> None:
    conn.execute(
        "INSERT INTO fct_listing_keyword (snapshot_at, listing_id, keyword_id, hit_text)"
        " VALUES (CAST(? AS TIMESTAMP), ?, ?, ?)", [round_ts, listing_id, keyword_id, hit_text])


@pytest.fixture()
def seeded(settings: cfg.Settings) -> cfg.Settings:
    """两份原神 + 一份鸣潮；原神 A 两轮（含天花板词与折价词），B 单轮且黄数不自洽。"""
    db.init_db(settings.paths.db)
    with db.connect(settings.paths.db) as conn:
        db.upsert_dim_keywords(conn, X.seed_db_rows())
        _listing(conn, "A1", 10026, "原神 满命 官服", "2026-10-01 10:00:00")
        _listing(conn, "B2", 10026, "原神 初始号", "2026-10-03 09:00:00")
        _listing(conn, "W3", 10302, "鸣潮 3命弗洛洛", "2026-10-03 09:00:00")
        # A1：两轮（100 → 120），第二轮带详情回填与抽取特征
        _snapshot(conn, "A1", "2026-10-03 10:00:00", 100, level=60, yellow=20,
                  five_chars=10, five_weapons=5, features='{"constellation_cnt": 3}',
                  publish="2026-10-02 08:30:00")
        _snapshot(conn, "A1", "2026-10-03 11:00:00", 120, level=60, yellow=20,
                  five_chars=10, five_weapons=5, features='{"constellation_cnt": 6}',
                  publish="2026-10-02 08:30:00")
        _hit(conn, "A1", "2026-10-03 11:00:00", "ys_ceiling_manming", "满命")
        _hit(conn, "A1", "2026-10-03 11:00:00", "ys_floor_deadmail", "死邮")
        # B2：单轮 + 黄数(5) < 五星角色(4)+五星武器(3) → 质量标记
        _snapshot(conn, "B2", "2026-10-03 11:00:00", 300, level=1, yellow=5,
                  five_chars=4, five_weapons=3)
        # W3：无价格（price_missing）+ 鸣潮词命中
        _snapshot(conn, "W3", "2026-10-03 11:00:00", None, yellow=39, five_chars=3,
                  features='{"constellation_cnt": 3}')
        _hit(conn, "W3", "2026-10-03 11:00:00", "wuwa_seg_nming", "3命")
        _hit(conn, "W3", "2026-10-03 11:00:00", "wuwa_resource_xingsheng", "星声")
    return settings


# --------------------------------------------------------------------------- #
# 视图语义
# --------------------------------------------------------------------------- #
def test_init_db_creates_analysis_views(settings: cfg.Settings) -> None:
    summary = db.init_db(settings.paths.db)
    assert "meta_params" in db.TABLES
    assert set(summary["analysis_layer"]) == set(db.ANALYSIS_LAYER)
    with db.connect(settings.paths.db, read_only=True) as conn:
        views = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'"
        ).fetchall()}
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
        ).fetchall()}
        assert set(db.ANALYSIS_VIEWS) <= views
        assert set(db.ANALYSIS_TABLES) <= tables, "特征矩阵按设计是物化表（视图不支持动态 PIVOT）"
        assert conn.execute("SELECT count(*) FROM meta_params WHERE key = ?",
                            [db.MIN_CELL_SAMPLE_PARAM]).fetchone()[0] == 1


def test_latest_view_takes_latest_round_with_trajectory(seeded: cfg.Settings) -> None:
    result = A.prepare_analysis(seeded)
    assert result["view_rows"]["v_listing_latest"] == 3, "每号一行"
    with db.connect(seeded.paths.db, read_only=True) as conn:
        row = conn.execute("""
            SELECT price_yuan, snapshot_rounds, price_min, price_max, feat_constellation_cnt,
                   publish_time, days_on_market
            FROM v_listing_latest WHERE listing_id = 'A1'""").fetchone()
        assert float(row[0]) == 120.0, "取最新一轮价格"
        assert row[1] == 2 and float(row[2]) == 100.0 and float(row[3]) == 120.0
        assert row[4] == 6, "抽取特征从 extracted_features 展开（最新轮）"
        assert str(row[5]).startswith("2026-10-02"), "上架时间解析"
        assert row[6] == 2, "first_seen → 最新轮的在售天数"


def test_analysis_view_flags_and_keyword_counts(seeded: cfg.Settings) -> None:
    A.prepare_analysis(seeded)
    with db.connect(seeded.paths.db, read_only=True) as conn:
        a1 = conn.execute("SELECT kw_ceiling_hits, kw_floor_hits, ceiling_keywords, floor_keywords,"
                          " quality_flags FROM v_listing_analysis WHERE listing_id = 'A1'"
                          ).fetchone()
        assert a1[0] == 1 and a1[1] == 1, "按类型计数"
        assert "满命" in a1[2] and "死邮" in a1[3], "命中词清单（归因用）"
        assert "single_round" not in (a1[4] or ""), "两轮不算单轮"
        b2 = conn.execute("SELECT quality_flags FROM v_listing_analysis WHERE listing_id = 'B2'"
                          ).fetchone()[0]
        assert "single_round" in b2 and "yellow_lt_5star_sum" in b2, f"B2 标记：{b2}"
        w3 = conn.execute("SELECT quality_flags, kw_hits FROM v_listing_analysis"
                          " WHERE listing_id = 'W3'").fetchone()
        assert "price_missing" in w3[0] and w3[1] == 2


def test_keyword_matrix_and_hits_views(seeded: cfg.Settings) -> None:
    A.prepare_analysis(seeded)
    with db.connect(seeded.paths.db, read_only=True) as conn:
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'keyword_feature_matrix' ORDER BY ordinal_position").fetchall()]
        assert {"listing_id", "snapshot_at", "game_id", "ys_ceiling_manming",
                "wuwa_seg_nming"} <= set(cols), f"矩阵列：{cols}"
        val = conn.execute("SELECT ys_ceiling_manming, wuwa_seg_nming FROM keyword_feature_matrix"
                           " WHERE listing_id = 'A1'").fetchone()
        assert val[0] == 1 and val[1] is None, "别的游戏的词条列在该行为 NULL（未命中）"
        latest = conn.execute("SELECT count(*) FROM v_keyword_hits WHERE is_latest_round"
                              ).fetchone()[0]
        assert latest == 4, "四条命中都在各自 listing 的最新一轮"


def test_price_band_gate_and_deals(seeded: cfg.Settings) -> None:
    A.prepare_analysis(seeded)
    with db.connect(seeded.paths.db, read_only=True) as conn:
        bands = {r[0] for r in conn.execute("SELECT DISTINCT price_band FROM v_price_band"
                                           ).fetchall()}
        assert bands == {"unpublished"}, f"样本不足必须 unpublished：{bands}"
        assert conn.execute("SELECT count(*) FROM v_deals").fetchone()[0] == 0, \
            "样本不足时不应产出捡漏候选（不伪造）"
        assert conn.execute("SELECT DISTINCT band_basis FROM v_price_band").fetchone()[0] \
            == "price_level", "口径如实标注"
    # 门槛放开到 1 → 开始发布（重新准备要写连接，务必先关掉上面的只读连接：
    # DuckDB 同一库文件不允许只读/读写混合连接）
    A.prepare_analysis(seeded, min_cell_sample=1)
    with db.connect(seeded.paths.db, read_only=True) as conn:
        published = conn.execute("SELECT listing_id, price_band, keyword_domain FROM v_price_band"
                                 " WHERE price_band <> 'unpublished' ORDER BY listing_id"
                                 ).fetchall()
        assert {r[0] for r in published} == {"A1", "B2"}, "W3 无价格不参与定价"
        a1 = next(r for r in published if r[0] == "A1")
        assert a1[1] in ("floor", "low") and a1[2] == "floor", "命中折价词 → floor 域"
        deals = conn.execute("SELECT listing_id, caution, method FROM v_deals").fetchall()
        assert any(d[0] == "A1" and d[1] and "折价词" in d[1] for d in deals), \
            "含折价词的低价必须给 caution"
        assert all(d[2] == "v0_rule" for d in deals), "方法如实标注为规则版"


# --------------------------------------------------------------------------- #
# 导出与数据字典
# --------------------------------------------------------------------------- #
def test_export_writes_files_and_dictionary(seeded: cfg.Settings, tmp_path: Path) -> None:
    A.prepare_analysis(seeded)
    out = tmp_path / "analysis-out"
    exported = A.export_views(seeded, out, fmt="both")
    names = set(exported["files"])
    for view in db.ANALYSIS_LAYER:
        assert f"{view}.csv" in names and f"{view}.parquet" in names, f"缺 {view} 的导出文件"
    readme = (out / "README.md").read_text(encoding="utf-8")
    for marker in ("分析数据字典", "样本门槛", "已知缺口", "挂牌价 ≠ 成交价", "v_listing_analysis"):
        assert marker in readme, f"数据字典缺：{marker}"
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["min_cell_sample"] == 30 and manifest["format"] == "both"
    counts = {item["view"]: item["rows"] for item in manifest["views"]}
    assert counts["v_listing_latest"] == 3
    # Parquet 回读校验：行数与视图一致，且列可直接被 duckdb/pandas 读取
    with duckdb.connect() as probe:
        got = probe.execute("SELECT count(*) FROM read_parquet(?)",
                            [str(out / "v_listing_analysis.parquet")]).fetchone()[0]
        cols = [r[0] for r in probe.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)",
            [str(out / "v_listing_analysis.parquet")]).fetchall()]
    assert got == counts["v_listing_analysis"] == 3
    for col in ("listing_id", "game_name", "price_yuan", "kw_ceiling_hits", "quality_flags"):
        assert col in cols, f"Parquet 缺列：{col}"


def test_export_game_filter_and_notes(seeded: cfg.Settings, tmp_path: Path) -> None:
    A.prepare_analysis(seeded)
    out = tmp_path / "analysis-wuwa"
    exported = A.export_views(seeded, out, fmt="csv", game_id=10302)
    rows = {item["view"]: item["rows"] for item in exported["views"]}
    assert rows["v_listing_analysis"] == 1, "只导出鸣潮的行"
    assert rows["v_keyword_hits"] == 2, "鸣潮的两条命中"
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["game_id"] == 10302
    assert not any("过滤未作用于" in note for note in manifest["notes"]), \
        f"所有视图都带 game_id，不应出现跳过说明：{manifest['notes']}"


def test_export_main_csv_single_file_with_bom(seeded: cfg.Settings, tmp_path: Path) -> None:
    """一份 CSV：一行一个 listing，UTF-8 带 BOM（Excel 友好），列=分析主表全部列。"""
    A.prepare_analysis(seeded)
    out = tmp_path / "one.csv"
    result = A.export_main_csv(seeded, out)
    raw = out.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "必须带 UTF-8 BOM（否则 Excel 中文乱码）"
    assert result["rows"] == 3 and result["column_count"] == len(result["columns"])
    with db.connect(seeded.paths.db, read_only=True) as conn:
        view_cols = list(conn.table(A.MAIN_CSV_VIEW).columns)
    assert result["columns"] == view_cols, "CSV 列必须与分析主表一一对应（不裁剪不隐藏）"
    # 用标准库读回：列名/行数/中文字段值正确
    import csv
    import io
    text = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 3
    a1 = next(r for r in rows if r["listing_id"] == "A1")
    assert a1["game_name"] == "原神" and float(a1["price_yuan"]) == 120.0
    assert "满命" in a1["hit_keywords"] and "死邮" in a1["floor_keywords"]
    w3 = next(r for r in rows if r["listing_id"] == "W3")
    assert w3["price_yuan"] == "" and "price_missing" in w3["quality_flags"], \
        "NULL 写空字段；质量标记随行"
    lines = A.export_main_csv_lines(result)
    assert any("已导出 3 行" in line for line in lines)
    assert any("utf-8-sig" in line for line in lines), "提示里要给出 pandas 读法"


def test_export_main_csv_game_filter(seeded: cfg.Settings, tmp_path: Path) -> None:
    A.prepare_analysis(seeded)
    result = A.export_main_csv(seeded, tmp_path / "wuwa.csv", game_id=10302)
    assert result["rows"] == 1 and result["game_id"] == 10302
    assert [g["game_name"] for g in result["per_game"]] == ["鸣潮"]


def test_format_report_mentions_gaps(seeded: cfg.Settings) -> None:
    result = A.prepare_analysis(seeded)
    text = "\n".join(A.format_report(result))
    assert "v_listing_analysis" in text and "样本门槛" in text
    assert "下架事实表为空" in text, "缺口清单必须点出下架侧信号缺失"
    assert "鸣潮" in text and "原神" in text
