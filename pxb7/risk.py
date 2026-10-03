"""风控退避状态机与限速器 —— docs/01 §3.3 逐条落地（契约 riskRules 6 条 + rateLimits 10 条）。

状态机（信号 → 动作，与 §3.3 表格一一对应）：
| 信号 | 动作 |
|---|---|
| captcha 验证码/滑块 | 立即停采该任务，24h 后以 **1/4 频率**试探恢复；连续 2 次 → 停并告警 |
| empty_response_rate 请求被拒/空响应率 >30% | 当轮终止，退避 **6h** |
| ip_blocked 整站不可达 | 停止采集，**人工介入**（不自动恢复） |
| login_kicked 主登录态被风控/踢出 | 切备用登录态，频率降至 **1/2** |
| login_both_invalid 备用亦失效 | 降级**游客态** + **72h** 观察后人工换号（不连续重试登录） |
| parse_rate_low 解析成功率 <80% | **继续**采集但只入 raw 层 + 解析告警（不改变限速档） |
| ok | 试探成功：解除试探模式与连续计数，回到常规档 |

限速器（rateLimits，硬数值照抄 docs/01 §3.1-4）：单页间隔 3–6s 随机、任务间 ≥60s、
每任务每轮 ≤5 页；登录态不放松限速（只解锁可见性，游客态与登录态同档）；
备用 1/2 档（主登录态被踢）；验证码后试探恢复 1/4 档。

持久化：data/state/risk_state.json（至少含 level、reason、backoff_until、last_trigger、consecutive）。
每轮 crawl 开始前调用 `RiskMachine.check_before_run()`；未到期直接终止并在 summary 里写明原因。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import Settings

STATE_SCHEMA_VERSION = "v2"   # v2：任务级档位（tasks 映射）；v1 旧文件读取时自动迁移

# ---- 信号 ---------------------------------------------------------------- #
SIGNAL_OK = "ok"
SIGNAL_CAPTCHA = "captcha"
SIGNAL_EMPTY_RATE = "empty_response_rate"
SIGNAL_IP_BLOCKED = "ip_blocked"
SIGNAL_LOGIN_KICKED = "login_kicked"
SIGNAL_LOGIN_BOTH_INVALID = "login_both_invalid"
SIGNAL_PARSE_RATE_LOW = "parse_rate_low"

# ---- 级别 ---------------------------------------------------------------- #
LEVEL_NORMAL = "normal"            # 常规
LEVEL_WARN = "warn"                # 降级/观察中（游客态 72h 观察、只入 raw）
LEVEL_BACKOFF = "backoff"          # 退避中（6h）
LEVEL_TASK_PAUSED = "task_paused"  # 验证码停采 24h，到期以 1/4 频率试探
LEVEL_STOPPED = "stopped"          # 停采 + 告警，等待人工

# ---- 会话槽位 ------------------------------------------------------------ #
SLOT_PRIMARY = "primary"
SLOT_BACKUP = "backup"
SLOT_GUEST = "guest"

# 验证码/滑块页面的文本标记（**可见文本**匹配；由 collector 剔除 script/style 后扫描）
# 2026-10-02 冒烟实测：站点在游客态返回阿里云 WAF 滑块页，可见文本含
#   「访问验证 为保证您的正常访问,请进行如下验证 TraceID: …」+「请按住滑块，拖动到最右边」
# 注意：裸词「滑块」不作为标记（价格区间滑块等正常 UI 会误报）——用「请按住滑块」「拖动到最右边」等强特征。
CAPTCHA_MARKERS: tuple[str, ...] = (
    "验证码", "安全验证", "人机验证", "行为验证", "请完成验证",
    "拖动滑块", "请按住滑块", "拖动到最右边",
    "访问验证", "为保证您的正常访问",
    "captcha", "verify you are human", "are you a robot",
)
# 整站不可达/被拒的页面文本标记
BLOCK_MARKERS: tuple[str, ...] = (
    "访问被拒绝", "您的访问过于频繁", "请求过于频繁", "IP 已被封禁", "已被限制访问",
    "403 forbidden", "access denied", "too many requests",
)
# 登录墙标记：**登录态**下出现这些字样 ⇒ 登录态已失效/被踢（docs/01 §3.3 主登录态被风控）
LOGIN_WALL_MARKERS: tuple[str, ...] = (
    "请先登录", "请登录后再", "登录已过期", "重新登录", "登录状态已失效",
    "登录后可见", "登录查看", "立即登录",
)
# DOM 级强特征：WAF/验证组件的**激活**状态（2026-10-02 实测 waf_nc_block 为 display:block）
WAF_DOM_PATTERNS: tuple[tuple[str, str], ...] = (
    ("waf-slider-active", r"id=[\"']waf_nc_block[\"'][^>]*style=[\"'][^\"']*display:\s*block"),
    ("aliyun-captcha-shown", r"aliyunCaptcha-show"),
    ("waf-nocaptcha-shown", r"id=[\"']nocaptcha[\"'][^>]*style=[\"'][^\"']*display:\s*block"),
)

_HISTORY_LIMIT = 50


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
@dataclass
class TaskRisk:
    """单任务风控档位（docs/01 §3.3：停采**该任务**，不影响其它任务）。"""
    level: str = LEVEL_NORMAL
    reason: str | None = None
    backoff_until: _dt.datetime | None = None
    consecutive: int = 0
    probe_mode: bool = False
    frequency_factor: float = 1.0
    raw_only: bool = False
    updated_at: _dt.datetime | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("backoff_until", "updated_at"):
            value = data.get(key)
            data[key] = value.isoformat(timespec="seconds") if isinstance(value, _dt.datetime) else None
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "TaskRisk":
        data = data or {}
        kwargs: dict[str, Any] = {}
        for key in ("level", "reason", "consecutive", "probe_mode", "frequency_factor", "raw_only"):
            if data.get(key) is not None:
                kwargs[key] = data[key]
        for key in ("backoff_until", "updated_at"):
            raw = data.get(key)
            if raw:
                try:
                    kwargs[key] = _dt.datetime.fromisoformat(str(raw))
                except ValueError:
                    pass
        return cls(**kwargs)

    def backoff_remaining(self, now: _dt.datetime) -> float:
        if not self.backoff_until:
            return 0.0
        return max(0.0, (self.backoff_until - now).total_seconds())


@dataclass
class RiskState:
    """风控状态（持久化）。

    - 全局字段：IP 封禁（整站不可达）、会话槽位、观察期、任务间隔等——对所有任务生效；
    - `tasks`：**按任务**的档位（验证码停采 24h、空响应退避 6h、解析率低只入 raw 等），
      一个任务命中验证码不会让别的任务跟着停采（docs/01 §3.3「立即停采该任务」）。
    """
    level: str = LEVEL_NORMAL            # 全局级别（stopped 表示整站不可达，需人工介入）
    reason: str | None = None
    backoff_until: _dt.datetime | None = None   # 全局退避（当前仅 IP 封禁以外的场景留空）
    last_trigger: _dt.datetime | None = None
    consecutive: int = 0                 # 兼容字段：最近触发任务的连续计数
    updated_at: _dt.datetime | None = None
    task_id: str | None = None           # 最近一次触发的任务
    session_slot: str = SLOT_PRIMARY          # primary / backup / guest
    probe_mode: bool = False                  # 验证码后 1/4 频率试探轮（最近任务）
    frequency_factor: float = 1.0             # 乘在单页间隔上的频率系数（最近任务）
    raw_only: bool = False                    # 解析成功率 <80%：只入 raw 层（最近任务）
    observe_until: _dt.datetime | None = None # 主备登录态均失效后的 72h 观察截止
    last_task_finished_at: _dt.datetime | None = None   # 任务间隔 ≥60s 的依据
    # 全局停采标志（2026-10-03 试运行修复）：仅 IP 封禁/整站不可达/状态文件不可读为 True。
    # 顶层 level 是「最近操作任务」的镜像，任务级 captcha-consecutive 的 STOPPED 也会被
    # 镜像上来——若把镜像出的 STOPPED 当全局停采，会违反 §3.3「停采该任务」不外溢。
    global_stop: bool = False
    alerts: list[dict[str, Any]] = _dc_field(default_factory=list)   # 待推送告警
    history: list[dict[str, Any]] = _dc_field(default_factory=list)  # 最近触发记录
    tasks: dict[str, dict[str, Any]] = _dc_field(default_factory=dict)  # task_id → TaskRisk JSON
    schema_version: str = STATE_SCHEMA_VERSION

    # -- 任务档位 --------------------------------------------------------- #
    def task_state(self, task_id: str | None) -> TaskRisk:
        if not task_id:
            return TaskRisk()
        raw = self.tasks.get(task_id)
        return TaskRisk.from_json(raw) if raw else TaskRisk()

    def put_task_state(self, task_id: str, entry: TaskRisk) -> None:
        self.tasks[task_id] = entry.to_json()

    # -- 序列化 ---------------------------------------------------------- #
    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("backoff_until", "last_trigger", "updated_at", "observe_until",
                    "last_task_finished_at"):
            value = data.get(key)
            data[key] = value.isoformat(timespec="seconds") if isinstance(value, _dt.datetime) else None
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> "RiskState":
        if not isinstance(data, Mapping):
            return cls()
        kwargs: dict[str, Any] = {}
        for key in ("level", "reason", "consecutive", "task_id", "session_slot",
                    "probe_mode", "frequency_factor", "raw_only", "global_stop"):
            if key in data and data[key] is not None:
                kwargs[key] = data[key]
        kwargs["schema_version"] = STATE_SCHEMA_VERSION
        for key in ("backoff_until", "last_trigger", "updated_at", "observe_until",
                    "last_task_finished_at"):
            raw = data.get(key)
            if raw:
                try:
                    kwargs[key] = _dt.datetime.fromisoformat(str(raw))
                except ValueError:
                    pass
        for key in ("alerts", "history"):
            value = data.get(key)
            if isinstance(value, list):
                kwargs[key] = value
        tasks = data.get("tasks")
        if isinstance(tasks, Mapping):
            kwargs["tasks"] = {str(k): dict(v) for k, v in tasks.items()
                               if isinstance(v, Mapping)}
        elif data.get("task_id"):
            # v1 → v2 迁移：旧状态把任务档位平铺在顶层，这里搬进 tasks[task_id]
            legacy = TaskRisk.from_json({k: data.get(k) for k in (
                "level", "reason", "backoff_until", "consecutive", "probe_mode",
                "frequency_factor", "raw_only", "updated_at")})
            kwargs["tasks"] = {str(data["task_id"]): legacy.to_json()}
            kwargs["schema_version"] = STATE_SCHEMA_VERSION
            if legacy.level == LEVEL_STOPPED and legacy.reason == "ip-blocked":
                kwargs["level"] = LEVEL_STOPPED          # IP 封禁是全局的，保留全局级别
        return cls(**kwargs)

    def backoff_remaining(self, now: _dt.datetime | None = None) -> float:
        """全局退避剩余秒数（任务级退避见 task_state()）。"""
        now = now or _dt.datetime.now()
        if not self.backoff_until:
            return 0.0
        return max(0.0, (self.backoff_until - now).total_seconds())

    def is_blocked(self, now: _dt.datetime | None = None, task_id: str | None = None) -> bool:
        now = now or _dt.datetime.now()
        if self.level == LEVEL_STOPPED and self.global_stop:
            return True
        entry = self.task_state(task_id)
        return entry.backoff_remaining(now) > 0 or entry.level == LEVEL_STOPPED


@dataclass
class RunDecision:
    """每轮 crawl 的前置判定结果（供 summary 如实说明）。"""
    allow: bool
    reason: str | None
    level: str
    backoff_until: _dt.datetime | None
    remaining_s: float
    frequency_factor: float
    probe_mode: bool
    raw_only: bool
    session_slot: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow, "reason": self.reason, "level": self.level,
            "backoff_until": self.backoff_until.isoformat(timespec="seconds") if self.backoff_until else None,
            "remaining_s": round(self.remaining_s, 1), "frequency_factor": self.frequency_factor,
            "probe_mode": self.probe_mode, "raw_only": self.raw_only,
            "session_slot": self.session_slot,
        }


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
class RiskStateStore:
    """risk_state.json 读写（原子写：临时文件 + os.replace）。"""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> RiskState:
        if not self.path.is_file():
            return RiskState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 状态文件损坏**不得**按初始态放行（fail-open）：文件里可能记着仍在生效的
            # 验证码 24h 停采 / 6h 退避，静默清零会丢掉 docs/01 §3.3 的纪律。
            # 按全局 stopped 处理（需人工检查该文件），并生成 A4 告警。
            now_iso = _dt.datetime.now().isoformat(timespec="seconds")
            state = RiskState(level=LEVEL_STOPPED,
                              reason=f"state-file-unreadable（{type(exc).__name__}）",
                              global_stop=True)
            state.alerts.append({
                "at": now_iso, "kind": "state-unreadable", "severity": "ops",
                "message": f"风控状态文件不可读（{self.path}，{type(exc).__name__}）。"
                           "为不丢失停采/退避纪律已停止采集，请人工检查该文件后恢复。",
                "task_id": None,
            })
            return state
        return RiskState.from_json(data)

    def save(self, state: RiskState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_json(), ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
class RiskMachine:
    """docs/01 §3.3 状态机；所有时间可注入（便于测试）。"""

    def __init__(self, settings: Settings, *, state_path: str | os.PathLike[str] | None = None,
                 now: Callable[[], _dt.datetime] | None = None,
                 store: RiskStateStore | None = None):
        self.settings = settings
        self.rc = settings.risk_control
        self._now = now or _dt.datetime.now
        self.store = store or RiskStateStore(state_path or settings.paths.risk_state)
        self.state = self.store.load()

    # -- 工具 ------------------------------------------------------------ #
    def now(self) -> _dt.datetime:
        return self._now()

    def _touch(self) -> None:
        self.state.updated_at = self.now()

    def _record(self, signal: str, action: str, detail: str | None = None, *,
                task_id: str | None = None) -> None:
        entry = {
            "at": self.now().isoformat(timespec="seconds"),
            "signal": signal,
            "action": action,
            "detail": detail,
            "level": self.state.level,
            "consecutive": self.state.consecutive,
            "task_id": task_id or self.state.task_id,
        }
        self.state.history.append(entry)
        del self.state.history[:-_HISTORY_LIMIT]
        self.state.last_trigger = self.now()

    def _alert(self, kind: str, message: str, severity: str = "ops",
               task_id: str | None = None) -> None:
        self.state.alerts.append({
            "at": self.now().isoformat(timespec="seconds"),
            "kind": kind, "severity": severity, "message": message,
            "task_id": task_id or self.state.task_id,
        })
        del self.state.alerts[:-20]

    def drain_alerts(self) -> list[dict[str, Any]]:
        """取走待推送告警（A4 采集健康告警）。"""
        alerts = list(self.state.alerts)
        self.state.alerts.clear()
        return alerts

    # -- 前置检查 -------------------------------------------------------- #
    def check_before_run(self, task_id: str | None = None) -> RunDecision:
        """每轮 crawl 开始前调用：该任务（或全局）未过退避期则直接终止（allow=False）。

        docs/01 §3.3：验证码/空响应是**任务级**停采，一个任务命中不影响其它任务；
        只有 IP 整站不可达是全局 stop。
        """
        now = self.now()
        state = self.state
        if task_id:
            state.task_id = task_id
        entry = state.task_state(task_id)
        self._mirror(entry)          # 顶层字段始终反映该任务的实际档位（含拒绝时）

        if state.level == LEVEL_STOPPED and state.global_stop:
            # 全局停采：仅 IP 封禁/整站不可达/状态文件不可达走这条分支；
            # 任务级 captcha-consecutive 的 STOPPED 只停该任务（不外溢，§3.3）
            return RunDecision(False, state.reason or "stopped", state.level,
                               state.backoff_until, 0.0, 0.0, False, False,
                               state.session_slot)
        if entry.level == LEVEL_STOPPED:          # 任务级：连续验证码等，人工介入
            return RunDecision(False, entry.reason or "stopped", entry.level,
                               entry.backoff_until, 0.0, entry.frequency_factor,
                               entry.probe_mode, entry.raw_only, state.session_slot)
        remaining = entry.backoff_remaining(now)
        if remaining > 0:
            return RunDecision(False, entry.reason or "backoff", entry.level,
                               entry.backoff_until, remaining, entry.frequency_factor,
                               entry.probe_mode, entry.raw_only, state.session_slot)

        # 退避到期：验证码暂停 → 以 1/4 频率试探；其余 → 恢复档位
        if entry.level == LEVEL_TASK_PAUSED and entry.backoff_until:
            entry.probe_mode = True
            entry.level = LEVEL_WARN
            entry.reason = "captcha-probe"
            entry.frequency_factor = self.settings.rate_limit.probe_factor
            entry.backoff_until = None
            self._record("probe-start", "以 1/4 频率试探恢复", entry.reason, task_id=task_id)
        elif entry.level == LEVEL_BACKOFF:
            entry.level = LEVEL_WARN
            entry.reason = "backoff-expired"
            entry.frequency_factor = self._slot_factor()
            entry.backoff_until = None
            self._record("backoff-expired", "退避到期，恢复采集", task_id=task_id)

        if state.observe_until and now >= state.observe_until \
                and state.reason == "login-both-invalid":
            state.observe_until = None
            self._record("observe-expired", "72h 观察到期，需人工换号后恢复登录态", task_id=task_id)
            self._alert("login-replace", "主备登录态均失效的 72h 观察期已满，请人工更换小号并重新登录",
                        severity="ops")
        if task_id:
            state.put_task_state(task_id, entry)
        self._mirror(entry)

        self._touch()
        self.store.save(state)
        return RunDecision(True, entry.reason, entry.level, None, 0.0,
                           entry.frequency_factor, entry.probe_mode, entry.raw_only,
                           state.session_slot)

    # -- 槽位系数 -------------------------------------------------------- #
    def _slot_factor(self) -> float:
        return self.settings.rate_limit.factor_for(self.state.session_slot)

    def _mirror(self, entry: TaskRisk) -> None:
        """把任务档位镜像到顶层字段，保证 status/risk_detail 与旧读法一致。

        全局 STOPPED（IP 封禁/整站不可达）优先，不被任务档位覆盖。
        """
        state = self.state
        if state.level == LEVEL_STOPPED:
            return
        state.level = entry.level
        state.reason = entry.reason
        state.backoff_until = entry.backoff_until
        state.consecutive = entry.consecutive
        state.probe_mode = entry.probe_mode
        state.frequency_factor = entry.frequency_factor
        state.raw_only = entry.raw_only

    def sync_session_slot(self, slot: str) -> None:
        """把状态机里的会话槽位同步为**实际使用**的槽位（游客态就该记 guest）。

        仅同步槽位与据此计算的频率系数；不覆盖验证码试探档（1/4）与停采档。
        """
        state = self.state
        slot = str(slot or SLOT_GUEST)
        changed = state.session_slot != slot
        state.session_slot = slot
        for task_id, raw in list(state.tasks.items()):
            entry = TaskRisk.from_json(raw)
            if entry.probe_mode or entry.level in (LEVEL_STOPPED, LEVEL_TASK_PAUSED):
                continue
            entry.frequency_factor = self.settings.rate_limit.factor_for(slot)
            state.put_task_state(task_id, entry)
        if not state.task_id:
            return
        entry = state.task_state(state.task_id)
        if not (entry.probe_mode or entry.level in (LEVEL_STOPPED, LEVEL_TASK_PAUSED)):
            self._mirror(entry)
        if changed:
            self._record("session-slot", f"会话槽位同步为 {slot}")
            self._touch()
            self.store.save(state)

    # -- 信号处理 -------------------------------------------------------- #
    def on_signal(self, signal: str, *, detail: str | None = None,
                  task_id: str | None = None) -> RiskState:
        """按信号推进状态机并落盘。

        任务级信号（验证码/空响应/解析率/登录态）写入 tasks[task_id]；
        IP 整站不可达写全局级别（对所有任务生效，需人工介入）。
        """
        now = self.now()
        state = self.state
        if task_id:
            state.task_id = task_id
        entry = state.task_state(task_id)
        task_label = task_id or state.task_id

        if signal == SIGNAL_OK:
            entry.consecutive = 0
            entry.probe_mode = False
            entry.backoff_until = None
            if entry.level in (LEVEL_BACKOFF, LEVEL_TASK_PAUSED, LEVEL_WARN):
                entry.level = LEVEL_NORMAL
                entry.reason = None
            entry.frequency_factor = self._slot_factor()
            entry.raw_only = False
            self._record(signal, "本轮正常，清零连续计数", task_id=task_label)

        elif signal == SIGNAL_CAPTCHA:
            entry.consecutive += 1
            entry.level = LEVEL_TASK_PAUSED
            entry.reason = "captcha"
            hours = self.rc.captcha_probe_after_hours
            entry.backoff_until = now + _dt.timedelta(hours=hours)
            entry.probe_mode = False
            entry.frequency_factor = self.settings.rate_limit.probe_factor
            self._record(signal, f"立即停采该任务；{hours:g}h 后以 1/4 频率试探恢复",
                         detail, task_id=task_label)
            if entry.consecutive >= self.rc.captcha_escalate_after:
                entry.level = LEVEL_STOPPED
                entry.reason = "captcha-consecutive"
                entry.backoff_until = None
                self._record(signal, "连续命中验证码达到阈值 → 停采该任务并告警",
                             task_id=task_label)
                self._alert("captcha-stop",
                            f"任务 {task_label} 连续 {entry.consecutive} 次命中验证码/滑块，"
                            f"已停采该任务，需人工介入", severity="ops")

        elif signal == SIGNAL_EMPTY_RATE:
            entry.level = LEVEL_BACKOFF
            entry.reason = "empty-response-rate"
            hours = self.rc.empty_response_backoff_hours
            entry.backoff_until = now + _dt.timedelta(hours=hours)
            entry.frequency_factor = self._slot_factor()
            self._record(signal, f"当轮终止，退避 {hours:g}h", detail, task_id=task_label)
            self._alert("empty-response",
                        f"任务 {task_label} 空响应/被拒率超阈值（{detail or 'n/a'}），"
                        f"当轮终止并退避 {hours:g}h")

        elif signal == SIGNAL_IP_BLOCKED:
            state.level = LEVEL_STOPPED              # 全局：所有任务停采
            state.global_stop = True
            state.reason = "ip-blocked"
            state.backoff_until = None
            state.frequency_factor = 0.0
            self._record(signal, "整站不可达 → 停止采集，人工介入", detail, task_id=task_label)
            self._alert("ip-blocked", "疑似 IP 被限制/整站不可达，已停止采集，需人工确认",
                        severity="ops")
            self._touch()
            self.store.save(state)
            return state

        elif signal == SIGNAL_LOGIN_KICKED:
            state.session_slot = SLOT_BACKUP         # 会话是全局资源
            if entry.level in (LEVEL_NORMAL, LEVEL_WARN):
                entry.level = LEVEL_WARN
            entry.reason = "login-kicked"
            entry.frequency_factor = self._slot_factor()
            self._record(signal, "切换备用登录态，频率降至 1/2", detail, task_id=task_label)

        elif signal == SIGNAL_LOGIN_BOTH_INVALID:
            state.session_slot = SLOT_GUEST
            entry.level = LEVEL_WARN
            entry.reason = "login-both-invalid"
            hours = self.rc.observe_hours_after_both_invalid
            state.observe_until = now + _dt.timedelta(hours=hours)
            entry.frequency_factor = self._slot_factor()
            self._record(signal, f"降级游客态 + {hours:g}h 观察后人工换号（不连续重试登录）",
                         detail, task_id=task_label)
            self._alert("login-degraded",
                        f"主备登录态均失效，已降级游客态并进入 {hours:g}h 观察期"
                        f"（collected_via=guest）")

        elif signal == SIGNAL_PARSE_RATE_LOW:
            entry.raw_only = True
            if entry.level == LEVEL_NORMAL:
                entry.level = LEVEL_WARN
            entry.reason = entry.reason or "parse-rate-low"
            self._record(signal, "采集继续但只入 raw 层 + 解析告警", detail, task_id=task_label)
            self._alert("parse-rate-low",
                        f"任务 {task_label} 卡片解析成功率低于阈值（{detail or 'n/a'}），"
                        f"本轮只入 raw 层并告警")

        else:
            raise ValueError(f"未知风控信号：{signal!r}")

        if task_label:
            state.put_task_state(task_label, entry)
            self._mirror(entry)
        self._touch()
        self.store.save(state)
        return state

    # -- 运行期辅助 ------------------------------------------------------ #
    def note_task_finished(self, when: _dt.datetime | None = None) -> None:
        self.state.last_task_finished_at = when or self.now()
        self._touch()
        self.store.save(self.state)

    def seconds_since_last_task(self) -> float:
        last = self.state.last_task_finished_at
        if not last:
            return float("inf")
        return max(0.0, (self.now() - last).total_seconds())

    def should_switch_to_backup(self) -> bool:
        return self.rc.login_switch_to_backup and self.state.session_slot == SLOT_PRIMARY


def detect_signals(text: str) -> tuple[str, ...]:
    """从**可见文本**识别风控信号（纯函数，便于离线测试）。

    调用方须先剔除 script/style/noscript（pxb7.parser.visible_text），
    否则内联脚本里的字样会造成误报。
    """
    if not text:
        return ()
    lowered = text.lower()
    signals: list[str] = []
    if any(marker.lower() in lowered for marker in CAPTCHA_MARKERS):
        signals.append(SIGNAL_CAPTCHA)
    if any(marker.lower() in lowered for marker in BLOCK_MARKERS):
        signals.append(SIGNAL_IP_BLOCKED)
    return tuple(signals)


def detect_login_wall(text: str) -> bool:
    """登录态页面出现登录墙标记 ⇒ 登录态失效/被踢（游客态不适用：本来就没登录）。"""
    if not text:
        return False
    return any(marker in text for marker in LOGIN_WALL_MARKERS)


def detect_dom_signals(html: str) -> tuple[str, ...]:
    """从原始 HTML 识别**已激活**的 WAF/验证组件（DOM 级强特征，非 SDK 挂载）。

    与 detect_signals 互补：文本层看用户可见内容，DOM 层看组件激活状态。
    """
    if not html:
        return ()
    hits: list[str] = []
    for name, pattern in WAF_DOM_PATTERNS:
        if re.search(pattern, html, re.IGNORECASE):
            hits.append(f"{SIGNAL_CAPTCHA}:{name}")
    return tuple(hits)


def combine_signals(*groups: Iterable[str]) -> tuple[str, ...]:
    """合并多来源信号，去重保序（captcha/ip_blocked 等原始信号名优先保留）。"""
    seen: list[str] = []
    for group in groups:
        for item in group:
            base = item.split(":", 1)[0]
            if base not in seen:
                seen.append(base)
    return tuple(seen)


def evaluate_empty_response_rate(rejected: int, total: int, threshold: float) -> tuple[bool, float]:
    """空响应/被拒率是否越线（>阈值）。返回 (越线, 比率)。"""
    if total <= 0:
        return (False, 0.0)
    rate = rejected / total
    return (rate > threshold, rate)


def evaluate_parse_rate(cards_parsed: int, cards_seen: int, threshold: float) -> tuple[bool, float]:
    """解析成功率是否低于阈值（<阈值 → 只入 raw 层）。返回 (越线, 比率)。"""
    if cards_seen <= 0:
        return (False, 0.0)
    rate = cards_parsed / cards_seen
    return (rate < threshold, rate)


# --------------------------------------------------------------------------- #
# 限速器
# --------------------------------------------------------------------------- #
class RateLimiter:
    """docs/01 §3.1-4 限速：3–6s 随机/页、任务间 ≥60s、每任务每轮 ≤5 页。"""

    def __init__(self, settings: Settings, *, machine: RiskMachine | None = None,
                 sleeper: Callable[[float], None] | None = None,
                 rng: random.Random | None = None,
                 recorder: Callable[[float, str], None] | None = None):
        self.settings = settings
        self.rl = settings.rate_limit
        self.machine = machine
        self._sleep = sleeper or time.sleep
        # 用系统 CSPRNG 取抖动：间隔不可预测（固定节奏本身是可被识别的特征）。
        # 非加密用途，测试可注入确定性 rng。
        self._rng = rng or random.SystemRandom()
        self._recorder = recorder
        self.slept_seconds = 0.0

    # -- 计算 ------------------------------------------------------------ #
    def frequency_factor(self, override: float | None = None) -> float:
        if override is not None:
            return max(0.0, float(override))
        if self.machine is not None:
            return max(0.0, float(self.machine.state.frequency_factor))
        return self.rl.login_factor

    def page_interval(self, factor: float | None = None) -> float:
        """单页间隔：在 [3,6] 秒内随机，再乘以频率系数（登录态系数=1，不放松）。"""
        lo, hi = self.rl.page_interval_sec
        base = self._rng.uniform(lo, hi)
        return base * self.frequency_factor(factor)

    def max_pages(self, pages_requested: int | None = None) -> int:
        """每任务每轮页数上限（settings 硬上限 5 与任务配置取小）。"""
        cap = self.rl.max_pages_per_run
        if pages_requested is None:
            return cap
        return max(1, min(int(pages_requested), cap))

    def task_interval(self, elapsed_s: float | None = None) -> float:
        """距上一次任务结束还差多少秒才满 ≥60s（已满则 0）。"""
        elapsed = self.machine.seconds_since_last_task() if elapsed_s is None else elapsed_s
        if elapsed == float("inf"):
            return 0.0
        return max(0.0, self.rl.task_interval_sec - elapsed)

    # -- 等待 ------------------------------------------------------------ #
    def wait_between_pages(self, *, page_no: int = 1, factor: float | None = None) -> float:
        seconds = self.page_interval(factor)
        if self._recorder:
            self._recorder(seconds, f"page-{page_no}")
        self._sleep(seconds)
        self.slept_seconds += seconds
        return seconds

    def wait_between_tasks(self, *, factor: float | None = None) -> float:
        remaining = self.task_interval()
        if factor is not None and factor > 1:
            remaining *= factor
        if remaining <= 0:
            return 0.0
        if self._recorder:
            self._recorder(remaining, "task-gap")
        self._sleep(remaining)
        self.slept_seconds += remaining
        return remaining

    def note_task_finished(self) -> None:
        if self.machine is not None:
            self.machine.note_task_finished()


def limiter_for(settings: Settings, machine: RiskMachine | None = None, **kwargs) -> RateLimiter:
    return RateLimiter(settings, machine=machine, **kwargs)


def describe_rules(settings: Settings) -> dict[str, Any]:
    """把生效中的风控/限速参数打出来（供 status / 排障核对）。"""
    rc = settings.risk_control
    rl = settings.rate_limit
    return {
        "page_interval_sec": list(rl.page_interval_sec),
        "task_interval_sec": rl.task_interval_sec,
        "max_pages_per_run": rl.max_pages_per_run,
        "runs_per_day": list(rl.runs_per_day),
        "factors": {"login": rl.login_factor, "backup": rl.backup_state_factor,
                    "guest": rl.guest_factor, "probe": rl.probe_factor},
        "note_factors": "登录态与游客态同档（docs/01 §3.3 登录只解锁可见性）；"
                        "backup=1/2（主登录态被踢）；probe=1/4（验证码后试探恢复）",
        "captcha": {"probe_after_hours": rc.captcha_probe_after_hours,
                    "escalate_after_consecutive": rc.captcha_escalate_after},
        "empty_response": {"ratio_threshold": rc.empty_response_ratio,
                           "backoff_hours": rc.empty_response_backoff_hours},
        "ip_blocked_action": rc.ip_blocked_action,
        "login_state": {"observe_hours_after_both_invalid": rc.observe_hours_after_both_invalid,
                        "retry_login_forbidden": rc.retry_login_forbidden,
                        "allow_concurrent_tasks": rc.allow_concurrent_tasks},
        "parse_success_ratio": rc.parse_success_ratio,
    }


def signals_from_round(*, rejected: int, total: int, cards_seen: int, cards_parsed: int,
                       text: str = "", threshold_empty: float | None = None,
                       threshold_parse: float | None = None) -> list[str]:
    """一轮采集后按信号优先级给出应触发的信号列表（不含 ok）。"""
    out: list[str] = []
    page_signals = detect_signals(text)
    if SIGNAL_IP_BLOCKED in page_signals:
        out.append(SIGNAL_IP_BLOCKED)
    if SIGNAL_CAPTCHA in page_signals:
        out.append(SIGNAL_CAPTCHA)
    if threshold_empty is not None:
        hit, _ = evaluate_empty_response_rate(rejected, total, threshold_empty)
        if hit:
            out.append(SIGNAL_EMPTY_RATE)
    if threshold_parse is not None:
        hit, _ = evaluate_parse_rate(cards_parsed, cards_seen, threshold_parse)
        if hit:
            out.append(SIGNAL_PARSE_RATE_LOW)
    return out


__all__ = [
    "RiskState", "RiskStateStore", "RiskMachine", "RunDecision", "RateLimiter",
    "SIGNAL_OK", "SIGNAL_CAPTCHA", "SIGNAL_EMPTY_RATE", "SIGNAL_IP_BLOCKED",
    "SIGNAL_LOGIN_KICKED", "SIGNAL_LOGIN_BOTH_INVALID", "SIGNAL_PARSE_RATE_LOW",
    "LEVEL_NORMAL", "LEVEL_WARN", "LEVEL_BACKOFF", "LEVEL_TASK_PAUSED", "LEVEL_STOPPED",
    "SLOT_PRIMARY", "SLOT_BACKUP", "SLOT_GUEST",
    "detect_signals", "evaluate_empty_response_rate", "evaluate_parse_rate",
    "signals_from_round", "limiter_for", "describe_rules",
]
