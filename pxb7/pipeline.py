"""单任务一轮编排：风控前置 → 采集 → 解析 → 词表抽取 → 增量合并 → 幂等入库 → summary。

summary.json 顶层键名照抄契约（不得改名）：
    run_id, task_id, status(completed|aborted|partial), collected_via(guest|login), pages,
    cards_seen, cards_parsed, parse_success_rate, extract_hit_rate, snapshots_inserted,
    new_listings, price_changes, delist_events, detail_fetched, risk_trigger(string|null),
    raw_dir, duration_s

指标定义（契约，不得为达标而放宽）：
- ``cards_seen`` = 本轮识别到的卡片总数；
- ``cards_parsed`` = 解析出价格且至少 3 个其他字段的卡片数（parser 判定）；
- ``parse_success_rate`` = cards_parsed / cards_seen（分母 0 记 0）；
- ``extract_hit_rate`` = 标题/卡片**文本**命中 ≥1 词表关键词的 listing 数 / cards_parsed
  （分母 0 记 0）。卡片字段通道（原石/纠缠之源直取）写入特征与桥表但**不**计入该口径，
  另在 extraction_report 以 card_field_coverage 披露——否则指标被结构性数值稀释
  （终审发现项 #13/#38）。

增量合并口径：
- 新 listing → dim_listing（first_seen/last_seen）；
- 与**上一轮快照** diff → 价格变化写 fct_price_change（ts=本轮轮次）；
- 本轮未见且 last_seen 早于在售窗口 → fct_delist_event，**下架≠成交**（可能撤牌/平台下架），
  且仅在「本轮完整覆盖」时推断（风控终止 / 只入 raw / 翻页未生效 / 有被拒页时一律不推断）；
- 幂等：快照按 (snapshot_at 截断到轮次, listing_id) 先删后插，同轮重跑不产生重复。

风控：任何情况下都写 summary.json；风控终止时 status=aborted、risk_trigger 填原因、计数写实际值。
解析成功率 <80%（raw_only）时按 docs/01 §3.3 **只入 raw 层**，不写快照。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import collector as C
from . import db
from . import extract as X
from . import notify as N
from . import parser as P
from .config import ConfigError, Settings, Task, load_tasks
from .risk import (
    SIGNAL_CAPTCHA,
    SIGNAL_IP_BLOCKED,
    RateLimiter,
    RiskMachine,
)

STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_PARTIAL = "partial"

# summary.json 顶层键（契约：键名照抄，不得改名）
SUMMARY_KEYS: tuple[str, ...] = (
    "run_id", "task_id", "status", "collected_via", "pages", "cards_seen", "cards_parsed",
    "parse_success_rate", "extract_hit_rate", "snapshots_inserted", "new_listings",
    "price_changes", "delist_events", "detail_fetched", "risk_trigger", "raw_dir", "duration_s",
)


class PipelineError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass
class ListingRow:
    """一条 listing 的本轮解析结果 + 词表抽取结果。"""
    listing_id: str
    title: str | None
    card: P.ParsedCard
    extraction: X.Extraction
    viewers_masked: int | None = None
    favorites_cnt: int | None = None

    def snapshot_row(self, *, snapshot_at: _dt.datetime, collected_via: str,
                     parser_version: str) -> dict[str, Any]:
        row = self.card.as_snapshot_row()
        row.update({
            "snapshot_at": snapshot_at,
            "listing_id": self.listing_id,
            "viewers_masked": self.viewers_masked,
            "favorites_cnt": self.favorites_cnt,
            "extracted_features": self.extraction.features_json(),
            "parser_version": parser_version,
            "collected_via": collected_via,
        })
        return row

    def keyword_rows(self) -> list[dict[str, Any]]:
        return [{"listing_id": self.listing_id, "keyword_id": h.keyword_id,
                 "hit_text": h.hit_text} for h in self.extraction.hits]


@dataclass
class LoadStats:
    snapshots_inserted: int = 0
    new_listings: int = 0
    price_changes: int = 0
    delist_events: int = 0
    keyword_hits: int = 0
    prev_round: _dt.datetime | None = None
    delist_inferred: bool = False
    delist_skip_reason: str | None = None
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshots_inserted": self.snapshots_inserted,
            "new_listings": self.new_listings,
            "price_changes": self.price_changes,
            "delist_events": self.delist_events,
            "keyword_hits": self.keyword_hits,
            "prev_round": self.prev_round.isoformat(timespec="seconds") if self.prev_round else None,
            "delist_inferred": self.delist_inferred,
            "delist_skip_reason": self.delist_skip_reason,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class PipelineResult:
    summary: dict[str, Any]
    listings: list[ListingRow] = _dc_field(default_factory=list)
    list_result: C.ListRunResult | None = None
    details: list[C.DetailResult] = _dc_field(default_factory=list)
    load_stats: LoadStats = _dc_field(default_factory=LoadStats)
    extraction_report: dict[str, Any] = _dc_field(default_factory=dict)
    summary_path: str | None = None
    report_path: str | None = None
    alerts_file: str | None = None
    notify: dict[str, Any] | None = None
    field_diagnostics: list[dict[str, Any]] = _dc_field(default_factory=list)
    risk_detail: dict[str, Any] | None = None

    @property
    def aborted(self) -> bool:
        return self.summary.get("status") == STATUS_ABORTED


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def build_listings(list_result: C.ListRunResult, keywords: Sequence[X.Keyword]) -> list[ListingRow]:
    """把本轮各页解析出的卡片去重后做词表抽取（listing_id 缺失的卡片无法入库，跳过并计数）。"""
    rows: list[ListingRow] = []
    seen: set[str] = set()
    for page in list_result.pages:
        if not page.parse:
            continue
        for card in page.parse.cards:
            listing_id = (card.listing_id or "").strip()
            if not listing_id or listing_id in seen:
                continue
            seen.add(listing_id)
            extraction = X.extract_listing(keywords, listing_id=listing_id,
                                           title=card.title, card_fields=card.fields)
            rows.append(ListingRow(listing_id=listing_id, title=card.title, card=card,
                                   extraction=extraction))
    return rows


def load_round(conn, settings: Settings, task: Task, *, round_ts: _dt.datetime,
               listings: Sequence[ListingRow], collected_via: str,
               infer_delist: bool = True, now: _dt.datetime | None = None,
               delist_skip_reason: str | None = None) -> LoadStats:
    """增量合并 + 幂等快照写入（同轮重跑先删后插）。"""
    now = now or _dt.datetime.now()
    stats = LoadStats()
    if not listings:
        stats.skipped_reason = "no-listings"
        return stats

    ids = [row.listing_id for row in listings]

    # 1) 新 listing 判定（入库前查询）
    flags = db.fetch_listing_flags(conn, ids)
    stats.new_listings = sum(1 for i in ids if i not in flags)

    # 2) dim_listing 更新（first_seen/last_seen/is_active）
    db.upsert_dim_listings(conn, [{"listing_id": r.listing_id, "game_id": task.game_id,
                                   "title": r.title, "seen_at": round_ts} for r in listings])

    # 3) 与上一轮快照 diff → 价格变化
    stats.prev_round = db.previous_round(conn, round_ts)
    if stats.prev_round is not None:
        prev_prices = db.fetch_snapshot_prices(conn, stats.prev_round, ids)
        changes = []
        for row in listings:
            price = row.card.fields.get("price_yuan")
            old = prev_prices.get(row.listing_id)
            if price is None or old is None:
                continue
            if float(price) != float(old):
                changes.append({"listing_id": row.listing_id, "ts": round_ts,
                                "old_price": float(old), "new_price": float(price)})
        stats.price_changes = db.write_price_changes(conn, changes)

    # 4) 幂等快照 + 关键词命中（同轮共删共插）
    snapshot_rows = [row.snapshot_row(snapshot_at=round_ts, collected_via=collected_via,
                                      parser_version=settings.parser_version)
                     for row in listings]
    keyword_rows: list[dict[str, Any]] = []
    for row in listings:
        keyword_rows.extend(row.keyword_rows())
    written = db.write_snapshot_round(conn, round_ts, snapshot_rows, keyword_rows,
                                      round_minutes=settings.snapshot_round_minutes,
                                      # batch：只删本批 listing_id。整桶 DELETE 会在
                                      # 「同一轮次桶内跑第二个任务/第二个进程」时删掉对方已写入的行
                                      scope="batch")
    stats.snapshots_inserted = written["snapshot_rows"]
    stats.keyword_hits = written["keyword_rows"]

    # 5) 下架推断（口径：下架≠成交；仅在本轮**完整覆盖切片**时）
    if infer_delist:
        cutoff = round_ts - _dt.timedelta(hours=settings.collect_opts.delist_window_hours)
        candidates = db.fetch_delist_candidates(conn, game_id=task.game_id, before_ts=cutoff)
        stats.delist_events = db.write_delist_events(conn, candidates)
        stats.delist_inferred = True
    else:
        stats.delist_skip_reason = delist_skip_reason or "not-inferred"
    return stats


def _risk_detail(machine: RiskMachine, now_callable, *, requests_issued: bool) -> dict[str, Any]:
    """风控上下文（不进 summary 顶层键；供 CLI/排障说明「为什么这轮没数据」）。"""
    state = machine.state
    return {
        "level": state.level,
        "reason": state.reason,
        "backoff_until": state.backoff_until.isoformat(timespec="seconds")
        if state.backoff_until else None,
        "backoff_remaining_s": round(state.backoff_remaining(now_callable()), 1),
        "consecutive": state.consecutive,
        "probe_mode": state.probe_mode,
        "session_slot": state.session_slot,
        "frequency_factor": state.frequency_factor,
        "raw_only": state.raw_only,
        "requests_issued": requests_issued,
    }


def _status_for(list_result: C.ListRunResult, load_stats: LoadStats) -> str:
    if list_result.aborted_by_risk:
        return STATUS_ABORTED
    if load_stats.skipped_reason == "raw-only":
        return STATUS_PARTIAL
    if list_result.rejection_count or list_result.pagination_stalled:
        return STATUS_PARTIAL
    return STATUS_COMPLETED


def persist_alerts(settings: Settings, alerts: Sequence[Mapping[str, Any]]) -> str | None:
    """A4 告警落盘（JSONL，按天分文件）：未配置 webhook 时告警也不丢。

    终审发现项 #16/#41：drain_alerts() 取走即清空，webhook 为空时告警被直接丢弃，
    三天无人值守下验证码停采/解析告警不留任何可发现痕迹。落盘路径
    ``{paths.runs}/alerts/alerts-YYYYMMDD.jsonl``，写失败不影响采集（返回 None）。
    """
    if not alerts:
        return None
    day = _dt.datetime.now().strftime("%Y%m%d")
    out_dir = Path(settings.paths.runs) / "alerts"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"alerts-{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for alert in alerts:
                fh.write(json.dumps(dict(alert), ensure_ascii=False, sort_keys=True) + "\n")
        return str(path)
    except OSError:
        return None


def _parse_rate(cards_parsed: int, cards_seen: int) -> float:
    return (cards_parsed / cards_seen) if cards_seen else 0.0


def _coverage_complete(list_result: C.ListRunResult, *, raw_only: bool,
                       page_limit: int) -> tuple[bool, str]:
    """本轮是否**完整覆盖了该任务切片**——只有完整覆盖才允许推断下架。

    任务切片（如原神官服全部在售）远大于每轮 ≤5 页的窗口，被页数上限截断的轮次里
    「没看到」只代表滑出了窗口，不代表下架；按 48h 未见就写 delist 会批量误报。
    注意（有意保守，终审发现项 #12/#37 的口径澄清）：出厂任务 filter={} 时任何
    ≤page_limit 的轮次都会判 page-cap-reached ⇒ fct_delist_event 长期为 0——这是
    **正确行为**：要在采样窗口上推断下架，必须把任务 filter 收窄到 ≤pages_per_run
    可完整覆盖的切片（并在 tasks.yaml notes 里记录），或接入按完整分页采集的专任务。
    """
    if list_result.aborted_by_risk:
        return (False, "risk-aborted")
    if list_result.rejection_count:
        return (False, "rejected-pages")
    if list_result.pagination_stalled:
        return (False, "pagination-stalled")
    if raw_only:
        return (False, "raw-only")
    pages = list_result.pages
    if not pages:
        return (False, "no-pages")
    if len(pages) >= page_limit and not pages[-1].empty:
        return (False, f"page-cap-reached({len(pages)}/{page_limit})")
    return (True, "full-slice")


def _pid_alive(pid: int) -> bool | None:
    """进程是否存活；无法判定时返回 None（由年龄阈值兜底）。

    终审发现项 #18/#43：此前仅凭 6h mtime 判陈旧——进程被硬杀（关机/任务终止）后，
    残留锁会让后续采集在 6h 内以「另一个采集进程正在运行」失败，形成无人值守空窗。
    Windows 下 OpenProcess 被拒（ACCESS_DENIED）视为存活（保守，不抢锁）。
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_ACCESS_DENIED = 5
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    code = ctypes.c_ulong()
                    if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                        return code.value == STILL_ACTIVE
                    return True                  # 查询失败：保守视为存活
                finally:
                    k32.CloseHandle(handle)
            return k32.GetLastError() == ERROR_ACCESS_DENIED
        os.kill(pid, 0)                          # POSIX：信号 0 探活
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, AttributeError, ValueError):
        return None


