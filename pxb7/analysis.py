"""分析就绪层：把采集数据整理成"可直接分析"的视图与文件（docs/01 §4「分析层」）。

职责：
1. **视图**（DDL 在 `db.ensure_analysis_views`，幂等重建）：
   - `v_listing_latest` 每号最新一轮快照（跨断面分析基座）+ 轨迹聚合（轮次数/价格区间/在售天数）
   - `v_listing_analysis` 分析主表：最新快照 + 该轮词表命中（按类型计数与命中词清单）+ 质量标记
   - `v_keyword_hits` 关键词命中长表（游戏/画像/类型/锚点 + 是否最新一轮）
   - `keyword_feature_matrix` 特征矩阵（**物化表**：行 = listing×轮次×游戏，列 = keyword_id，0/1；
     因 DuckDB 视图不支持动态 PIVOT，随本命令重建）
   - `v_listing_daily` 日粒度轨迹（价格首/末/最低/最高 + 最新浏览/收藏）
   - `v_price_index_weekly` 周聚合（游戏×区服×价格段，含样本门槛 `publishable`）
   - `v_price_band` 价格带 v0（关键词锚点分域 + 域内价格分位，样本不足 `unpublished`）
   - `v_deals` 捡漏候选 v0（规则版；非 M2 残差）
   - `v_delist_speed` 下架速度（在售天数与价格/天）
2. **导出**：每个视图写 CSV + Parquet，并生成数据字典 `README.md` 与 `manifest.json`。
   实现走 DuckDB 关系 API（`conn.table(view)` + 程序化过滤 + `write_csv/write_parquet`），
   导出路径与应用层筛选都不拼接 SQL。
3. **质量摘要**：每游戏行数/最新轮次/价格缺失/词表覆盖/单轮占比 + 已知缺口清单，
   让人一眼看出"这批数据现在能做什么、不能做什么"。

口径纪律（docs/02 §6「不伪造」）：样本不足一律不发布——`v_price_band` 给 `unpublished`、
周聚合给 `publishable=false`；`v_deals` 是 v0 规则候选，只作人工复核线索，不是模型结论。
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Mapping

import duckdb

from . import db
from . import extract as X
from .config import Settings

# --------------------------------------------------------------------------- #
# 视图字典（数据字典与文档共用；列口径只写“需要解释”的那些，其余见 docs/01 §4）
# --------------------------------------------------------------------------- #
VIEW_SPECS: dict[str, dict[str, Any]] = {
    "v_listing_latest": {
        "title": "每号最新一轮快照",
        "desc": "每个 listing 取最新一轮快照（跨断面分析的基座），并附带轨迹聚合。",
        "game_filterable": True,
        "columns": {
            "snapshot_rounds": "该 listing 被采到的轮次数（1 = 只有单轮，轨迹分析不足）",
            "price_min/price_max": "历史快照里的最低/最高挂牌价（挂牌≠成交，docs/01 §4）",
            "days_on_market": "first_seen → 本轮快照的在售天数（需 ≥2 轮才有意义）",
            "feat_*": "从 extracted_features JSON 展开的常用抽取特征（如命座/精炼）",
            "publish_time": "上架时间（卡片文本解析，TRY_CAST 失败为 NULL）",
            "collected_via": "login = 登录态浏览采到（插件通道）；guest = 降级通道",
        },
    },
    "v_listing_analysis": {
        "title": "分析主表（最新态 + 词表命中 + 质量标记）",
        "desc": "给分析/建模用的宽表：一行一个 listing，含词表命中统计与质量标记。",
        "game_filterable": True,
        "columns": {
            "kw_hits/kw_*_hits": "该轮命中的关键词数（按 ceiling/floor/resource/risk/segment 分列）",
            "ceiling_keywords/floor_keywords": "命中的天花板词/折价词清单（顿号分隔，归因用）",
            "quality_flags": "逗号分隔的数据质量标记：price_missing / price_nonpositive /"
                             " yellow_lt_5star_sum（黄数 < 五星角色+武器）/ inactive / single_round",
            "snapshot_age_h": "本轮快照距现在的小时数（>48h 属陈旧，仅供参考）",
        },
    },
    "v_keyword_hits": {
        "title": "关键词命中长表",
        "desc": "一行一条命中（listing × 关键词 × 轮次），做词频/相关性分析用。",
        "game_filterable": True,
        "columns": {
            "keyword_type": "ceiling/floor/risk/resource/segment（docs/02 §3）",
            "price_anchor": "该词是否价格锚点（ceiling/floor/none）",
            "hit_text": "命中到的原文片段（可追溯，不猜）",
            "is_latest_round": "是否该 listing 的最新一轮命中",
        },
    },
    "keyword_feature_matrix": {
        "title": "特征矩阵（物化表，0/1）",
        "desc": "行 = (listing, 轮次, 游戏)，列 = keyword_id；直接可当回归特征矩阵。"
                "物化表：词表/数据更新后重跑 prepare-analysis 刷新。",
        "game_filterable": True,
        "columns": {
            "<keyword_id>": "1 = 该轮命中该关键词；列为 NULL 表示该词表画像里没有此词",
        },
    },
    "v_listing_daily": {
        "title": "日粒度轨迹",
        "desc": "每天每号的价格首/末/最低/最高与最新浏览/收藏（需求侧为登录态取值）。",
        "game_filterable": True,
        "columns": {
            "price_first/price_last": "当天第一/最后一轮的价格（提价/降价的日界面对比）",
            "viewers_last/favorites_last": "当天最后一轮的「正在浏览/收藏」（详情页字段，可能未公示）",
        },
    },
    "v_price_index_weekly": {
        "title": "周聚合（游戏×区服×价格段）",
        "desc": "计数/中位数/四分位；样本 < 门槛 的行 publishable=false（不发布）。",
        "game_filterable": True,
        "columns": {
            "publishable": "listings ≥ 门槛（默认 30，settings.quality.min_cell_sample）",
            "method": "口径说明；链式指数需 ≥4 周历史，本版本只发聚合量（docs/01 §5）",
        },
    },
    "v_price_band": {
        "title": "价格带 v0（关键词锚点 + 域内价格分位）",
        "desc": "先按关键词锚点分域（ceiling/floor/normal），再在「游戏×域」内按价格分位定带。",
        "game_filterable": True,
        "columns": {
            "keyword_domain": "floor（命中折价词）/ ceiling（命中天花板词，且无折价词）/ normal",
            "price_pctl_in_domain": "域内价格分位（0–100）",
            "price_band": "floor / low / fair / high / ceiling；域样本不足 → unpublished",
            "band_basis": "price_level —— 价格水平分位；M2 hedonic 残差分位就绪后替换（docs/02 §6）",
            "domain_n": "该「游戏×域」的样本量（判断能不能信这个带）",
        },
    },
    "v_deals": {
        "title": "捡漏候选 v0（规则版）",
        "desc": "底价/偏低带且样本过门槛的候选；含折价词会给 caution。非模型结论。",
        "game_filterable": True,
        "columns": {
            "caution": "含折价词（渠道服/死邮等）时提示：低价可能来自折价项而非机会",
            "method": "v0_rule：待 M2 残差口径上线后替换（docs/02 §6）",
        },
    },
    "v_delist_speed": {
        "title": "下架速度",
        "desc": "在售天数与价格/天。插件通道覆盖不完整 → 下架推断固定关闭，通常为空（设计如此）。",
        "game_filterable": True,
        "columns": {
            "days_on_market": "first_seen → last_seen 的天数（下架≠成交，只是推断口径）",
            "price_per_day": "last_price / days_on_market（粗略的去化速度）",
        },
    },
}

DISCLAIMERS: tuple[str, ...] = (
    "挂牌价 ≠ 成交价：所有价格均为页面挂牌价（docs/01 §4）。",
    "下架 ≠ 一定成交：可能是撤牌/平台下架；本项目的 delist 只作成交的**推断**口径。",
    "样本不足不发布：v_price_band 的 `unpublished` 与 v_price_index_weekly 的"
    " `publishable=false` 都是这个纪律（docs/02 §6）。",
    "插件通道覆盖不完整：同一 listing 的轮次数取决于你是否浏览到它，跨号比较前先看 snapshot_rounds。",
)


# --------------------------------------------------------------------------- #
# 准备（建/重建视图）+ 质量摘要
# --------------------------------------------------------------------------- #
def view_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """各分析视图行数（关系 API，不拼 SQL）。"""
    counts: dict[str, int] = {}
    for name in db.ANALYSIS_LAYER:
        counts[name] = int(conn.table(name).aggregate("count(*) AS n").fetchone()[0])
    return counts


def quality_summary(conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """质量摘要 + 已知缺口：先看这批数据"能做什么"。"""
    min_sample = conn.execute(
        "SELECT coalesce(CAST(value AS INTEGER), ?) FROM meta_params WHERE key = ?",
        [db.MIN_CELL_SAMPLE_DEFAULT, db.MIN_CELL_SAMPLE_PARAM]).fetchone()
    min_sample = int(min_sample[0]) if min_sample else db.MIN_CELL_SAMPLE_DEFAULT

    tables = db.table_counts(conn)
    games = [{
        "game_id": row[0], "game_name": row[1], "listings": row[2],
        "price_median": float(row[3]) if row[3] is not None else None,
        "price_missing": row[4], "kw_covered": row[5], "single_round": row[6],
        "latest_round": row[7].isoformat(timespec="minutes") if row[7] else None,
        "latest_age_h": float(row[8]) if row[8] is not None else None,
    } for row in conn.execute("""
        SELECT game_id, game_name, count(*) AS listings,
               median(price_yuan) AS price_median,
               sum(CASE WHEN price_yuan IS NULL THEN 1 ELSE 0 END) AS price_missing,
               sum(CASE WHEN kw_hits > 0 THEN 1 ELSE 0 END) AS kw_covered,
               sum(CASE WHEN snapshot_rounds = 1 THEN 1 ELSE 0 END) AS single_round,
               max(snapshot_at), max(snapshot_age_h)
        FROM v_listing_analysis GROUP BY 1, 2 ORDER BY 3 DESC
    """).fetchall()]

    bands = {row[0]: row[1] for row in conn.execute(
        "SELECT price_band, count(*) FROM v_price_band GROUP BY 1").fetchall()}
    weekly = conn.execute(
        "SELECT count(*), coalesce(sum(CASE WHEN publishable THEN 1 ELSE 0 END), 0)"
        " FROM v_price_index_weekly").fetchone()

    gaps: list[str] = []
    if tables.get("fct_delist_event", 0) == 0:
        gaps.append("下架事实表为空（插件通道下架推断固定关闭）：v_delist_speed / 成交侧信号暂缺，"
                    "恢复管线通道或开启覆盖完整的切片后才会有数据")
    no_profile = [g["game_name"] for g in games if g["kw_covered"] == 0]
    if no_profile:
        gaps.append("无词表命中（未建画像）的游戏：" + "、".join(no_profile)
                    + " —— 特征只有通用字段，按 docs/02 §7「待采集清单」补齐画像后再扩列")
    if bands and set(bands) == {"unpublished"}:
        gaps.append(f"v_price_band 全部 unpublished：各「游戏×关键词域」样本 < {min_sample}，"
                    "样本够了自动开始发布（不伪造）")
    if int(weekly[1] or 0) == 0:
        gaps.append("周聚合全部 publishable=false：链式指数需 ≥4 周历史（docs/01 §5），"
                    "当前只累积聚合量")
    total_listings = sum(g["listings"] for g in games)
    single_round = sum(g["single_round"] for g in games)
    if total_listings and single_round / total_listings > 0.5:
        gaps.append(f"{single_round}/{total_listings} 个 listing 只有单轮快照："
                    "价格轨迹/成交推断（M7）需要多轮，继续按轮次浏览同一批页面")

    return {
        "min_cell_sample": min_sample,
        "tables": tables,
        "games": games,
        "price_bands": bands,
        "weekly_cells": int(weekly[0] or 0),
        "weekly_publishable": int(weekly[1] or 0),
        "gaps": gaps,
    }


def prepare_analysis(settings: Settings, *, min_cell_sample: int | None = None,
                     conn: duckdb.DuckDBPyConnection | None = None) -> dict[str, Any]:
    """建/重建分析视图（幂等）并返回行数与质量摘要。

    `min_cell_sample` 缺省取 settings.quality.min_cell_sample（默认 30）。自建连接时与采集
    共用串行锁（DuckDB 单写者，DDL 也是写）。
    """
    size = int(min_cell_sample if min_cell_sample is not None
               else settings.quality.get("min_cell_sample", db.MIN_CELL_SAMPLE_DEFAULT))

    if conn is not None:
        views = db.ensure_analysis_views(conn, min_cell_sample=size)
        return {"db_path": str(settings.paths.db), "min_cell_sample": size,
                "views": views, "view_rows": view_counts(conn),
                "quality": quality_summary(conn)}

    from .pipeline import run_lock            # 延迟导入：避免 db/config 层反向依赖
    with run_lock(settings):
        with db.connect(settings.paths.db) as own:
            views = db.ensure_analysis_views(own, min_cell_sample=size)
            return {"db_path": str(settings.paths.db), "min_cell_sample": size,
                    "views": views, "view_rows": view_counts(own),
                    "quality": quality_summary(own)}


# --------------------------------------------------------------------------- #
# 导出（CSV / Parquet + 数据字典）
# --------------------------------------------------------------------------- #
def _relation_for(conn: duckdb.DuckDBPyConnection, view: str, game_id: int | None):
    """取视图关系；给了 game_id 且该视图有 game_id 列时做程序化过滤（不拼 SQL）。"""
    rel = conn.table(view)
    spec = VIEW_SPECS.get(view, {})
    if game_id is not None and spec.get("game_filterable", True) and "game_id" in rel.columns:
        rel = rel.filter(duckdb.ColumnExpression("game_id")
                         == duckdb.ConstantExpression(int(game_id)))
    return rel


def export_views(settings: Settings, out_dir: str | Path, *, fmt: str = "both",
                 game_id: int | None = None, quality: Mapping[str, Any] | None = None
                 ) -> dict[str, Any]:
    """把分析视图导出为 CSV / Parquet，并写数据字典 README.md 与 manifest.json。

    fmt: "csv" | "parquet" | "both"。game_id 只作用于含 game_id 的视图（其余视图全量导出，
    并在 README/manifest 里注明）。
    """
    if fmt not in ("csv", "parquet", "both"):
        raise ValueError(f"fmt 只能是 csv/parquet/both，实际 {fmt!r}")
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    exported: list[dict[str, Any]] = []
    with db.connect(settings.paths.db, read_only=True) as conn:
        quality = dict(quality) if quality is not None else quality_summary(conn)
        skipped_filter: list[str] = []
        for name in db.ANALYSIS_LAYER:
            rel = _relation_for(conn, name, game_id)
            if game_id is not None and "game_id" not in conn.table(name).columns:
                skipped_filter.append(name)
            rows = int(rel.aggregate("count(*) AS n").fetchone()[0])
            files: list[str] = []
            if fmt in ("csv", "both"):
                filename = f"{name}.csv"
                rel.write_csv(str(out / filename))
                files.append(filename)
            if fmt in ("parquet", "both"):
                filename = f"{name}.parquet"
                rel.write_parquet(str(out / filename))
                files.append(filename)
            exported.append({"view": name, "kind": ("table" if name in db.ANALYSIS_TABLES else "view"),
                             "title": VIEW_SPECS.get(name, {}).get("title", name),
                             "rows": rows, "files": files})

    generated_at = _dt.datetime.now().isoformat(timespec="seconds")
    man = {
        "generated_at": generated_at,
        "db_path": str(settings.paths.db),
        "out_dir": str(out),
        "format": fmt,
        "game_id": game_id,
        "min_cell_sample": quality.get("min_cell_sample") if quality else None,
        "views": exported,
        "notes": (["game_id 过滤未作用于：" + "、".join(skipped_filter)] if skipped_filter else [])
                 + ["CSV 为 UTF-8；Parquet 用 duckdb.read_parquet 或 pandas.read_parquet 读取"],
    }
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    (out / "README.md").write_text(
        render_dictionary(man=man, quality=quality or {}), encoding="utf-8")
    return {"out_dir": str(out), "format": fmt, "game_id": game_id, "views": exported,
            "files": sorted(p.name for p in out.iterdir() if p.is_file()),
            "readme": str(out / "README.md"), "manifest": str(out / "manifest.json")}


# 单份 CSV 的主表（一行 = 一个 listing 的最新一轮；列 = 该视图的全部列）
MAIN_CSV_VIEW = "v_listing_analysis"


def export_main_csv(settings: Settings, out_path: str | Path | None = None, *,
                    game_id: int | None = None,
                    quality: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把采集数据导出成**一份 CSV**（一行一个 listing：最新价 + 结构化字段 + 词表命中 + 质量标记）。

    - 编码 UTF-8 且带 BOM：Excel 双击直接打开不乱码（pandas 读用 encoding="utf-8-sig"）；
    - 列 = 分析主表 `v_listing_analysis` 的全部列（不裁剪，避免隐藏信息）；
      列名保持中性，按游戏的术语对应关系见返回的 `column_notes` 与数据字典；
    - 默认路径：`data/analysis/pxb7-listings-<日期>.csv`；`--game` 可只导一个游戏。
    """
    path = (Path(out_path).expanduser().resolve() if out_path is not None
            else (Path(settings.project_root) / "data" / "analysis"
                  / f"pxb7-listings-{_dt.datetime.now().strftime('%Y%m%d')}.csv"))
    path.parent.mkdir(parents=True, exist_ok=True)

    with db.connect(settings.paths.db, read_only=True) as conn:
        rel = _relation_for(conn, MAIN_CSV_VIEW, game_id)
        columns = list(rel.columns)
        rows = int(rel.aggregate("count(*) AS n").fetchone()[0])
        # 摘要里的按游戏分布与文件内容保持一致（--game 时只统计该游戏）
        if game_id is None:
            per_game = [{"game_id": r[0], "game_name": r[1], "rows": r[2]}
                        for r in conn.execute(
                            "SELECT game_id, game_name, count(*) FROM v_listing_analysis"
                            " GROUP BY 1, 2 ORDER BY 3 DESC").fetchall()]
        else:
            per_game = [{"game_id": r[0], "game_name": r[1], "rows": r[2]}
                        for r in conn.execute(
                            "SELECT game_id, game_name, count(*) FROM v_listing_analysis"
                            " WHERE game_id = ? GROUP BY 1, 2 ORDER BY 3 DESC",
                            [int(game_id)]).fetchall()]
        min_sample = quality.get("min_cell_sample") if quality else conn.execute(
            "SELECT CAST(value AS INTEGER) FROM meta_params WHERE key = ?",
            [db.MIN_CELL_SAMPLE_PARAM]).fetchone()
        rel.write_csv(str(path), header=True, encoding="utf-8")   # NULL 输出空字段

    # Excel 友好：补 UTF-8 BOM（DuckDB 不写 BOM，中文列名在 Excel 里会乱码）
    raw = path.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        path.write_bytes(b"\xef\xbb\xbf" + raw)

    return {
        "path": str(path),
        "rows": rows,
        "columns": columns,
        "column_count": len(columns),
        "bytes": path.stat().st_size,
        "game_id": game_id,
        "min_cell_sample": min_sample[0] if isinstance(min_sample, (list, tuple)) else min_sample,
        "per_game": per_game,
        "column_notes": {
            "price_yuan": "挂牌价（元）；≠ 成交价",
            "level/yellow_cnt/five_star_chars/five_star_weapons": "等级/黄数/五星角色数/五星武器数"
                                                             "（各游戏按自己的卡片与记法抽取）",
            "primogems/intertwined_fate": "原神：原石/纠缠之源",
            "feat_constellation_cnt/feat_five_star_weapon_refined": "抽取特征：命座数（鸣潮=共鸣链 N命）/武器精炼（精N）",
            "kw_*_hits": "该轮词表命中数（按 ceiling/floor/resource/risk/segment 分列）",
            "hit_keywords/ceiling_keywords/floor_keywords": "命中词清单（顿号分隔，归因用）",
            "quality_flags": "数据质量标记：price_missing / yellow_lt_5star_sum / single_round / inactive",
            "snapshot_rounds": "该号被采到的轮次数（1 = 还没有轨迹）",
        },
    }


