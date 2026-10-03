"""通知推送：飞书 / 钉钉群机器人 webhook（A4 采集健康告警）。

约定（合规红线）：
- **凭据只从环境变量读取**，配置为空即静默跳过、不报错、不重试；
  settings.yaml 的 ``notify.webhook_url`` 保持空字符串，仓库内不写任何可用凭据；
- 环境变量：``PXB7_NOTIFY_WEBHOOK``（兜底）、``PXB7_NOTIFY_BUSINESS``（A1–A3/A5 业务）、
  ``PXB7_NOTIFY_OPS``（A4 采集健康，本模块默认渠道）；
- 发送前过 URL 守卫（仅 http/https、拒绝 localhost/环回/私有/保留地址）——
  webhook 也是出网请求，同样适用红线；
- 推送内容只含指标与原因，不含登录态、cookie、原始 DOM。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field as _dc_field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import Settings, ENV_NOTIFY_WEBHOOK
from .urlguard import UnsafeUrlError, assert_safe_url

DEFAULT_TIMEOUT_S = 10.0
MAX_TEXT_LEN = 1800

CHANNEL_OPS = "ops"          # A4 采集健康
CHANNEL_BUSINESS = "business"  # A1–A3/A5 业务预警


@dataclass
class NotifyResult:
    sent: bool
    skipped_reason: str | None = None
    channel: str | None = None
    status: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"sent": self.sent, "skipped_reason": self.skipped_reason,
                "channel": self.channel, "status": self.status, "error": self.error}


def resolve_webhook(settings: Settings, channel: str = CHANNEL_OPS) -> tuple[str, str]:
    """解析 webhook 地址：环境变量优先（业务/运维分渠道），其次 settings（默认留空）。"""
    notify = settings.notify
    env_name = (str(notify.get("business_channel_env") or "PXB7_NOTIFY_BUSINESS")
                if channel == CHANNEL_BUSINESS
                else str(notify.get("ops_channel_env") or "PXB7_NOTIFY_OPS"))
    url = (os.environ.get(env_name) or os.environ.get(ENV_NOTIFY_WEBHOOK)
           or str(notify.get("webhook_url") or ""))
    return url.strip(), env_name


def build_payload(text: str, *, channel: str = CHANNEL_OPS) -> dict[str, Any]:
    """飞书/钉钉通用的 text 消息体（两者字段兼容）。"""
    title = "pxb7 采集健康" if channel == CHANNEL_OPS else "pxb7 业务预警"
    return {"msg_type": "text",
            "content": {"text": f"[{title}] {text[:MAX_TEXT_LEN]}"}}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向：302 到内网/环回地址是最常见的 SSRF 绕过手法。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None          # 返回 None → urllib 不跟随，直接抛出该 3xx


def _build_opener() -> Any:
    """默认 opener：禁重定向（webhook 重定向一律不跟随）。"""
    return urllib.request.build_opener(_NoRedirectHandler)


def send_text(text: str, *, settings: Settings, channel: str = CHANNEL_OPS,
              opener: Callable[..., Any] | None = None,
              timeout: float = DEFAULT_TIMEOUT_S) -> NotifyResult:
    """推送一条文本；配置为空/地址非法时静默跳过（不抛异常）。"""
    url, env_name = resolve_webhook(settings, channel)
    if not url:
        return NotifyResult(False, skipped_reason=f"未配置 webhook（{env_name} 为空）", channel=channel)
    try:
        # 出网请求同样过守卫：解析 DNS 并拒绝内网/环回/保留地址（不只查字面 host）
        assert_safe_url(url, require_https=False, resolve_dns=True)
    except UnsafeUrlError as exc:
        return NotifyResult(False, skipped_reason=f"webhook 未通过 URL 守卫：{exc}", channel=channel)

    body = json.dumps(build_payload(text, channel=channel), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    open_url = opener or _build_opener().open
    try:
        with open_url(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
        return NotifyResult(True, channel=channel, status=int(status))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        return NotifyResult(False, channel=channel, error=f"{type(exc).__name__}: {exc}"[:200])


def format_health_alert(summary: Mapping[str, Any], alerts: Sequence[Mapping[str, Any]] = ()) -> str:
    """把一轮 summary + 风控告警拼成一条 A4 健康告警文本。"""
    lines = [
        f"run_id={summary.get('run_id')} task={summary.get('task_id')} status={summary.get('status')}",
        f"pages={summary.get('pages')} cards_seen={summary.get('cards_seen')} "
        f"parse_rate={summary.get('parse_success_rate')} extract_hit_rate={summary.get('extract_hit_rate')}",
        f"snapshots={summary.get('snapshots_inserted')} new={summary.get('new_listings')} "
        f"price_changes={summary.get('price_changes')} delist={summary.get('delist_events')}",
        f"collected_via={summary.get('collected_via')} risk_trigger={summary.get('risk_trigger')}",
    ]
    for alert in alerts:
        lines.append(f"[{alert.get('kind')}] {alert.get('message')}")
    return "\n".join(str(x) for x in lines)


def notify_health(summary: Mapping[str, Any], *, settings: Settings,
                  alerts: Sequence[Mapping[str, Any]] = (),
                  only_when_problem: bool = True,
                  opener: Callable[..., Any] | None = None) -> NotifyResult:
    """A4 采集健康推送：仅在异常（风控触发/解析率低/被拒页>0）时发，避免噪声。"""
    problem = bool(summary.get("risk_trigger")) or bool(alerts) \
        or float(summary.get("parse_success_rate") or 0) < settings.risk_control.parse_success_ratio \
        or int(summary.get("status") != "completed")
    if only_when_problem and not problem:
        return NotifyResult(False, skipped_reason="本轮无异常，按去噪策略不推送", channel=CHANNEL_OPS)
    return send_text(format_health_alert(summary, alerts), settings=settings,
                     channel=CHANNEL_OPS, opener=opener)


def notify_alerts(alerts: Iterable[Mapping[str, Any]], *, settings: Settings,
                  opener: Callable[..., Any] | None = None) -> list[NotifyResult]:
    """逐条推送风控/健康告警（A4 渠道）。"""
    results: list[NotifyResult] = []
    for alert in alerts:
        results.append(send_text(str(alert.get("message") or alert.get("kind")),
                                 settings=settings, channel=CHANNEL_OPS, opener=opener))
    return results