@contextmanager
def run_lock(settings: Settings):
    """采集串行锁：同一登录态不做多任务并发（docs/01 §3.3 登录态运行纪律）。

    allow_concurrent_tasks=true 时不加锁（留给 W2 的调度器自行串行化）。
    陈旧锁判定（终审发现项 #18）：锁内 pid 已不存活，或 mtime 超过 6h（兜底）→ 接管。
    """
    if settings.risk_control.allow_concurrent_tasks:
        yield None
        return
    lock_path = Path(settings.paths.state_dir) / "crawl.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stale_after_s = 6 * 3600
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"pid={os.getpid()} at={_dt.datetime.now().isoformat(timespec='seconds')}\n")
            break
        except FileExistsError:
            try:
                raw = lock_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = ""
            pid_m = re.search(r"pid=(\d+)", raw)
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            alive = _pid_alive(int(pid_m.group(1))) if pid_m else None
            if alive is False or age > stale_after_s:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            detail = f"pid={pid_m.group(1) if pid_m else '?'} 存活={alive}，已持有 {age / 60:.1f} 分钟"
            raise PipelineError(
                f"另一个采集进程正在运行（{lock_path}，{detail}）；同一登录态不做多任务并发")
    try:
        yield str(lock_path)
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def build_summary(*, run_id: str, task_id: str, status: str, collected_via: str,
                  list_result: C.ListRunResult | None, load_stats: LoadStats,
                  detail_fetched: int, risk_trigger: str | None, raw_dir: str | None,
                  duration_s: float, listings: Sequence[ListingRow] = ()) -> dict[str, Any]:
    """按契约键名组装 summary；指标口径见模块 docstring。"""
    pages = list_result.pages_collected if list_result else 0
    cards_seen = list_result.cards_seen if list_result else 0
    cards_parsed = list_result.cards_parsed if list_result else 0
    stats = X.summarize([row.extraction for row in listings]) if listings else X.ExtractStats()
    text_hit_listings = stats.text_hit_listings       # 契约口径：仅词表文本命中
    summary = {
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "collected_via": collected_via,
        "pages": pages,
        "cards_seen": cards_seen,
        "cards_parsed": cards_parsed,
        "parse_success_rate": round(_parse_rate(cards_parsed, cards_seen), 4),
        "extract_hit_rate": round(X.extra_hit_rate(text_hit_listings, cards_parsed), 4),
        "snapshots_inserted": load_stats.snapshots_inserted,
        "new_listings": load_stats.new_listings,
        "price_changes": load_stats.price_changes,
        "delist_events": load_stats.delist_events,
        "detail_fetched": detail_fetched,
        "risk_trigger": risk_trigger,
        "raw_dir": raw_dir,
        "duration_s": round(duration_s, 1),
    }
    missing = [k for k in SUMMARY_KEYS if k not in summary]
    extra = [k for k in summary if k not in SUMMARY_KEYS]
    if missing or extra:                      # 契约键名自检：不允许改名/缺项
        raise PipelineError(f"summary 键不符契约：missing={missing} extra={extra}")
    return summary