def export_main_csv_lines(result: Mapping[str, Any]) -> list[str]:
    """CSV 导出结果的纯文本摘要（CLI 用）。"""
    lines = [f"[csv] 已导出 {result['rows']} 行 × {result['column_count']} 列 → {result['path']}",
             f"[csv] 体积 {result['bytes'] / 1024:.1f} KB；UTF-8 带 BOM（Excel 双击直接打开，"
             f"pandas 用 encoding='utf-8-sig'）"]
    if result.get("game_id"):
        lines.append(f"[csv] 仅包含 game_id = {result['game_id']}（样本门槛 {result['min_cell_sample']}）")
    for game in result.get("per_game", []):
        lines.append(f"[csv]   {game['game_name']}（{game['game_id']}）：{game['rows']} 行")
    notes = list(result.get("column_notes", {}).items())[:4]
    if notes:
        lines.append("[csv] 列口径摘录：" + "；".join(f"{k}={v}" for k, v in notes)
                     + " …（完整口径见数据字典 README.md 或 docs/01 §4）")
    lines.append("[csv] 提示：一行 = 一个 listing 的最新一轮；轨迹看 snapshot_rounds 与 "
                 "price_min/price_max，价格带/捡漏候选见 prepare-analysis 的 v_price_band / v_deals")
    return lines


