"""每日 QC 与 W1 验收指标（docs/01 §5 QC、§8 排期与验收）。

QC 检查项（docs/01 §5「QC（每日自动，结果进通知）」）：
1. 价格非负；
2. 快照断档 ≤36h；
3. 卡片解析成功率 ≥80%；
4. 关键字段缺失率（**文档未给阈值** → 只报告不判定，见 policy 口径注释）；
5. listing_id 去重校验。

W1 验收四指标（docs/01 §8）：
- 连续 3 天采集成功率 ≥95%（成功轮次 = 完成且真正取到页面）；
- 解析成功率 ≥85%；
- 词表抽取命中率 ≥70%；
- 风控触发次数 = 0。

数据来源：各轮 summary.json（raw 目录内 / data/runs 下）+ DuckDB 快照表。
只读，不发任何请求。
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb

from . import db
from .config import PROJECT_ROOT, Settings

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_INFO = "info"
STATUS_SKIP = "skip"

# docs/01 §5 / §8 的阈值（照抄，不放松）
SNAPSHOT_GAP_MAX_HOURS = 36.0
PARSE_SUCCESS_MIN = 0.80            # 改版信号线（<80% 只入 raw 层）
W1_SUCCESS_RATE_MIN = 0.95
W1_PARSE_RATE_MIN = 0.85
W1_EXTRACT_HIT_MIN = 0.70
W1_WINDOW_DAYS = 3
REQUIRED_KEY_FIELDS = ("publish_time_text", "level", "yellow_cnt", "five_star_chars",
                       "five_star_weapons", "server", "mail_status", "featured_chars")


@dataclass
class QcCheck:
    name: str
    status: str
    detail: str
    value: Any = None
    target: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "value": self.value, "target": self.target}


@dataclass
class W1Metrics:
    window_days: int = W1_WINDOW_DAYS
    rounds_total: int = 0
    rounds_ok: int = 0
    days_covered: int = 0
    success_rate: float = 0.0
    parse_success_rate: float = 0.0
    extract_hit_rate: float = 0.0
    risk_triggers: int = 0
    evaluations: list[dict[str, Any]] = _dc_field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return bool(self.evaluations) and all(e["pass"] for e in self.evaluations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days, "rounds_total": self.rounds_total,
            "rounds_ok": self.rounds_ok, "days_covered": self.days_covered,
            "success_rate": round(self.success_rate, 4),
            "parse_success_rate": round(self.parse_success_rate, 4),
            "extract_hit_rate": round(self.extract_hit_rate, 4),
            "risk_triggers": self.risk_triggers,
            "evaluations": self.evaluations, "all_pass": self.all_pass,
        }


@dataclass
class QcReport:
    generated_at: _dt.datetime
    checks: list[QcCheck]
    w1: W1Metrics
    sources: list[str]
    db_path: str

    @property
    def ok(self) -> bool:
        return all(c.status != STATUS_FAIL for c in self.checks) and self.w1.all_pass

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "ok": self.ok, "db_path": self.db_path, "sources": self.sources,
            "checks": [c.as_dict() for c in self.checks], "w1": self.w1.as_dict(),
        }


# --------------------------------------------------------------------------- #
# 数据来源
# --------------------------------------------------------------------------- #
def find_summaries(settings: Settings, *, runs_dir: str | Path | None = None,
                   now: _dt.datetime | None = None,
                   window_days: int = W1_WINDOW_DAYS) -> list[tuple[Path, dict[str, Any]]]:
    """收集窗口内的轮次 summary（raw 目录内的 summary*.json + paths.runs 下的 *.json）。

    终审发现项 #10/#40：此前只匹配 ``summary*.json``，而 CLI ``--summary-json`` 写出的
    文件名（smoke.json / gate-1.json…）不在命中范围，W1 指标无法从提交物复算。
    现对 runs 目录做全量 ``*.json`` 扫描，并按 summary 契约键（run_id + cards_seen /
    parse_success_rate）过滤——QC 报告、extraction_report 等非 summary 文件不会混入。
    """
    now = now or _dt.datetime.now()
    cutoff = now - _dt.timedelta(days=window_days)
    sources: list[tuple[Path, str]] = []
    if runs_dir:
        sources.append((Path(runs_dir), "flat"))
    else:
        sources.append((Path(settings.paths.raw_root), "rglob"))
        sources.append((Path(settings.paths.runs), "flat"))
    found: dict[str, tuple[Path, dict[str, Any]]] = {}
    for base, mode in sources:
        if not base.is_dir():
            continue
        paths = base.rglob("summary*.json") if mode == "rglob" else base.glob("*.json")
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or "run_id" not in payload:
                continue
            if "cards_seen" not in payload and "parse_success_rate" not in payload:
                continue          # 非 summary（QC 报告 / extraction_report 等）
            stamp = _summary_time(path, payload)
            if stamp and stamp < cutoff:
                continue
            found[str(payload.get("run_id"))] = (path, payload)
    return sorted(found.values(), key=lambda item: _summary_time(item[0], item[1])
                  or _dt.datetime.min)


def _summary_time(path: Path, payload: dict[str, Any]) -> _dt.datetime | None:
    for key in ("collected_at", "started_at", "finished_at"):
        raw = payload.get(key)
        if raw:
            try:
                return _dt.datetime.fromisoformat(str(raw))
            except ValueError:
                pass
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def evaluate_w1(summaries: Sequence[dict[str, Any]], *, window_days: int = W1_WINDOW_DAYS,
                max_risk_triggers: int = 0,
                days_covered: int | None = None) -> W1Metrics:
    """按 docs/01 §8 的 W1 四指标汇总（口径与 summary 契约一致）。

    证据门槛（终审发现项 #11/#36：空样本/单轮不得判通过）：
    - 「连续 3 天采集成功率 ≥95%」要求窗口内覆盖满 ``window_days`` 天，不足则该项
      不判通过（注明样本不足）；
    - 解析/命中率在样本为 0 时不判通过；0 轮次时**全部**指标（含风控触发）不判通过。
    """
    metrics = W1Metrics(window_days=window_days, rounds_total=len(summaries))
    cards_seen = cards_parsed = hit_listings = 0
    for item in summaries:
        pages = int(item.get("pages") or 0)
        if item.get("status") == "completed" and pages > 0:
            metrics.rounds_ok += 1
        seen = int(item.get("cards_seen") or 0)
        parsed = int(item.get("cards_parsed") or 0)
        cards_seen += seen
        cards_parsed += parsed
        # extract_hit_rate 口径为「词表文本命中数 / cards_parsed」→ 反推命中数后按样本量
        # 加权聚合。这里不做四舍五入（summary 里的比率已保留 4 位小数），避免逐轮取整
        # 引入偏差；精确命中数见各轮 raw 目录下的 extraction_report（extract.text_hit_listings）。
        try:
            hit_listings += float(item.get("extract_hit_rate") or 0.0) * parsed
        except (TypeError, ValueError):
            pass
        if item.get("risk_trigger"):
            metrics.risk_triggers += 1

    if days_covered is None:
        days_covered = len({str(item.get("collected_at") or "")[:10]
                            for item in summaries if item.get("collected_at")})
    metrics.days_covered = int(days_covered)

    metrics.success_rate = (metrics.rounds_ok / metrics.rounds_total) if metrics.rounds_total else 0.0
    metrics.parse_success_rate = (cards_parsed / cards_seen) if cards_seen else 0.0
    metrics.extract_hit_rate = (hit_listings / cards_parsed) if cards_parsed else 0.0

    def add(name: str, actual: float, target: float, *, ge: bool = True,
            ok: bool | None = None, note: str | None = None) -> None:
        if ok is None:
            ok = (actual >= target) if ge else (actual <= target)
        source = "summary.json 聚合" if metrics.rounds_total else "窗口内无轮次数据"
        if note:
            source = f"{source}；{note}"
        metrics.evaluations.append({
            "name": name, "value": round(actual, 4), "target": target,
            "comparison": ">=" if ge else "<=", "pass": bool(ok), "source": source})

    days_ok = metrics.rounds_total > 0 and metrics.days_covered >= window_days
    add("采集成功率", metrics.success_rate, W1_SUCCESS_RATE_MIN,
        ok=bool(days_ok and metrics.success_rate >= W1_SUCCESS_RATE_MIN),
        note=None if days_ok else
        f"「连续 {window_days} 天」口径要求覆盖 {window_days} 天，实际 {metrics.days_covered} 天")
    add("解析成功率", metrics.parse_success_rate, W1_PARSE_RATE_MIN,
        ok=bool(cards_seen > 0 and metrics.parse_success_rate >= W1_PARSE_RATE_MIN),
        note=None if cards_seen > 0 else "无卡片样本，不判通过")
    add("词表抽取命中率", metrics.extract_hit_rate, W1_EXTRACT_HIT_MIN,
        ok=bool(cards_parsed > 0 and metrics.extract_hit_rate >= W1_EXTRACT_HIT_MIN),
        note=None if cards_parsed > 0 else "无已解析样本，不判通过")
    add("风控触发次数", float(metrics.risk_triggers), float(max_risk_triggers), ge=False,
        ok=bool(metrics.rounds_total > 0 and metrics.risk_triggers <= max_risk_triggers),
        note=None if metrics.rounds_total > 0 else "无轮次数据，不判通过")
    return metrics


# --------------------------------------------------------------------------- #
# QC 检查
# --------------------------------------------------------------------------- #
def _check_prices(conn: duckdb.DuckDBPyConnection) -> QcCheck:
    negative = conn.execute(
        "SELECT count(*) FROM fct_listing_snapshot WHERE price_yuan IS NOT NULL AND price_yuan < 0"
    ).fetchone()[0]
    nulls = conn.execute(
        "SELECT count(*) FROM fct_listing_snapshot WHERE price_yuan IS NULL").fetchone()[0]
    total = conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0]
    if negative:
        return QcCheck("价格非负", STATUS_FAIL, f"{negative} 行价格为负", negative, 0)
    return QcCheck("价格非负", STATUS_PASS,
                   f"无负价；价格为空 {nulls}/{total} 行（缺失如实保留 NULL，不填 0）",
                   negative, 0)


def _check_snapshot_gap(conn: duckdb.DuckDBPyConnection, now: _dt.datetime,
                        max_hours: float = SNAPSHOT_GAP_MAX_HOURS) -> QcCheck:
    latest = db.latest_round(conn)
    if latest is None:
        return QcCheck("快照断档", STATUS_FAIL, "库里没有任何快照（无法评估断档）", None,
                       f"≤{max_hours:g}h")
    gap_h = (now - latest).total_seconds() / 3600.0
    detail = f"最近轮次 {latest.isoformat(timespec='seconds')}，距今 {gap_h:.1f}h"
    return QcCheck("快照断档", STATUS_PASS if gap_h <= max_hours else STATUS_FAIL, detail,
                   round(gap_h, 2), f"≤{max_hours:g}h")


def _check_key_fields(conn: duckdb.DuckDBPyConnection) -> QcCheck:
    latest = db.latest_round(conn)
    if latest is None:
        return QcCheck("关键字段缺失率", STATUS_SKIP, "无快照可评估", None, "文档未给阈值")
    total = conn.execute(
        "SELECT count(*) FROM fct_listing_snapshot WHERE snapshot_at = ?", [latest]).fetchone()[0]
    if not total:
        return QcCheck("关键字段缺失率", STATUS_SKIP, "最近轮次快照为空", None, "文档未给阈值")
    missing: dict[str, float] = {}
    for field in REQUIRED_KEY_FIELDS:
        if field not in _allowed_snapshot_columns(conn):
            continue
        nulls = _count_nulls(conn, field, latest)
        missing[field] = round(nulls / total, 4)
    worst = sorted(missing.items(), key=lambda kv: -kv[1])[:3]
    return QcCheck("关键字段缺失率", STATUS_INFO,
                   f"最近轮次 n={total}；缺失率 top3：" +
                   "、".join(f"{k}={v:.0%}" for k, v in worst) +
                   "（docs 未规定阈值，故只报告不判定）",
                   missing, "文档未给阈值")


_ALLOWED_COLUMNS_CACHE: dict[str, set[str]] = {}


def _allowed_snapshot_columns(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """列名白名单（来自 information_schema），杜绝把外部字符串拼进 SQL。"""
    if "snapshot" not in _ALLOWED_COLUMNS_CACHE:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'fct_listing_snapshot'").fetchall()
        _ALLOWED_COLUMNS_CACHE["snapshot"] = {str(r[0]) for r in rows}
    return _ALLOWED_COLUMNS_CACHE["snapshot"]


def _count_nulls(conn: duckdb.DuckDBPyConnection, field: str,
                 snapshot_at: _dt.datetime) -> int:
    """统计某列在最近轮次的空值数：列名经白名单校验后逐列静态查询。"""
    queries = {
        "publish_time_text": "SELECT count(*) FROM fct_listing_snapshot"
                             " WHERE snapshot_at = ? AND publish_time_text IS NULL",
        "level": "SELECT count(*) FROM fct_listing_snapshot"
                 " WHERE snapshot_at = ? AND level IS NULL",
        "yellow_cnt": "SELECT count(*) FROM fct_listing_snapshot"
                      " WHERE snapshot_at = ? AND yellow_cnt IS NULL",
        "five_star_chars": "SELECT count(*) FROM fct_listing_snapshot"
                           " WHERE snapshot_at = ? AND five_star_chars IS NULL",
        "five_star_weapons": "SELECT count(*) FROM fct_listing_snapshot"
                             " WHERE snapshot_at = ? AND five_star_weapons IS NULL",
        "server": "SELECT count(*) FROM fct_listing_snapshot"
                  " WHERE snapshot_at = ? AND server IS NULL",
        "mail_status": "SELECT count(*) FROM fct_listing_snapshot"
                       " WHERE snapshot_at = ? AND mail_status IS NULL",
        "featured_chars": "SELECT count(*) FROM fct_listing_snapshot"
                          " WHERE snapshot_at = ? AND featured_chars IS NULL",
    }
    sql = queries.get(field)
    if sql is None:
        return 0
    return int(conn.execute(sql, [snapshot_at]).fetchone()[0])


def _check_dedup(conn: duckdb.DuckDBPyConnection) -> QcCheck:
    dup_listings = conn.execute(
        "SELECT count(*) - count(DISTINCT listing_id) FROM dim_listing").fetchone()[0]
    dup_snapshots = conn.execute(
        "SELECT count(*) - count(DISTINCT (snapshot_at, listing_id)) FROM fct_listing_snapshot"
    ).fetchone()[0]
    if dup_listings or dup_snapshots:
        return QcCheck("listing_id 去重", STATUS_FAIL,
                       f"重复：dim_listing {dup_listings}，快照 {dup_snapshots}", None, 0)
    return QcCheck("listing_id 去重", STATUS_PASS, "无重复 listing_id / 快照键", 0, 0)


def _check_parse_rate(summaries: Sequence[dict[str, Any]]) -> QcCheck:
    seen = sum(int(s.get("cards_seen") or 0) for s in summaries)
    parsed = sum(int(s.get("cards_parsed") or 0) for s in summaries)
    if not seen:
        return QcCheck("卡片解析成功率", STATUS_SKIP,
                       "窗口内没有卡片样本（无数据即不评估，不伪造）", None,
                       f"≥{PARSE_SUCCESS_MIN:.0%}")
    rate = parsed / seen
    return QcCheck("卡片解析成功率",
                   STATUS_PASS if rate >= PARSE_SUCCESS_MIN else STATUS_FAIL,
                   f"{parsed}/{seen} 张解析成功（<{PARSE_SUCCESS_MIN:.0%} 视为站点改版信号）",
                   round(rate, 4), f"≥{PARSE_SUCCESS_MIN:.0%}")


def run_qc(settings: Settings, *, conn: duckdb.DuckDBPyConnection | None = None,
           runs_dir: str | Path | None = None, window_days: int = W1_WINDOW_DAYS,
           now: _dt.datetime | None = None) -> QcReport:
    """执行 QC 与 W1 汇总（只读；不发请求）。"""
    now = now or _dt.datetime.now()
    owns = conn is None
    db_conn = conn or db.connect(settings.paths.db, read_only=True)
    try:
        checks = [
            _check_prices(db_conn),
            _check_snapshot_gap(db_conn, now),
            _check_dedup(db_conn),
            _check_key_fields(db_conn),
        ]
    finally:
        if owns:
            db_conn.close()

    summaries = find_summaries(settings, runs_dir=runs_dir, now=now, window_days=window_days)
    checks.append(_check_parse_rate([s for _, s in summaries]))
    stamps = [t for t in (_summary_time(p, s) for p, s in summaries) if t is not None]
    days_covered = len({t.date() for t in stamps})
    w1 = evaluate_w1([s for _, s in summaries], window_days=window_days,
                     days_covered=days_covered)
    return QcReport(generated_at=now, checks=checks, w1=w1,
                    sources=[str(p) for p, _ in summaries], db_path=str(settings.paths.db))


def format_report(report: QcReport) -> list[str]:
    lines = [f"[qc] {report.generated_at.isoformat(timespec='seconds')} "
             f"db={report.db_path} 轮次样本={len(report.sources)} 总体={'通过' if report.ok else '未通过'}"]
    for check in report.checks:
        mark = {"pass": "✔", "fail": "✘", "warn": "!", "info": "i", "skip": "-"}.get(
            check.status, "?")
        target = f"（目标 {check.target}）" if check.target is not None else ""
        lines.append(f"[qc] {mark} {check.name}: {check.detail}{target}")
    w1 = report.w1
    lines.append(f"[qc] W1 验收（{w1.window_days} 天窗口）：轮次 {w1.rounds_ok}/{w1.rounds_total} 成功"
                 f"，解析率 {w1.parse_success_rate:.2%}，命中率 {w1.extract_hit_rate:.2%}"
                 f"，风控触发 {w1.risk_triggers} 次")
    for item in w1.evaluations:
        lines.append(f"[qc]   {'✔' if item['pass'] else '✘'} {item['name']} "
                     f"{item['value']} {item['comparison']} {item['target']}"
                     f"（{item['source']}）")
    return lines
