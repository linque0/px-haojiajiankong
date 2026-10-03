"""登录态增益实测（docs/01 §3.1-2 v1.3 与 §8 W1 验收项）。

要测的三项增益（docs/01 §3.1-2）：
1. **单页条数上限**：登录态单页卡片数是否高于游客态；
2. **「正在浏览」完整值**：登录态是否拿到完整数值（游客态打码）；
3. **收藏等需求侧字段可见性**：登录态能否取到收藏数。

实现：同一任务分别以 guest / login 跑一轮（各 ≤pages 页、可选详情页），对比三项指标，
产出报告 JSON。**不做任何绕过**；无可用登录态时直接判定 skipped，绝不伪造增益。
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import collector as C
from . import pipeline as PL
from .config import Settings, Task, load_tasks

STATUS_MEASURED = "measured"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"


@dataclass
class ModeProbe:
    mode: str                       # guest | login
    collected_via: str
    session_slot: str
    run_id: str
    pages: int = 0
    cards_seen: int = 0
    cards_parsed: int = 0
    max_cards_per_page: int = 0
    detail_fetched: int = 0
    viewers_visible: int = 0        # 取到完整数值的详情页数
    viewers_masked: int = 0         # 打码/不可得
    favorites_visible: int = 0
    listings_sampled: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "collected_via": self.collected_via,
            "session_slot": self.session_slot, "run_id": self.run_id,
            "pages": self.pages, "cards_seen": self.cards_seen,
            "cards_parsed": self.cards_parsed,
            "max_cards_per_page": self.max_cards_per_page,
            "cards_per_page": round(self.cards_seen / self.pages, 2) if self.pages else 0.0,
            "detail_fetched": self.detail_fetched,
            "viewers_visible": self.viewers_visible, "viewers_masked": self.viewers_masked,
            "favorites_visible": self.favorites_visible,
            "listings_sampled": self.listings_sampled,
        }


@dataclass
class SessionGainReport:
    task_id: str
    status: str
    reason: str | None
    measured_at: _dt.datetime
    guest: ModeProbe | None = None
    login: ModeProbe | None = None
    gains: dict[str, Any] = _dc_field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "status": self.status, "reason": self.reason,
            "measured_at": self.measured_at.isoformat(timespec="seconds"),
            "guest": self.guest.as_dict() if self.guest else None,
            "login": self.login.as_dict() if self.login else None,
            "gains": self.gains,
            "note": "指标如实记录；无可用登录态时 status=skipped，不推断任何增益",
        }


def _probe_from_round(mode: str, result: PL.PipelineResult) -> ModeProbe:
    list_result = result.list_result
    pages = list_result.pages if list_result else []
    return ModeProbe(
        mode=mode,
        collected_via=result.summary.get("collected_via", "guest"),
        session_slot=getattr(list_result, "session_slot", "guest") if list_result else "guest",
        run_id=str(result.summary.get("run_id")),
        pages=result.summary.get("pages", 0),
        cards_seen=result.summary.get("cards_seen", 0),
        cards_parsed=result.summary.get("cards_parsed", 0),
        max_cards_per_page=max((p.cards_seen for p in pages), default=0),
        detail_fetched=result.summary.get("detail_fetched", 0),
        viewers_visible=sum(1 for d in result.details if d.viewers_visible),
        viewers_masked=sum(1 for d in result.details if not d.viewers_visible),
        favorites_visible=sum(1 for d in result.details if d.favorites_visible),
        listings_sampled=len(result.listings),
    )


def run_session_gain(settings: Settings, *, task: Task | None = None, pages: int = 1,
                     detail: int = 1, conn=None, now=None,
                     session_factory: Callable[[str], Any] | None = None,
                     runner: Callable[..., PL.PipelineResult] | None = None,
                     out_path: str | Path | None = None) -> SessionGainReport:
    """分别以 guest / login 跑一轮并对比增益。

    - 无可用登录态（login 轮实际仍为 guest）→ status=skipped，reason 说明原因；
    - session_factory / runner 可注入，便于离线测试（默认走真实 BrowserSession/pipeline）。
    """
    now = now or _dt.datetime.now
    task = task or load_tasks(settings=settings).by_id("genshin_official")
    runner = runner or PL.run_once

    def _run(mode: str) -> PL.PipelineResult:
        session = session_factory(mode) if session_factory else None
        return runner(settings=settings, task=task, pages=pages, detail=detail,
                      mode=mode, session=session, conn=conn,
                      run_id=f"gain-{mode}-{now().strftime('%Y%m%dT%H%M%S')}",
                      notify_result=False)

    guest_result = _run("guest")
    guest = _probe_from_round("guest", guest_result)
    login_result = _run(None)          # None = 自动选（主→备→游客）
    login = _probe_from_round("login", login_result)

    if login.collected_via != "login":
        report = SessionGainReport(
            task_id=task.task_id, status=STATUS_SKIPPED,
            reason="无可用登录态（该轮实际以游客态运行）→ 无法测量增益，未做任何绕过",
            measured_at=now(), guest=guest, login=login)
    else:
        report = SessionGainReport(
            task_id=task.task_id, status=STATUS_MEASURED, reason=None, measured_at=now(),
            guest=guest, login=login,
            gains={
                "max_cards_per_page": {
                    "guest": guest.max_cards_per_page, "login": login.max_cards_per_page,
                    "delta": login.max_cards_per_page - guest.max_cards_per_page},
                "viewers_visible_ratio": {
                    "guest": _ratio(guest.viewers_visible, guest.detail_fetched),
                    "login": _ratio(login.viewers_visible, login.detail_fetched)},
                "favorites_visible_ratio": {
                    "guest": _ratio(guest.favorites_visible, guest.detail_fetched),
                    "login": _ratio(login.favorites_visible, login.detail_fetched)},
            })
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return report


def _ratio(hit: int, total: int) -> float:
    return round(hit / total, 4) if total else 0.0


def format_report(report: SessionGainReport) -> list[str]:
    lines = [f"[session-gain] task={report.task_id} status={report.status} "
             f"({report.reason or 'ok'})"]
    for probe in (report.guest, report.login):
        if probe:
            lines.append(f"[session-gain]   {probe.mode:<6} via={probe.collected_via:<5} "
                         f"pages={probe.pages} cards={probe.cards_seen} "
                         f"单页最多={probe.max_cards_per_page} 详情={probe.detail_fetched} "
                         f"正在浏览可见={probe.viewers_visible}/{probe.detail_fetched} "
                         f"收藏可见={probe.favorites_visible}/{probe.detail_fetched}")
    for name, value in (report.gains or {}).items():
        lines.append(f"[session-gain]   增益 {name}: {value}")
    return lines