# --------------------------------------------------------------------------- #
# 按游戏拆分 CSV（每游戏一份表）
# --------------------------------------------------------------------------- #
_UNCLASSIFIED_LABEL = "未分类"
_INVALID_FILENAME_RE = re.compile(r'[\\/:*?"<>|\s]+')


def _game_label(row: Mapping[str, Any]) -> str:
    """分组标签：游戏名-game_id（缺名退回 id，都缺 = 未分类，不丢行）。"""
    name = str(row.get("game_name") or "").strip()
    gid = str(row.get("game_id") or "").strip()
    if name and gid:
        return f"{name}-{gid}"
    return name or gid or _UNCLASSIFIED_LABEL


def split_csv_by_game(csv_path: str | Path, *,
                      out_dir: str | Path | None = None) -> dict[str, Any]:
    """把一份 pxb7-listings CSV 按**游戏**拆成多个表（每游戏一份 CSV）。

    - 分组键：`game_name`+`game_id`（缺名退回 id；两者都缺的行进 `*-未分类.csv`，不丢行）；
    - 输出默认源文件同级的 `by_game/` 目录，文件名 `<源名>-<游戏标签>.csv`；
    - 列与列序与源文件完全一致（不裁剪）；编码 UTF-8 带 BOM（Excel 双击直接打开，
      pandas 读用 encoding="utf-8-sig"，与 export_main_csv 同约定）。
    """
    src = Path(csv_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"找不到要拆分的 CSV：{src}")
    target = (Path(out_dir).expanduser().resolve() if out_dir
              else src.parent / "by_game")
    target.mkdir(parents=True, exist_ok=True)

    with src.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in reader:
            buckets.setdefault(_game_label(row), []).append(row)

    if not fieldnames:
        raise ValueError(f"CSV 没有表头（列）：{src}")

    written: list[dict[str, Any]] = []
    for label, rows in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        safe = _INVALID_FILENAME_RE.sub("_", label).strip("_") or _UNCLASSIFIED_LABEL
        out = target / f"{src.stem}-{safe}.csv"
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        # 标签里的 name/gid 拆回摘要字段（未分类行两者皆空）
        name, _, gid = safe.rpartition("-")
        written.append({"file": str(out), "label": safe, "rows": len(rows),
                        "game_name": name or None, "game_id": gid or None})

    return {"source": str(src), "out_dir": str(target), "rows": sum(len(r) for r in buckets.values()),
            "column_count": len(fieldnames), "columns": fieldnames, "files": written}