def write_summary(path: str | Path, summary: Mapping[str, Any]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def aggregate_field_diagnostics(list_result: C.ListRunResult | None) -> list[dict[str, Any]]:
    """跨页汇总字段级诊断（命中/缺失/命中率/样例值），供报告与 CLI 打印。"""
    hits = {f: 0 for f in P.CONTRACT_FIELDS}
    missing = {f: 0 for f in P.CONTRACT_FIELDS}
    samples: dict[str, list[dict[str, Any]]] = {f: [] for f in P.CONTRACT_FIELDS}
    for page in (list_result.pages if list_result else []):
        parsed = page.parse
        if parsed is None:
            continue
        for name in P.CONTRACT_FIELDS:
            hits[name] += parsed.field_hits.get(name, 0)
            missing[name] += parsed.field_missing.get(name, 0)
            if len(samples[name]) < 3:
                samples[name].extend(parsed.field_samples.get(name, [])[:3 - len(samples[name])])
    rows: list[dict[str, Any]] = []
    for name in P.CONTRACT_FIELDS:
        total = hits[name] + missing[name]
        rows.append({
            "field": name,
            "required": name in P.REQUIRED_FIELDS,
            "hits": hits[name],
            "missing": missing[name],
            "hit_rate": round((hits[name] / total) if total else 0.0, 4),
            "strategies": sorted({s.get("strategy") for s in samples[name] if s.get("strategy")}),
            "samples": samples[name],
        })
    return rows


def format_field_table(rows: Sequence[Mapping[str, Any]], *, indent: str = "  ") -> list[str]:
    """把字段诊断渲染成人类可读表格行（crawl / parse-raw 共用）。"""
    lines: list[str] = []
    for row in rows:
        mark = "*" if row.get("required") else " "
        samples = row.get("samples") or []
        sample_txt = " | ".join(str(s.get("value"))[:24] for s in samples[:2]) or "-"
        strategies = ",".join(row.get("strategies") or []) or "-"
        lines.append(f"{indent}{mark}{row['field']:<20} 命中 {row['hits']:>4} "
                     f"缺失 {row['missing']:>4} 率 {row['hit_rate']:.0%}  "
                     f"[{strategies}]  样例: {sample_txt}")
    if lines:
        lines.append(f"{indent}(* = 契约 required 字段)")
    return lines


def write_extraction_report(path: str | Path, *, summary: Mapping[str, Any],
                            stats: X.ExtractStats, load_stats: LoadStats,
                            list_result: C.ListRunResult | None = None,
                            diagnostics: Sequence[Mapping[str, Any]] | None = None) -> str:
    """诊断报告（不进 summary 顶层键）：仅关键词命中率、逐字段命中/样例、逐页明细。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    required_incomplete = sum(
        (p.parse.cards_required_incomplete for p in (list_result.pages if list_result else [])
         if p.parse is not None), 0)
    payload = {
        "run_id": summary.get("run_id"),
        "task_id": summary.get("task_id"),
        "parser_version": P.PARSER_VERSION,
        "extract": stats.as_dict(),
        "load": load_stats.as_dict(),
        "cards_required_incomplete": required_incomplete,
        "fields": list(diagnostics if diagnostics is not None
                       else aggregate_field_diagnostics(list_result)),
        "pages": [
            {"page_no": p.page_no, "url": p.url, "cards_seen": p.cards_seen,
             "cards_parsed": p.cards_parsed, "parse_success_rate": round(p.parse_success_rate, 4),
             "empty": p.empty, "error": p.error, "pagination_used": p.pagination_used,
             "card_wait_selector": p.card_wait_selector,
             "card_selector_used": (p.parse.card_selector_used if p.parse else None),
             "risk_signals": list(p.signals),
             "failing_examples": (p.parse.failing_examples() if p.parse else [])}
            for p in (list_result.pages if list_result else [])
        ],
        "notes": [
            "extract_hit_rate 为契约口径（标题/卡片文本命中 ≥1 词表关键词）；"
            "card_field_coverage 披露 [卡] 字段通道（原石/纠缠之源直取）的覆盖面——"
            "该通道写入特征与桥表，但不计入契约口径，防止结构性数值稀释指标",
            "fields[].samples 为该字段命中样例（含命中策略名），用于按真实 DOM 校准 parser 选择器",
            "cards_required_incomplete = 有 listing_id 但缺契约 required 字段的卡片数（进 QC 缺失率）",
        ],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


# --------------------------------------------------------------------------- #
# 一轮编排
# --------------------------------------------------------------------------- #
def run_once(*, settings: Settings, task: Task | None = None, pages: int | None = None,
             detail: int = 0, mode: str | None = None, session=None,
             machine: RiskMachine | None = None, limiter: RateLimiter | None = None,
             keywords: Sequence[X.Keyword] | None = None, conn=None,
             run_id: str | None = None, summary_path: str | Path | None = None,
             clock=None, notify_result: bool = True) -> PipelineResult:
    """跑一轮（可注入 session/machine/limiter/conn 以便离线测试）。"""
    task = task or load_tasks(settings=settings).by_id("genshin_official")
    keywords = list(keywords) if keywords is not None else X.seed_keywords()
    machine = machine or RiskMachine(settings)
    limiter = limiter or RateLimiter(settings, machine=machine)
    now = clock or _dt.datetime.now
    started = now()
    run_id = run_id or C.new_run_id(task.task_id, now=started)

    owns_session = session is None
    owns_conn = conn is None
    session_obj = session
    db_conn = conn
    load_stats = LoadStats()
    listings: list[ListingRow] = []
    details: list[C.DetailResult] = []
    list_result: C.ListRunResult | None = None
    stats = X.ExtractStats()
    lock_ctx = None
    lock_held = False

    try:
        # 风控前置闸门：**先检查、再启动浏览器**——退避未到期直接终止，本轮不产生任何请求，
        # 也不启动 chromium（check_before_run 只调用一次，避免状态机被推进两次）。
        decision = machine.check_before_run(task.task_id)
        if not decision.allow:
            list_result = C.aborted_round(
                task, settings=settings, run_id=run_id, decision=decision, now=now,
                collected_via=getattr(session_obj, "collected_via", "guest"),
                session_slot=getattr(session_obj, "slot", None))
            summary = build_summary(
                run_id=run_id, task_id=task.task_id, status=STATUS_ABORTED,
                collected_via=list_result.collected_via, list_result=list_result,
                load_stats=load_stats, detail_fetched=0,
                risk_trigger=list_result.risk_signal, raw_dir=None,
                duration_s=(now() - started).total_seconds())
            result = PipelineResult(summary=summary, list_result=list_result,
                                    load_stats=load_stats, field_diagnostics=[])
            result.risk_detail = _risk_detail(machine, now, requests_issued=False)
            if summary_path:
                result.summary_path = write_summary(summary_path, summary)
            alerts = machine.drain_alerts()
            result.alerts_file = persist_alerts(settings, alerts)
            if notify_result:
                try:
                    result.notify = N.notify_health(
                        summary, settings=settings, alerts=alerts).as_dict()
                except Exception as exc:
                    result.notify = {"sent": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            return result

        # 串行锁：同一登录态不做多任务并发（docs/01 §3.3 登录态运行纪律）
        lock_ctx = run_lock(settings)
        lock_ctx.__enter__()
        lock_held = True

        # v1.3 登录态优先：状态槽位为 guest 而**可用登录态现已出现**（首次导出/重新登录）
        # 时，升级回主登录态（2026-10-03 试运行发现：槽位 guest 是“当时无登录态”的遗留，
        # 会让 crawl 无视可用的 storage_primary 继续以游客态运行并被 WAF 拦截）。
        # 主备被踢的降级不受影响：那会写入 observe_until（72h 观察），观察期内不升级。
        if mode is None and machine.state.session_slot == "guest" \
                and not machine.state.observe_until:
            from .browser import choose_session
            intent = choose_session(settings)
            if intent.collected_via == "login":
                machine.sync_session_slot(intent.slot)   # 记录升级历史并更新档位

        if session_obj is None:
            from .browser import BrowserSession
            # 用状态机记录的实际槽位建会话：主登录态被踢后，下一轮自动改用备用/游客
            session_obj = BrowserSession(settings, slot=machine.state.session_slot, mode=mode)
            session_obj.start()
        # 把「实际使用的槽位」回写状态机（游客态就该记 guest，避免档位与数据来源不一致）
        machine.sync_session_slot(getattr(session_obj, "slot", "guest"))
        if db_conn is None:
            db_conn = db.connect(settings.paths.db)

        limiter.wait_between_tasks()          # 任务间 ≥60s（首轮不等待）

        list_result = C.collect_list(task, settings=settings, session=session_obj,
                                     machine=machine, limiter=limiter,
                                     run_id=run_id, pages=pages, clock=now,
                                     decision=decision)
        listings = build_listings(list_result, keywords)
        stats = X.summarize([row.extraction for row in listings])

        # 详情页（M5：正在浏览/收藏）；仅在明确要求且会话可用时
        if detail and settings.collect_opts.detail_enabled and not list_result.aborted_by_risk:
            for row in listings[:int(detail)]:
                detail_res = C.collect_detail(
                    row.listing_id, settings=settings, session=session_obj,
                    task_id=task.task_id, run_id=run_id,
                    raw_dir=Path(list_result.raw_dir) if list_result.raw_dir else None, now=now)
                details.append(detail_res)
                row.viewers_masked = detail_res.viewers_masked
                row.favorites_cnt = detail_res.favorites_cnt
                # 详情页同样会命中风控：把信号喂给状态机，命中即停采该任务（§3.3）
                if detail_res.signals:
                    if C.SIGNAL_LOGIN_KICKED in detail_res.signals \
                            and getattr(session_obj, "collected_via", "guest") == "login":
                        # 登录态在详情页看到登录墙 ⇒ 该登录态已失效：按 §3.3 主→备→游客
                        # 逐级降级（此前只有列表页接了这条链，终审发现项 #8/#33）。
                        via = C.handle_login_invalid(
                            machine, session_obj, task_id=task.task_id,
                            reason="详情页出现登录墙标记（登录态失效）")
                        list_result.aborted_by_risk = True
                        list_result.risk_signal = C.SIGNAL_LOGIN_KICKED
                        list_result.risk_level = machine.state.level
                        list_result.risk_reason = (
                            f"详情页登录态失效 → 已切至 {getattr(session_obj, 'slot', 'guest')}"
                            f"（collected_via={via}）")
                        break
                    C._apply_risk_signals(machine, detail_res.signals, task_id=task.task_id,
                                          settings=settings, limiter=limiter, session=session_obj)
                    if C.SIGNAL_CAPTCHA in detail_res.signals \
                            or C.SIGNAL_IP_BLOCKED in detail_res.signals:
                        list_result.aborted_by_risk = True
                        list_result.risk_signal = (
                            C.SIGNAL_IP_BLOCKED if C.SIGNAL_IP_BLOCKED in detail_res.signals
                            else C.SIGNAL_CAPTCHA)
                        list_result.risk_level = machine.state.level
                        list_result.risk_reason = f"详情页命中风控：{detail_res.url}"
                        break

        # 入库（raw_only 时按 §3.3 只留 raw，不写快照）
        if machine.state.raw_only:
            load_stats.skipped_reason = "raw-only"
        elif listings:
            round_ts = db.truncate_to_round(list_result.started_at or started,
                                            settings.snapshot_round_minutes)
            page_limit = limiter.max_pages(pages if pages is not None else task.pages_per_run)
            coverage_ok, coverage_reason = _coverage_complete(
                list_result, raw_only=machine.state.raw_only, page_limit=page_limit)
            load_stats = load_round(
                db_conn, settings, task, round_ts=round_ts, listings=listings,
                collected_via=list_result.collected_via,
                infer_delist=coverage_ok, now=now(),
                delist_skip_reason=None if coverage_ok else coverage_reason)

        finished = now()
        status = _status_for(list_result, load_stats)
        summary = build_summary(
            run_id=run_id, task_id=task.task_id, status=status,
            collected_via=list_result.collected_via, list_result=list_result,
            load_stats=load_stats, detail_fetched=len(details),
            risk_trigger=list_result.risk_signal, raw_dir=list_result.raw_dir_actual,
            duration_s=(finished - started).total_seconds(), listings=listings)
        if lock_held:
            lock_ctx.__exit__(None, None, None)
            lock_held = False
    except Exception:
        if lock_held:
            lock_ctx.__exit__(None, None, None)
            lock_held = False
        if session_obj is not None and owns_session:
            session_obj.stop()
        if db_conn is not None and owns_conn:
            db_conn.close()
        raise

    result = PipelineResult(summary=summary, listings=listings, list_result=list_result,
                            details=details, load_stats=load_stats,
                            field_diagnostics=aggregate_field_diagnostics(list_result))
    # 风控上下文（不进 summary 顶层键；供 CLI/排障说明「为什么这轮没数据」）
    result.risk_detail = _risk_detail(machine, now,
                                     requests_issued=bool(list_result.pages))

    base_dir = Path(summary_path).parent if summary_path else (
        Path(summary["raw_dir"]) if summary["raw_dir"] else settings.paths.raw_root)
    if summary_path:
        result.summary_path = write_summary(summary_path, summary)
    # 每轮都往 raw 目录写一份 summary.json：QC / 复核以文件为准，不依赖调用方传参
    if summary["raw_dir"]:
        try:
            write_summary(Path(summary["raw_dir"]) / "summary.json", summary)
        except OSError:
            pass
    result.extraction_report = stats.as_dict()
    try:
        result.report_path = write_extraction_report(
            base_dir / f"extraction_report_{run_id}.json", summary=summary, stats=stats,
            load_stats=load_stats, list_result=list_result,
            diagnostics=result.field_diagnostics)
    except OSError:
        result.report_path = None

    alerts = machine.drain_alerts()
    result.alerts_file = persist_alerts(settings, alerts)
    if notify_result:
        try:
            notify_res = N.notify_health(summary, settings=settings, alerts=alerts)
            result.notify = notify_res.as_dict()
        except Exception as exc:              # 通知失败绝不影响采集结果
            result.notify = {"sent": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    if db_conn is not None and owns_conn:
        db_conn.close()
    if session_obj is not None and owns_session:
        session_obj.stop()
    return result


# --------------------------------------------------------------------------- #
# 离线重放
# --------------------------------------------------------------------------- #
def find_raw_runs(settings: Settings, *, task_id: str, date: str | None = None) -> list[Path]:
    """列出 raw 目录下某任务（某日）的 run 目录。"""
    base = Path(settings.paths.raw_root) / task_id
    if not base.is_dir():
        return []
    days = [base / date] if date else sorted(p for p in base.iterdir() if p.is_dir())
    runs: list[Path] = []
    for day in days:
        if day.is_dir():
            runs.extend(sorted(p for p in day.iterdir() if p.is_dir()))
    return runs


def replay_raw_dir(settings: Settings, *, run_dir: str | Path, task: Task | None = None,
                   conn=None, keywords: Sequence[X.Keyword] | None = None,
                   infer_delist: bool = False) -> PipelineResult:
    """离线重放：解析 raw 目录里的列表页 HTML 并重新入库（不发起任何请求）。

    - 轮次时间取元数据 collected_at（缺失则回退文件名/目录 mtime）；
    - 默认 **不推断下架**（重放不代表当前在售状态）；
    - 幂等：同轮重跑先删后插，重复重放不产生重复行；
    - 与 crawl 共用串行锁：重放同样写 DuckDB（单写者模型），与采集并发会写冲突
      （终审发现项 #19/#44）。
    """
    with run_lock(settings):
        return _replay_raw_dir_locked(settings, run_dir=run_dir, task=task, conn=conn,
                                      keywords=keywords, infer_delist=infer_delist)


def _replay_raw_dir_locked(settings: Settings, *, run_dir: str | Path,
                           task: Task | None = None, conn=None,
                           keywords: Sequence[X.Keyword] | None = None,
                           infer_delist: bool = False) -> PipelineResult:
    run_path = Path(run_dir)
    if not run_path.is_dir():
        raise PipelineError(f"raw run 目录不存在：{run_path}")
    task = task or load_tasks(settings=settings).by_id("genshin_official")
    keywords = list(keywords) if keywords is not None else X.seed_keywords()
    owns_conn = conn is None
    db_conn = conn or db.connect(settings.paths.db)

    started = _dt.datetime.now()
    try:
        pages: list[C.ListPageResult] = []
        round_ts: _dt.datetime | None = None
        for html_path in sorted(run_path.glob("list_p*.html")):
            meta_path = html_path.with_suffix(".json")
            meta: dict[str, Any] = {}
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {}
            html = html_path.read_text(encoding="utf-8", errors="replace")
            parsed = P.parse_list_page(html, url=meta.get("url"),
                                       parser_version=settings.parser_version)
            # 风控识别**重新做一遍**（不信任 meta）：即便元数据缺失/失真的拦截页，
            # 重放也必须认出来，绝不能当成正常列表页入库。
            meta_signals = tuple(str(s) for s in (meta.get("risk_signals") or []))
            detected = C._collect_page_signals(html)
            signals = tuple(dict.fromkeys([*meta_signals, *detected]))
            if signals:
                print(f"[parse-raw] {html_path.name}: 识别到风控特征 {list(signals)}"
                      f"（cards_seen={parsed.cards_seen}）")
            collected_at = meta.get("collected_at")
            if collected_at and round_ts is None:
                try:
                    round_ts = _dt.datetime.fromisoformat(str(collected_at))
                except ValueError:
                    round_ts = None
            pages.append(C.ListPageResult(
                page_no=int(meta.get("page_no") or len(pages) + 1),
                url=str(meta.get("url") or html_path.name),
                status=meta.get("http_status"), ok=bool(meta.get("ok", True)),
                cards_seen=parsed.cards_seen, cards_parsed=parsed.cards_parsed,
                parse_success_rate=parsed.parse_success_rate, empty=parsed.cards_seen == 0,
                error=meta.get("error"), signals=signals,
                raw=C.RawDump(html_path=str(html_path), meta_path=str(meta_path),
                              page_type="list"),
                parse=parsed))
        if not pages:
            raise PipelineError(f"目录中没有 list_p*.html：{run_path}")

        detail_map: dict[str, dict[str, Any]] = {}
        for meta_path in sorted(run_path.glob("detail_*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if meta.get("listing_id"):
                detail_map[str(meta["listing_id"])] = meta

        round_ts = round_ts or _dt.datetime.fromtimestamp(run_path.stat().st_mtime)
        round_ts = db.truncate_to_round(round_ts, settings.snapshot_round_minutes)
        collected_via = "guest"          # raw 元数据缺失时的保守默认（不冒充登录态）
        for meta_path in sorted(run_path.glob("list_p*.json")):
            try:
                meta_via = json.loads(meta_path.read_text(encoding="utf-8")).get("collected_via")
            except json.JSONDecodeError:
                continue
            if meta_via:
                collected_via = str(meta_via)
                break

        list_result = C.ListRunResult(
            task_id=task.task_id, run_id=run_path.name, collected_via=collected_via,
            session_slot="guest", frequency_factor=1.0, raw_dir=str(run_path),
            pages=pages, started_at=round_ts, finished_at=_dt.datetime.now(),
            duration_s=(_dt.datetime.now() - started).total_seconds())

        # 拦截页（验证码/滑块/IP 拦截）绝不当正常页入库：直接以 aborted 结束，不写任何行
        load_stats = LoadStats()
        risk_signals = sorted({s for page in pages for s in page.signals
                               if s in (SIGNAL_CAPTCHA, SIGNAL_IP_BLOCKED)})
        if risk_signals:
            load_stats.skipped_reason = f"risk-page:{risk_signals[0]}"
            list_result.aborted_by_risk = True
            list_result.risk_signal = risk_signals[0]
            list_result.risk_reason = f"重放识别到风控页面：{','.join(risk_signals)}"
            listings = []
        else:
            listings = build_listings(list_result, keywords)
            for row in listings:
                meta = detail_map.get(row.listing_id)
                if meta:
                    row.viewers_masked = meta.get("viewers_masked")
                    row.favorites_cnt = meta.get("favorites_cnt")

        if listings:
            load_stats = load_round(db_conn, settings, task, round_ts=round_ts, listings=listings,
                                    collected_via=collected_via, infer_delist=infer_delist)
        elif not load_stats.skipped_reason:
            load_stats.skipped_reason = "no-listings"

        summary = build_summary(
            run_id=run_path.name, task_id=task.task_id,
            status=(STATUS_ABORTED if risk_signals else
                    (STATUS_PARTIAL if load_stats.skipped_reason else STATUS_COMPLETED)),
            collected_via=collected_via, list_result=list_result, load_stats=load_stats,
            detail_fetched=len(detail_map), risk_trigger=(risk_signals[0] if risk_signals else None),
            raw_dir=str(run_path),
            duration_s=(_dt.datetime.now() - started).total_seconds(), listings=listings)
    finally:
        if owns_conn:
            db_conn.close()

    stats = X.summarize([row.extraction for row in listings])
    result = PipelineResult(summary=summary, listings=listings, list_result=list_result,
                            load_stats=load_stats, extraction_report=stats.as_dict(),
                            field_diagnostics=aggregate_field_diagnostics(list_result))
    result.summary_path = str(write_summary(run_path / "summary.replay.json", summary))
    try:
        result.report_path = write_extraction_report(
            run_path / "extraction_report.replay.json", summary=summary, stats=stats,
            load_stats=load_stats, list_result=list_result,
            diagnostics=result.field_diagnostics)
    except OSError:
        result.report_path = None
    return result