def split_csv_lines(result: Mapping[str, Any]) -> list[str]:
    """拆分结果的纯文本摘要（CLI 用）。"""
    lines = [f"[split] 源 {result['rows']} 行 × {result['column_count']} 列 → "
             f"{len(result['files'])} 份表 → {result['out_dir']}"]
    for item in result["files"]:
        lines.append(f"[split]   {item['label']}：{item['rows']} 行 → {Path(item['file']).name}")
    lines.append("[split] 每份均为 UTF-8 带 BOM、列与源文件一致；pandas 读："
                 "pd.read_csv(r'<文件>', encoding='utf-8-sig')")
    return lines


# --------------------------------------------------------------------------- #
# 按游戏「看板列」精选导出（2026-10-04 用户指令：鸣潮数据表按看板列呈现）
# 列名/取值口径 = 看板「数据浏览」该游戏的列（gateway._GAME_COLUMNS）+ 基础列；
# 单元格文本与 dashboard.html cellText 同口径：链 "N命名称"、精炼 "精N名称"、
# 资源/付费商品 "名称：值"（资源 0 是值照列，未采到的省略；空值不伪装成 0，docs/10 §5.1）。
# 取数走关系 API（不拼接 SQL，同本模块导出口径）。
# --------------------------------------------------------------------------- #

# 鸣潮（10302）：基础列 + 该游戏自己的词表说法（docs/02 §4.A4；列序 = 用户指定表头）
WUWA_CURATED_HEADERS: tuple[str, ...] = (
    "listing_id", "游戏", "价格 ¥", "等级", "黄数", "五星角色", "五星武器",
    "共鸣链（N命）", "武器精炼（精N）", "资源", "额外付费商品", "区服",
    "商品发布时间", "收藏",
)

# 精选版式取的数据列（对全部游戏同一组，版式按游戏登记；未登记拒绝导出，不硬套别家术语）
_CURATED_COLUMNS: tuple[str, ...] = (
    "listing_id", "game_name", "price_yuan", "level", "yellow_cnt", "five_star_chars",
    "five_star_weapons", "feat_constellation_cnt", "feat_five_star_weapon_refined",
    "extracted_features", "server", "publish_time", "favorites_cnt",
)


def _roster_text(entries: Any, *, prefix: str = "", suffix: str = "") -> str | None:
    """具名清单 [{name, value}] → "6命长离、0命守岸人"（suffix=命）/ "精1千古洑流"（prefix=精）。

    与 dashboard.html cellText 同口径：链 = 值在前（"6命长离"），精炼 = "精" 在前（"精1音曦"）。
    """
    if not entries:
        return None
    parts = [f"{prefix}{e.get('value')}{suffix}{e.get('name')}"
             for e in entries if e.get("name")]
    return "、".join(parts) or None


def _fmt_wuwa_cells(row: Mapping[str, Any]) -> list[Any]:
    """一行 v_listing_analysis → 鸣潮看板列单元格（缺失回退与 dashboard 同口径）。"""
    features = json.loads(row["extracted_features"]) if row["extracted_features"] else {}
    publish = row["publish_time"]
    return [
        row["listing_id"],
        row["game_name"],
        row["price_yuan"],
        row["level"] if row["level"] is not None else features.get("account_level_cnt"),
        row["yellow_cnt"],
        row["five_star_chars"] if row["five_star_chars"] is not None
        else features.get("five_star_chars_cnt"),
        row["five_star_weapons"] if row["five_star_weapons"] is not None
        else features.get("five_star_weapons_cnt"),
        _roster_text(features.get("five_star_character_chains"), suffix="命")
        or features.get("constellation_cnt")
        or row["feat_constellation_cnt"],
        _roster_text(features.get("five_star_weapon_refinements"), prefix="精")
        or features.get("five_star_weapon_refined")
        or row["feat_five_star_weapon_refined"],
        "；".join(f"{label}：{features.get(key)}"
                  for key, label in X.WUWA_RESOURCES if features.get(key) is not None) or None,
        "；".join(f"{label}：{'、'.join(str(item) for item in features.get(key) or [])}"
                  for key, label in X.WUWA_PAID_ITEMS if features.get(key)) or None,
        row["server"],
        publish.isoformat(sep=" ", timespec="seconds") if publish is not None else None,
        row["favorites_cnt"],
    ]


CURATED_GAME_LAYOUTS: dict[int, tuple[tuple[str, ...], Any]] = {
    10302: (WUWA_CURATED_HEADERS, _fmt_wuwa_cells),   # 鸣潮；其他游戏按用户指令登记
}


def export_curated_csv(settings: Settings, out_path: str | Path | None = None, *,
                       game_id: int | None = None) -> dict[str, Any]:
    """按游戏导出「看板列」精选 CSV（一行一个 listing 的最新态，列 = 用户指定的看板口径）。

    - 编码 UTF-8 带 BOM；与 export_main_csv 同源（v_listing_analysis），列做精选与重命名；
    - `game_id` 必填且必须已登记版式（CURATED_GAME_LAYOUTS），否则明确报错不猜测；
    - 默认路径：`data/analysis/by_game/pxb7-listings-<日期>-<游戏名>-<game_id>-看板列.csv`。
    """
    if game_id is None:
        raise ValueError("看板列导出需要 --game（版式按游戏登记，不做通用猜测）")
    spec = CURATED_GAME_LAYOUTS.get(int(game_id))
    if spec is None:
        registered = "、".join(str(g) for g in sorted(CURATED_GAME_LAYOUTS))
        raise ValueError(f"game_id={game_id} 未登记看板列版式（已登记：{registered}）")
    headers, render = spec

    with db.connect(settings.paths.db, read_only=True) as conn:
        rel = _relation_for(conn, MAIN_CSV_VIEW, game_id)
        rel = rel.select(*[duckdb.ColumnExpression(c) for c in _CURATED_COLUMNS])
        rel = rel.order("TRY_CAST(listing_id AS BIGINT) DESC")
        names = list(rel.columns)
        rows_raw = rel.fetchall()
        game_name = (_relation_for(conn, MAIN_CSV_VIEW, game_id)
                     .aggregate("max(game_name) AS game_name").fetchone()[0]) or str(int(game_id))

    if out_path is None:
        out_path = (Path(settings.project_root) / "data" / "analysis" / "by_game"
                    / f"pxb7-listings-{_dt.datetime.now().strftime('%Y%m%d')}"
                      f"-{game_name}-{int(game_id)}-看板列.csv")
    path = Path(out_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = [render(dict(zip(names, row))) for row in rows_raw]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rendered)

    return {"path": str(path), "rows": len(rendered), "columns": list(headers),
            "column_count": len(headers), "game_id": int(game_id),
            "game_name": game_name, "bytes": path.stat().st_size}


def export_curated_csv_lines(result: Mapping[str, Any]) -> list[str]:
    """看板列导出结果的纯文本摘要（CLI 用）。"""
    return [
        f"[csv] 已导出 {result['rows']} 行 × {result['column_count']} 列 → {result['path']}",
        f"[csv] {result['game_name']}（{result['game_id']}）看板列版式；体积 "
        f"{result['bytes'] / 1024:.1f} KB；UTF-8 带 BOM（pandas 读 encoding='utf-8-sig'）",
        "[csv] 口径：一行 = 一个 listing 的最新一轮；资源只列采到的项（0 是值），"
        "链/精炼优先具名清单，缺失回退计数；空单元格 = 未采到（docs/10 §5.1）",
    ]


def render_dictionary(*, man: Mapping[str, Any], quality: Mapping[str, Any]) -> str:
    """生成导出目录的数据字典（分析同学先读这个）。"""
    out = man.get("out_dir", "")
    lines: list[str] = [
        "# 分析数据字典（pxb7 采集 → 分析就绪层）",
        "",
        f"- 生成时间：{man.get('generated_at')}",
        f"- 数据来源库：`{man.get('db_path')}`",
        f"- 导出目录：`{out}`　格式：{man.get('format')}",
        f"- 样本门槛 `min_cell_sample`：{man.get('min_cell_sample')}"
        "（settings.quality.min_cell_sample，样本不足的行标 unpublished / publishable=false）",
    ]
    if man.get("game_id"):
        lines.append(f"- 已按 game_id = {man['game_id']} 过滤（不支持的视图见 manifest.notes）")
    lines += ["", "## 怎么读", "",
              "```python", "import duckdb",
              f"con = duckdb.connect()  # 或直接连库：duckdb.connect(r'{man.get('db_path')}', read_only=True)",
              f"df = con.sql(\"SELECT * FROM read_parquet('{out}/v_listing_analysis.parquet')\").df()",
              "```", ""]

    lines += ["## 视图清单", "", "| 视图 | 行数 | 说明 |", "|---|---|---|"]
    for item in man.get("views", []):
        spec = VIEW_SPECS.get(item["view"], {})
        lines.append(f"| `{item['view']}` | {item['rows']} | {spec.get('desc', '')} |")

    for item in man.get("views", []):
        spec = VIEW_SPECS.get(item["view"])
        if not spec:
            continue
        lines += ["", f"### `{item['view']}` —— {spec['title']}", "", spec["desc"], ""]
        if spec.get("columns"):
            lines += ["| 列 | 口径 |", "|---|---|"]
            for col, note in spec["columns"].items():
                lines.append(f"| `{col}` | {note} |")

    if quality:
        lines += ["", "## 本批数据的质量摘要", ""]
        for game in quality.get("games", []):
            lines.append(f"- {game['game_name']}（{game['game_id']}）：{game['listings']} 个 listing，"
                         f"最新轮次 {game['latest_round']}，价格缺失 {game['price_missing']}，"
                         f"词表有命中 {game['kw_covered']}，单轮快照 {game['single_round']}")
        if quality.get("price_bands"):
            lines.append("- 价格带分布：" + "、".join(
                f"{k}={v}" for k, v in quality["price_bands"].items()))
        if quality.get("gaps"):
            lines += ["", "### 已知缺口（先看这个再决定分析口径）", ""]
            lines += [f"- {gap}" for gap in quality["gaps"]]

    lines += ["", "## 口径披露", ""] + [f"- {d}" for d in DISCLAIMERS]
    lines += ["", "> 视图定义在 `pxb7/db.py::ensure_analysis_views`（重建：`run.py init-db` 或 "
                  "`run.py prepare-analysis`）；词表更新后 `keyword_feature_matrix` 的列集会随之变化。", ""]
    return "\n".join(lines)


def format_report(result: Mapping[str, Any]) -> list[str]:
    """CLI 打印用（纯文本）。"""
    lines = [f"[analysis] 库：{result['db_path']}；样本门槛：{result['min_cell_sample']}"]
    rows = result.get("view_rows", {})
    for name in db.ANALYSIS_LAYER:
        title = VIEW_SPECS.get(name, {}).get("title", "")
        lines.append(f"  {name:<26} {rows.get(name, '—'):>7} 行  {title}")
    quality = result.get("quality") or {}
    lines.append("[analysis] 按游戏：")
    for game in quality.get("games", []):
        lines.append(f"  {game['game_name']}（{game['game_id']}）：listing {game['listings']}，"
                     f"最新轮次 {game['latest_round']}，价格缺失 {game['price_missing']}，"
                     f"词表命中覆盖 {game['kw_covered']}，单轮 {game['single_round']}")
    if quality.get("price_bands"):
        lines.append("[analysis] 价格带：" + "、".join(
            f"{k}={v}" for k, v in quality["price_bands"].items()))
    for gap in quality.get("gaps", []):
        lines.append(f"[analysis][缺口] {gap}")
    return lines
