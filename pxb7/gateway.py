"""本地采集网关 —— docs/00 §2 前端 B / docs/01 §8 W4 的采集落地通道。

背景（2026-10-03 试运行结论，docs/05「试运行记录」）：pxb7 的 WAF 按「自动化环境特征」
拦截 Playwright（无头/有头、完整人工登录 Cookie 均被滑块挑战），而用户自己的真实浏览器
畅通。因此采集端改为 **Tampermonkey 用户脚本**：在用户自己浏览的页面里就地收集已渲染的
DOM，POST 到本网关（仅 127.0.0.1），网关复用既有离线管线入库：

    用户脚本（读用户正在看的页面 DOM）→ POST /ingest/cards|detail →
    本网关（parser v0.2.0 → 词表抽取 → DuckDB 幂等入库 + raw 落盘）

合规与安全要点（与 docs/01 §8 W4 验收口径一致）：
- **插件/网关都不主动请求 pxb7 地址**：数据来自用户正在浏览的页面；「每次采集张数」调大时，
  内容脚本只在本页内点站点自己的「加载更多」或滚动触发站点自身的懒加载（等同用户手动滚动，
  不猜 URL、不请求站外接口），轮次上限 12、每 2 秒一轮；
- 网关只绑定 127.0.0.1 回环，不对外网开放；请求体上限 8MB；非 JSON/超限即拒绝；
- 拦截页（WAF/验证码）绝不入库（与 parse-raw 同一道防线，识别不信任元数据）；
- 下架推断一律关闭（插件只见到用户浏览过的页面，切片覆盖不完整）；
- 幂等：(轮次桶, listing_id) 同轮先删后插，用户刷新页面重复发送不产生重复行；
- 与 crawl 共用串行锁（run_lock）：DuckDB 单写者，采集网关与管线互斥。

collected_via 口径：插件数据来自用户**登录态**浏览的页面，如实记 ``login``，
raw 元数据附 ``channel: plugin`` 标注通道来源。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from . import config as cfgmod
from . import db
from . import extract as X
from . import parser as P
from . import pipeline as PL
from .collector import (
    _check_component,
    _collect_page_signals,
    _first_number,
    _write_raw,
    new_run_id,
    raw_dir_for,
    DETAIL_FAVORITE_PATTERNS,
    DETAIL_FAVORITE_SELECTORS,
    DETAIL_VIEWER_PATTERNS,
    DETAIL_VIEWER_SELECTORS,
    MASK_MARKERS,
    ListPageResult,
    ListRunResult,
)
from .config import PROJECT_ROOT, Settings, Task
from .risk import SIGNAL_CAPTCHA, SIGNAL_IP_BLOCKED, RiskStateStore, TaskRisk

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 8 * 1024 * 1024

# 静态资产（看板 / 用户脚本）：固定路径伺服，不接受任何用户输入拼接
_DASHBOARD_FILE = PROJECT_ROOT / "extension" / "dashboard.html"
_USERSCRIPT_FILE = PROJECT_ROOT / "extension" / "pxb7-collector.user.js"
_SCRIPT_VERSION_RE = re.compile(r"@version\s+([0-9.]+)")

# 插件脚本远端配置（用户脚本启动/定时拉取 /config 应用；看板设置界面写入）
PLUGIN_CONFIG_FILE = cfgmod.PLUGIN_CONFIG_FILENAME   # 存于 state_dir
DEFAULT_PLUGIN_CONFIG: dict[str, Any] = {
    "auto_ingest": True,           # 浏览到列表/详情页时自动采集
    "reingest_interval_min": 10,   # 同一 URL 的去重间隔（分钟）
    "spa_settle_ms": 2500,         # SPA 路由切换后的渲染等待
    "debug": False,                # 控制台调试日志
    "title_interval_ms": 3000,     # 完整标题请求完成后的间隔；0–10 秒（0.1 秒步进），与同页去重独立
    "cards_target": 16,            # 每次采集目标张数（站点一页渲染 16 张；调大 = 页内加载更多）
    "collection_mode": "list",     # 扩展：列表全文 / 逐个打开详情
    "targets": [],                 # 采集目标：dim_task.task_id 列表；空=只用网关启动任务
    "paths": {},                   # 数据路径自定义：{db|raw_root|runs|log_dir: 绝对路径}
}
_PLUGIN_CONFIG_SPEC: dict[str, tuple[type, tuple[float, float] | None]] = {
    "auto_ingest": (bool, None),
    "reingest_interval_min": (int, (1, 1440)),
    "spa_settle_ms": (int, (500, 30000)),
    "debug": (bool, None),
    "title_interval_ms": (float, (0, 10000)),
    "cards_target": (int, (1, 200)),
    "collection_mode": (str, None),
}
_TASK_ID_RE = re.compile(r"[0-9A-Za-z_\-]{1,64}")

# 详情页所属游戏的识别锚点（本地 raw 实测 2026-10-03：
# 详情页面包屑有唯一 href="/buy/{game_id}/{biz_prod}" 链接；推荐位卡片带 gameid= 属性作兜底）
_DETAIL_GAME_LINK_RE = re.compile(r'href="[^"]*?/buy/(\d{1,18})/(\d{1,3})"')
_DETAIL_GAMEID_ATTR_RE = re.compile(r'gameid="(\d{1,18})"')

# 允许来源：pxb7 页面（用户脚本/内容脚本回退通道）+ 本机浏览器扩展
# （chrome-extension://… —— 独立扩展的 SW/弹窗直连本机网关；网关仅绑回环，语境安全）
_ALLOWED_ORIGINS = ("https://www.pxb7.com", "https://pxb7.com")
_EXTENSION_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://")
_LOCAL_PAGE_ORIGIN_RE = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d{1,5})?$")
_LIST_URL_RE = re.compile(r"/buy/(\d+)/(\d+)")
_DETAIL_URL_RE = re.compile(r"/product/(\d+)")


def origin_allowed(origin: str | None) -> bool:
    """来源判定：无 Origin（本机 SW/客户端）、pxb7 页面、浏览器扩展、本机看板页均放行。"""
    if not origin:
        return True
    if origin in _ALLOWED_ORIGINS:
        return True
    if origin.startswith(_EXTENSION_ORIGIN_PREFIXES):
        return True
    return bool(_LOCAL_PAGE_ORIGIN_RE.match(origin))   # 看板页（http://127.0.0.1:8765）


def origin_allowed_local(origin: str | None) -> bool:
    """本机动作端点（打开目录等）的更严来源：仅扩展与本机回环页面，pxb7 网页不放行。"""
    if not origin:
        return True
    if origin.startswith(_EXTENSION_ORIGIN_PREFIXES):
        return True
    return bool(_LOCAL_PAGE_ORIGIN_RE.match(origin))


class GatewayError(ValueError):
    """请求体/参数不合法。"""


class GatewayState:
    """网关运行状态：任务集/采集目标、配置、待回填详情、最近批次、累计统计（线程安全）。

    多任务模型（「目标采集」）：网关持有 tasks.yaml 的全部任务；用户在扩展/看板里
    勾选 targets（task_id 列表）决定采集哪些游戏；未勾选任何目标时退回网关启动任务
    （serve --task / 第一个 enabled 任务），保持旧行为。入库按 URL 的 game_id 路由到
    对应任务（每个游戏一个任务，互不混算）。
    """

    def __init__(self, settings: Settings, tasks: Task | Sequence[Task], *,
                 default_task_id: str | None = None):
        task_list = (tasks,) if isinstance(tasks, Task) else tuple(tasks)
        if not task_list:
            raise ValueError("网关至少需要 1 个任务")
        self.settings = settings
        self.tasks: tuple[Task, ...] = task_list
        by_id = {t.task_id: t for t in task_list}
        default = by_id.get(default_task_id) if default_task_id else None
        self.default_task: Task = default or next(
            (t for t in task_list if t.enabled), task_list[0])
        self.pending_details: dict[str, dict[str, Any]] = {}
        self.recent_batches: deque = deque(maxlen=50)   # 最近批次（看板实时区）
        self.config = load_plugin_config(settings)      # 插件脚本远端配置
        self.script_version: str | None = None          # 用户脚本最近一次上报的版本
        self.script_seen_at: str | None = None
        self.extension_version: str | None = None       # 扩展最近一次上报的版本
        self.extension_seen_at: str | None = None
        # RLock：handle_ingest 外层持锁期间，_ingest_* 里的 note() 会再次进入
        self.lock = threading.RLock()
        self.stats: dict[str, Any] = {
            "batches": 0, "cards_seen": 0, "cards_parsed": 0, "snapshots_inserted": 0,
            "new_listings": 0, "details_stored": 0, "risk_pages_rejected": 0,
            "target_skipped": 0, "last_batch_at": None, "last_run_id": None,
        }

    # ----- 多任务 / 采集目标 -----
    @property
    def task(self) -> Task:
        """默认任务（兼容旧调用：/status 的 task 字段、单任务网关）。"""
        return self.default_task

    def task_by_id(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)

    def effective_task_ids(self) -> list[str]:
        """实际生效的采集目标：勾选值∩已启用任务；空 → 退回默认任务。"""
        with self.lock:
            selected = [tid for tid in (self.config.get("targets") or [])
                        if (t := self.task_by_id(tid)) is not None and t.enabled]
        return selected or [self.default_task.task_id]

    def effective_tasks(self) -> tuple[Task, ...]:
        ids = set(self.effective_task_ids())
        return tuple(t for t in self.tasks if t.task_id in ids)

    def targets_mode(self) -> str:
        with self.lock:
            selected = list(self.config.get("targets") or [])
        return "selected" if selected else "default"

    def match_task(self, game_id: int, biz_prod: int | None = None) -> Task | None:
        """按 URL 的 game_id（可带 biz_prod）路由到采集目标里的任务。"""
        for task in self.effective_tasks():
            if task.game_id == int(game_id) and (biz_prod is None
                                                 or task.biz_prod == int(biz_prod)):
                return task
        return None

    def note(self, **kv: Any) -> None:
        with self.lock:
            self.stats.update(kv)
            self.stats["batches"] = self.stats.get("batches", 0) + 1
            self.stats["last_batch_at"] = _dt.datetime.now().isoformat(timespec="seconds")

    def record_batch(self, *, kind: str, run_id: str, **fields: Any) -> None:
        with self.lock:
            self.recent_batches.append({
                "at": _dt.datetime.now().isoformat(timespec="seconds"),
                "kind": kind, "run_id": run_id, **fields,
            })


# --------------------------------------------------------------------------- #
# 插件脚本远端配置（看板设置界面写入，用户脚本拉取应用）
# --------------------------------------------------------------------------- #
def _plugin_config_path(settings: Settings) -> Path:
    return Path(settings.paths.state_dir) / PLUGIN_CONFIG_FILE


def _validate_targets(value: Any, *, known_ids: Sequence[str] | None = None) -> list[str]:
    """采集目标列表：任务 ID 数组（去重保序）；已知任务集非空时校验成员存在。"""
    if not isinstance(value, (list, tuple)):
        raise GatewayError("targets 必须是任务 ID 数组（如 [\"genshin_official\"]）")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _TASK_ID_RE.fullmatch(item.strip()):
            raise GatewayError(f"targets 含非法任务 ID：{item!r}")
        tid = item.strip()
        if tid not in out:
            out.append(tid)
    if len(out) > 50:
        raise GatewayError("targets 数量超限（≤50）")
    if known_ids:
        unknown = [tid for tid in out if tid not in set(known_ids)]
        if unknown:
            raise GatewayError(f"未知任务 ID：{unknown}（可用：{list(known_ids)}）")
    return out


def validate_plugin_config(raw: Any, *, known_targets: Sequence[str] | None = None,
                           base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """合并默认值（或 base 当前配置）并校验类型/范围；非法项直接拒绝（界面表单返回 400）。

    base 语义：POST /config 传当前生效配置 → 支持"只改一个键"的部分更新；
    默认 base=None 时未提供的键回落到缺省值（与旧行为一致）。"""
    merged = dict(DEFAULT_PLUGIN_CONFIG) if base is None else dict(base)
    for key, value in DEFAULT_PLUGIN_CONFIG.items():
        merged.setdefault(key, value)
    if raw is None:
        return merged
    if not isinstance(raw, dict):
        raise GatewayError("配置必须是对象")
    for key, value in raw.items():
        if key not in _PLUGIN_CONFIG_SPEC and key not in ("targets", "paths"):
            raise GatewayError(f"未知配置项：{key}")
        if key == "targets":
            merged[key] = _validate_targets(value, known_ids=known_targets)
            continue
        if key == "paths":
            if not isinstance(value, Mapping):
                raise GatewayError("paths 必须是 {路径键: 绝对路径} 对象")
            try:
                resolved = cfgmod.validate_path_overrides(value)
            except cfgmod.PathOverrideError as exc:
                raise GatewayError(str(exc)) from exc
            merged[key] = {k: str(v) for k, v in sorted(resolved.items())}
            continue
        expect_type, bounds = _PLUGIN_CONFIG_SPEC[key]
        if expect_type is int:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or int(value) != value:
                raise GatewayError(f"配置 {key} 必须是整数")
            value = int(value)
        elif expect_type is float:
            # title_interval_ms：0–10 秒（界面 0.1 秒步进）。先按原始值做范围判定，再取整到
            # 100ms 粒度——吸收 JS「秒*1000」的浮点误差（0.1*1000=100.000…1），否则回填
            # 输入框后 step 校验会失败；先判界保证 10001 不会被取整成合法的 10000。
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise GatewayError(f"配置 {key} 必须是数字")
            value = float(value)
            if value != value:                            # NaN
                raise GatewayError(f"配置 {key} 必须是数字")
            if bounds is not None and not (bounds[0] <= value <= bounds[1]):
                raise GatewayError(f"配置 {key}={value} 超出范围 {bounds[0]}–{bounds[1]}")
            value = round(value / 100) * 100
        elif expect_type is str:
            if value not in ("list", "detail"):
                raise GatewayError("collection_mode 必须是 list 或 detail")
        else:
            if not isinstance(value, bool):
                raise GatewayError(f"配置 {key} 必须是布尔值")
        if bounds is not None and not (bounds[0] <= value <= bounds[1]):
            raise GatewayError(f"配置 {key}={value} 超出范围 {bounds[0]}–{bounds[1]}")
        merged[key] = value
    return merged


def load_plugin_config(settings: Settings) -> dict[str, Any]:
    path = _plugin_config_path(settings)
    if not path.is_file():
        return dict(DEFAULT_PLUGIN_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_PLUGIN_CONFIG)    # 损坏按默认处理（下次保存覆盖）
    try:
        return validate_plugin_config(raw)
    except GatewayError:
        return dict(DEFAULT_PLUGIN_CONFIG)


def save_plugin_config(settings: Settings, raw: Any, *,
                       base: Mapping[str, Any] | None = None,
                       known_targets: Sequence[str] | None = None) -> dict[str, Any]:
    config = validate_plugin_config(raw, known_targets=known_targets, base=base)
    path = _plugin_config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


# --------------------------------------------------------------------------- #
# 用户脚本版本与一键更新（网关持最新版 .user.js，脚本上报自身版本）
# --------------------------------------------------------------------------- #
def _latest_script_version() -> str | None:
    try:
        text = _USERSCRIPT_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _SCRIPT_VERSION_RE.search(text)
    return m.group(1) if m else None


def _ver_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version).split("."):
        m = re.match(r"(\d+)", piece)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts)


def _latest_extension_version() -> str | None:
    """仓库内未打包扩展的 manifest 版本（解包安装的「最新版」基线）。"""
    manifest = PROJECT_ROOT / "extension" / "pxb7-extension" / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = str(data.get("version") or "").strip()
    return version or None


def record_script_version(state: "GatewayState", version: str | None,
                          channel: str = "userscript") -> None:
    """记录插件上报版本（/config?version=x.y.z&channel=userscript|extension）。"""
    if not version:
        return
    clean = str(version).strip()[:32]
    if not re.fullmatch(r"[0-9A-Za-z._\-]{1,32}", clean):
        return
    with state.lock:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        if channel == "extension":
            state.extension_version = clean
            state.extension_seen_at = now
        elif channel == "userscript":
            state.script_version = clean
            state.script_seen_at = now


def script_info(state: "GatewayState") -> dict[str, Any]:
    """脚本/扩展版本与更新检测（看板共用）。

    channel 分离：用户脚本走 @updateURL 一键更新；解包扩展需在 edge://extensions
    重新加载（或用 更新浏览器扩展.bat），因此两者各自报告版本与更新状态。"""
    latest = _latest_script_version()
    ext_latest = _latest_extension_version()
    with state.lock:
        installed = state.script_version
        seen_at = state.script_seen_at
        ext_installed = state.extension_version
        ext_seen_at = state.extension_seen_at
    update_available = bool(
        latest and installed and _ver_tuple(latest) > _ver_tuple(installed))
    ext_update_available = bool(
        ext_latest and ext_installed and _ver_tuple(ext_latest) > _ver_tuple(ext_installed))
    return {
        "latest_version": latest,
        "installed_version": installed,
        "seen_at": seen_at,
        "update_available": update_available,
        "update_url": "/userscript.js",
        "note": "Tampermonkey 会按 @updateURL 自动检查；也可点「一键更新」立即安装最新版",
        "extension": {
            "latest_version": ext_latest,
            "installed_version": ext_installed,
            "seen_at": ext_seen_at,
            "update_available": ext_update_available,
            "note": "解包扩展自 v0.3.0 起支持自更新（按磁盘代码自动重载，浏览器无关）；"
                    "首次安装/升级到 v0.3.0 需在浏览器扩展页手动「加载/重新加载」一次",
        },
    }


# --------------------------------------------------------------------------- #
# 入库处理（纯逻辑，HTTP 层只做薄封装，便于离线测试）
# --------------------------------------------------------------------------- #
def _reject_risk_page(html: str) -> list[str] | None:
    """拦截页防线：WAF/验证码页面绝不入库（识别不信任任何元数据）。"""
    signals = _collect_page_signals(html)
    hits = sorted({s for s in signals if s in (SIGNAL_CAPTCHA, SIGNAL_IP_BLOCKED)})
    return hits or None


def _parse_detail_fields(html: str) -> dict[str, Any]:
    """详情页 M5 字段（正在浏览/收藏）+ 打码判定（口径与 collector.collect_detail 一致）。"""
    text = P.visible_text(html, limit=50000)
    viewers, via_viewer = _first_number(html, DETAIL_VIEWER_SELECTORS, DETAIL_VIEWER_PATTERNS)
    favorites, via_fav = _first_number(html, DETAIL_FAVORITE_SELECTORS, DETAIL_FAVORITE_PATTERNS)
    mask_detected = any(marker in text for marker in MASK_MARKERS) and viewers is None
    return {"viewers_masked": viewers, "viewers_visible": viewers is not None,
            "viewers_strategy": via_viewer, "favorites_cnt": favorites,
            "favorites_visible": favorites is not None, "favorites_strategy": via_fav,
            "viewers_mask_detected": mask_detected}


def _extract_listing_id(payload: dict[str, Any]) -> str | None:
    """listing_id：优先取 payload 显式字段，其次从 /product/{id} URL 提取。"""
    raw = str(payload.get("listing_id") or "")
    if not raw:
        m = _DETAIL_URL_RE.search(str(payload.get("url") or ""))
        raw = m.group(1) if m else ""
    try:
        _check_component(raw, "listing_id")
    except Exception:
        return None
    m = re.search(r"(\d{3,})", raw)
    return m.group(1) if m else (raw or None)


def _lookup_listing_game(state: GatewayState, listing_id: str) -> int | None:
    """本地库已有该 listing 时取所属游戏（详情 URL 不含 game_id，这是最可靠的来源）。"""
    try:
        with PL.run_lock(state.settings):
            conn = db.connect(state.settings.paths.db, read_only=True)
            try:
                row = conn.execute("SELECT game_id FROM dim_listing WHERE listing_id = ?",
                                   [listing_id]).fetchone()
            finally:
                conn.close()
    except Exception:                      # 库不可用/无表 → 交给 HTML 锚点
        return None
    return int(row[0]) if row and row[0] is not None else None


def _identify_detail_game(state: GatewayState, listing_id: str,
                          html: str) -> tuple[int | None, int | None]:
    """详情页游戏识别：本地库 → 面包屑 /buy/{game}/{biz} → 推荐位 gameid 属性。"""
    game = _lookup_listing_game(state, listing_id)
    if game is not None:
        return game, None
    m = _DETAIL_GAME_LINK_RE.search(html)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _DETAIL_GAMEID_ATTR_RE.search(html)
    if m:
        return int(m.group(1)), None
    return None, None


def _target_not_selected(state: GatewayState, game_id: int) -> tuple[int, dict[str, Any]]:
    state.note(target_skipped=state.stats["target_skipped"] + 1)
    return 422, {"ok": False, "error": "target-not-selected", "game_id": game_id,
                 "selected_tasks": state.effective_task_ids(),
                 "message": f"游戏 {game_id} 不在采集目标中；请在扩展弹窗/看板「采集目标」"
                            f"勾选对应游戏后再浏览该页面"}


def _ingest_detail(state: GatewayState, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """详情页采集（M5）：落 raw + 回填最近一轮快照；快照未到则挂起待列表批次合并。"""
    html = str(payload.get("html") or "")
    if not html.strip():
        return 400, {"ok": False, "error": "empty-html"}
    risk = _reject_risk_page(html)
    if risk:
        state.note(risk_pages_rejected=state.stats["risk_pages_rejected"] + 1)
        return 422, {"ok": False, "error": "risk-page", "signals": risk}
    listing_id = _extract_listing_id(payload)
    if not listing_id:
        return 400, {"ok": False, "error": "missing-listing-id"}
    snapshot_at = None
    if "snapshot_round" in payload:
        try:
            snapshot_at = _dt.datetime.fromisoformat(str(payload["snapshot_round"]))
            if snapshot_at.tzinfo is not None:
                raise ValueError("only local naive rounds")
        except (ValueError, TypeError):
            return 400, {"ok": False, "error": "invalid-snapshot-round"}

    game_id, biz_prod = _identify_detail_game(state, listing_id, html)
    if game_id is None:
        return 422, {"ok": False, "error": "game-unresolved",
                     "message": "无法识别该详情页所属游戏（已尝试本地库与页面面包屑）；"
                                "请先浏览目标游戏的列表页，或该游戏尚无采集任务"}
    task = state.match_task(game_id, biz_prod)
    if task is None:
        return _target_not_selected(state, game_id)

    fields = _parse_detail_fields(html)
    title, attributes = P.parse_detail_attributes(html)
    extraction = X.extract_listing(X.seed_keywords(game_id=game_id),
                                   listing_id=listing_id, title=title, card_fields=attributes)
    extraction.features["_detail_fields"] = list(attributes)
    extraction.features["_detail_rosters"] = [key for key in
        ("five_star_character_chains", "five_star_weapon_refinements") if key in extraction.features]
    now = _dt.datetime.now()
    run_id = new_run_id(f"{task.task_id}-plugin", now=now)
    raw_dir = raw_dir_for(state.settings, task.task_id, run_id, now=now)
    meta = {"url": str(payload.get("url") or ""), "page_type": "detail",
            "listing_id": listing_id, "task_id": task.task_id, "run_id": run_id,
            "collected_at": now.isoformat(timespec="seconds"), "collected_via": "login",
            "channel": "plugin", "parser_version": P.PARSER_VERSION,
            "snapshot_round": payload.get("snapshot_round"),
            "note": "前端 B：读取已加载详情 DOM，回填对应列表轮次", **fields}
    _write_raw(raw_dir, f"detail_{listing_id}", html=html, meta=meta)

    entry = {"listing_id": listing_id, "viewers_masked": fields["viewers_masked"],
             "favorites_cnt": fields["favorites_cnt"], "attributes": attributes,
             "features": extraction.features, "snapshot_at": snapshot_at}
    updated = _apply_detail(state, entry)
    if snapshot_at is not None and not updated:
        return 409, {"ok": False, "error": "snapshot-round-not-found"}
    state.note(details_stored=state.stats["details_stored"] + 1)
    state.record_batch(kind="detail", run_id=run_id, listing_id=listing_id,
                       task_id=task.task_id, game_id=game_id,
                       viewers_masked=fields["viewers_masked"],
                       favorites_cnt=fields["favorites_cnt"], rows_updated=updated)
    ack = {"ok": True, "listing_id": listing_id, "task_id": task.task_id,
           "game_id": game_id,
           "viewers_masked": fields["viewers_masked"],
           "viewers_visible": fields["viewers_visible"],
           "viewers_mask_detected": fields["viewers_mask_detected"],
           "favorites_cnt": fields["favorites_cnt"],
           "attributes": attributes,
           "weapon_details_complete": (bool(attributes.get("five_star_weapons"))
                                       and len(extraction.features.get("five_star_weapon_refinements") or [])
                                       == attributes.get("five_star_weapons")),
           "snapshot_rows_updated": updated}
    if not updated:
        ack["pending"] = ("尚无该 listing 的快照行，详情字段已暂存，"
                          "将随下一批 /ingest/cards 一并入库")
    return 200, ack


def _apply_detail(state: GatewayState, entry: dict[str, Any]) -> int:
    """把详情字段回填该 listing 最近一轮快照；无快照则挂起 pending（等列表批次合并）。"""
    with PL.run_lock(state.settings):
        conn = db.connect(state.settings.paths.db)
        try:
            updated = db.update_detail_fields(
                conn, entry["listing_id"],
                viewers_masked=entry.get("viewers_masked"),
                favorites_cnt=entry.get("favorites_cnt"),
                attributes=entry.get("attributes"), features=entry.get("features"),
                snapshot_at=entry.get("snapshot_at"))
        finally:
            conn.close()
    if not updated and entry.get("snapshot_at") is None:
        state.pending_details[entry["listing_id"]] = entry
    return updated


def _safe_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if lo <= n <= hi else default


def _safe_sweep(raw: Any) -> dict[str, Any] | None:
    """页内「加载更多」统计（目标/实际/轮次/停止原因）：透明记录，非法输入静默丢弃。"""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in ("target", "initial", "final", "rounds", "stalled"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        out[key] = int(value)
    stop = raw.get("stop")
    if isinstance(stop, str) and re.fullmatch(r"[a-z\-]{1,32}", stop):
        out["stop"] = stop
    return out


def _ingest_cards(state: GatewayState, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """列表页采集：目标路由 → 落 raw → parser v0.2.0 → 词表抽取 → 幂等入库。

    页内「加载更多」由内容脚本完成（只触发站点自身的懒加载）；本端点只接收最终 DOM，
    page_no / sweep 统计如实落进 raw 元数据与批次记录，便于复核。"""
    html = str(payload.get("html") or "")
    url = str(payload.get("url") or "")
    if not html.strip():
        return 400, {"ok": False, "error": "empty-html"}
    risk = _reject_risk_page(html)
    if risk:
        state.note(risk_pages_rejected=state.stats["risk_pages_rejected"] + 1)
        return 422, {"ok": False, "error": "risk-page", "signals": risk}

    # 目标采集：按 URL 的 game_id 路由到勾选的目标任务；未勾选的游戏直接拒收
    task: Task | None = None
    m = _LIST_URL_RE.search(url)
    if m:
        task = state.match_task(int(m.group(1)), int(m.group(2)))
        if task is None:
            return _target_not_selected(state, int(m.group(1)))
    if task is None:                        # URL 不含 /buy/ 模式：按默认任务兜底
        task = state.default_task

    now = _dt.datetime.now()
    page_no = _safe_int(payload.get("page_no"), default=1, lo=1, hi=50)
    sweep = _safe_sweep(payload.get("sweep"))
    run_id = new_run_id(f"{task.task_id}-plugin", now=now)
    raw_dir = raw_dir_for(state.settings, task.task_id, run_id, now=now)
    meta = {"url": url, "page_type": "list", "page_no": page_no, "task_id": task.task_id,
            "run_id": run_id, "collected_at": now.isoformat(timespec="seconds"),
            "http_status": None, "ok": True, "error": None,
            "collected_via": "login", "session_slot": "primary", "channel": "plugin",
            "parser_version": P.PARSER_VERSION,
            "note": "前端 B 采集插件：用户浏览页面就地收集 DOM；页内滚动只触发站点自身的"
                    "加载更多（等效用户手动滚动），不请求站外接口"}
    if sweep:
        meta["sweep"] = sweep
    expansion = payload.get("title_expansion")
    if isinstance(expansion, dict):
        safe_expansion = {key: _safe_int(expansion.get(key), default=0, lo=0, hi=200)
                          for key in ("cards", "captured", "hovered", "failed")}
        meta["title_expansion"] = safe_expansion
        meta["note"] = "前端 B：列表页悬浮展开公开完整标题；站点可触发自身标题请求，不导航详情"
    collection = payload.get("title_collection")
    if isinstance(collection, dict):
        safe_collection = {key: _safe_int(collection.get(key), default=0, lo=0,
                                        hi=800 if key == "attempts" else 400 if key == "requested" else 200)
                           for key in ("cards", "captured", "requested", "failed", "attempts", "retried")}
        stop = collection.get("stop")
        if isinstance(stop, str) and re.fullmatch(r"[a-z\-]{1,32}", stop):
            safe_collection["stop"] = stop
        safe_errors = []
        for entry in (collection.get("errors") or [])[:200] if isinstance(collection.get("errors"), list) else []:
            if not isinstance(entry, dict):
                continue
            listing_id, error = entry.get("id"), entry.get("error")
            if not isinstance(listing_id, str) or not re.fullmatch(r"\d{6,24}", listing_id):
                continue
            if not isinstance(error, str) or not re.fullmatch(r"[a-z\-]{1,32}", error):
                continue
            item = {"id": listing_id, "error": error,
                    "attempts": _safe_int(entry.get("attempts"), default=0, lo=0, hi=10)}
            if entry.get("status"):
                item["status"] = _safe_int(entry["status"], default=0, lo=100, hi=599)
            safe_errors.append(item)
        safe_collection["errors"] = safe_errors
        if collection.get("interval_ms") is not None:
            safe_collection["interval_ms"] = _safe_int(collection["interval_ms"], default=3000, lo=2000, hi=15000)
        meta["title_collection"] = safe_collection
        meta["note"] = "前端 B：复用列表响应，按商品编号串行获取公开完整标题；不触发悬浮、不导航详情"
    dump = _write_raw(raw_dir, f"list_p{page_no:02d}", html=html, meta=meta)

    parsed = P.parse_list_page(html, url=url, parser_version=state.settings.parser_version)
    page_res = ListPageResult(page_no=1, url=url, status=None, ok=True,
                              cards_seen=parsed.cards_seen, cards_parsed=parsed.cards_parsed,
                              parse_success_rate=parsed.parse_success_rate,
                              empty=parsed.cards_seen == 0, error=None, signals=(),
                              raw=dump, parse=parsed)
    round_ts = db.truncate_to_round(now, state.settings.snapshot_round_minutes)
    list_result = ListRunResult(task_id=task.task_id, run_id=run_id, collected_via="login",
                                session_slot="primary", frequency_factor=1.0,
                                raw_dir=str(raw_dir), pages=[page_res],
                                started_at=now, finished_at=now)
    # 词表按游戏画像取（未建画像的游戏返回空表：不用别的游戏的词表顶替，docs/02 §G）
    listings = PL.build_listings(list_result, X.seed_keywords(game_id=task.game_id))
    merged_details = 0
    for row in listings:                     # 详情字段先到的情况：挂载后随批次一起入库
        detail = state.pending_details.pop(row.listing_id, None)
        if detail:
            row.viewers_masked = detail.get("viewers_masked")
            if detail.get("favorites_cnt") is not None:
                row.favorites_cnt = detail["favorites_cnt"]
            row.card.fields.update(detail.get("attributes") or {})
            row.extraction.features.update(detail.get("features") or {})
            merged_details += 1

    try:
        with PL.run_lock(state.settings):    # 与 crawl 互斥（DuckDB 单写者）
            conn = db.connect(state.settings.paths.db)
            try:
                load_stats = PL.load_round(conn, state.settings, task, round_ts=round_ts,
                                           listings=listings, collected_via="login",
                                           infer_delist=False,
                                           delist_skip_reason="plugin-channel-coverage-unknown")
            finally:
                conn.close()
    except PL.PipelineError as exc:
        return 409, {"ok": False, "error": f"busy: {exc}"}

    summary = PL.build_summary(run_id=run_id, task_id=task.task_id,
                               status=PL.STATUS_COMPLETED, collected_via="login",
                               list_result=list_result, load_stats=load_stats,
                               detail_fetched=merged_details, risk_trigger=None,
                               raw_dir=str(raw_dir), duration_s=0.0, listings=listings)
    try:
        PL.write_summary(Path(raw_dir) / "summary.json", summary)
    except OSError:
        pass
    state.note(cards_seen=state.stats["cards_seen"] + parsed.cards_seen,
               cards_parsed=state.stats["cards_parsed"] + parsed.cards_parsed,
               snapshots_inserted=state.stats["snapshots_inserted"] + load_stats.snapshots_inserted,
               new_listings=state.stats["new_listings"] + load_stats.new_listings,
               last_run_id=run_id)
    state.record_batch(kind="cards", run_id=run_id, task_id=task.task_id,
                       game_id=task.game_id, page_no=page_no,
                       sweep_rounds=(sweep or {}).get("rounds", 0),
                       cards_seen=parsed.cards_seen,
                       cards_parsed=parsed.cards_parsed,
                       snapshots=load_stats.snapshots_inserted,
                       new_listings=load_stats.new_listings,
                       parse_success_rate=round(parsed.parse_success_rate, 4),
                       extract_hit_rate=summary["extract_hit_rate"])
    return 200, {"ok": True, "run_id": run_id, "task_id": task.task_id,
                 "game_id": task.game_id, "page_no": page_no,
                 "round": round_ts.isoformat(timespec="seconds"),
                 "cards_seen": parsed.cards_seen, "cards_parsed": parsed.cards_parsed,
                 "parse_success_rate": round(parsed.parse_success_rate, 4),
                 "snapshots_inserted": load_stats.snapshots_inserted,
                 "new_listings": load_stats.new_listings,
                 "extract_hit_rate": summary["extract_hit_rate"],
                 "field_diagnostics": PL.aggregate_field_diagnostics(list_result)}


def handle_ingest(state: GatewayState, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
    """HTTP 无关的入口（便于离线测试）：返回 (status, payload)。"""
    if len(body) > MAX_BODY_BYTES:
        return 413, {"ok": False, "error": "payload-too-large"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "error": "invalid-json"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "body-must-be-object"}
    with state.lock:
        if path == "/ingest/cards":
            return _ingest_cards(state, payload)
        if path == "/ingest/detail":
            return _ingest_detail(state, payload)
    return 404, {"ok": False, "error": f"unknown-path: {path}"}


# --------------------------------------------------------------------------- #
# 采集目标（targets）与数据路径（paths）
# --------------------------------------------------------------------------- #
def _game_db_counts(state: GatewayState) -> dict[int, dict[str, Any]]:
    """按游戏聚合入库量（fct_listing_snapshot ⨝ dim_listing）；库不可用返回空。"""
    try:
        conn = db.connect(state.settings.paths.db, read_only=True)
        try:
            rows = conn.execute(
                "SELECT l.game_id, count(*), count(DISTINCT l.listing_id), max(s.snapshot_at)"
                " FROM fct_listing_snapshot s JOIN dim_listing l ON l.listing_id = s.listing_id"
                " GROUP BY 1").fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for game_id, rows_n, listings_n, latest in rows:
        if game_id is None:
            continue
        out[int(game_id)] = {
            "snapshots": rows_n, "listings": listings_n,
            "latest_round": latest.isoformat(timespec="minutes") if latest else None}
    return out


def targets_payload(state: GatewayState) -> dict[str, Any]:
    """采集目标清单（看板/扩展弹窗渲染多选框）：可选任务 + 勾选/生效 + 各游戏入库量。"""
    with state.lock:
        selected = list(state.config.get("targets") or [])
        tasks = state.tasks
        default_id = state.default_task.task_id
    effective = state.effective_task_ids()
    counts = _game_db_counts(state)
    available = []
    for task in tasks:
        c = counts.get(task.game_id, {})
        available.append({
            "task_id": task.task_id, "name": task.name, "game_id": task.game_id,
            "biz_prod": task.biz_prod, "enabled": task.enabled,
            "selected": task.task_id in selected, "effective": task.task_id in effective,
            "snapshots": c.get("snapshots", 0), "listings": c.get("listings", 0),
            "latest_round": c.get("latest_round"),
        })
    return {"available": available, "selected": selected, "effective": effective,
            "mode": state.targets_mode(), "default_task": default_id,
            "games": [t.game_id for t in state.effective_tasks()],
            "note": "只采集勾选目标的游戏页面；未勾选任何目标时使用网关启动任务"}


# --------------------------------------------------------------------------- #
# 入库数据浏览（看板「数据浏览」：按游戏换列名 + 翻页）
# --------------------------------------------------------------------------- #
# 列名用**该游戏自己的词表说法**（docs/02 §4 各游戏词表 + config/keywords_seed.yaml 画像）：
#   取值方式 "col:xxx" = 直接读 fct_listing_snapshot 列；"feat:xxx" = 读 extracted_features JSON。
# 规则：哪种说法在该游戏不成立就不列该列（宁可少列，不拿别家术语硬套）。
_GAME_COLUMNS: dict[int, tuple[tuple[str, str, str], ...]] = {
    10026: (   # 原神（docs/02 §4.A1：黄数/五星角色/专武精炼/原石/纠缠之源…）
        ("level", "等级", "col:level"),
        ("yellow_cnt", "黄数", "col:yellow_cnt"),
        ("five_star_chars", "五星角色", "col:five_star_chars"),
        ("five_star_weapons", "五星武器", "col:five_star_weapons"),
        ("constellation_cnt", "角色命座", "feat:constellation_cnt"),
        ("five_star_weapon_refined", "武器精炼（精N）", "feat:five_star_weapon_refined"),
        ("primogems", "原石", "col:primogems"),
        ("intertwined_fate", "纠缠之源", "col:intertwined_fate"),
        ("artifacts", "圣遗物", "col:artifacts"),
        ("skins", "时装", "col:skins"),
        ("server", "区服", "col:server"),
        ("mail_status", "邮箱", "col:mail_status"),
    ),
    10302: (   # 鸣潮（docs/02 §4.A4 + 站内实样 MVNGK0804：N命/精N 记法）
        ("level", "等级", "col:level"),
        ("yellow_cnt", "黄数", "col:yellow_cnt"),
        ("five_star_chars", "五星角色", "col:five_star_chars"),
        ("five_star_weapons", "五星武器", "col:five_star_weapons"),
        ("constellation_cnt", "共鸣链（N命）", "feat:constellation_cnt"),
        ("five_star_weapon_refined", "武器精炼（精N）", "feat:five_star_weapon_refined"),
        ("resources", "资源", "feat:wuwa_resources"),
        ("paid_items", "额外付费商品", "feat:wuwa_paid_items"),
        ("server", "区服", "col:server"),
        ("mail_status", "邮箱", "col:mail_status"),
    ),
    10032: (   # 火影忍者（docs/02 §4.F：绝版点券 S 忍等词条待建画像，先列已抽到的通用列）
        ("level", "等级", "col:level"),
        ("skins", "时装", "col:skins"),
        ("server", "区服", "col:server"),
        ("mail_status", "邮箱", "col:mail_status"),
    ),
    10371: (   # 三角洲行动（docs/02 §4.B：红皮/武器皮肤类目/烽火段位/货币；资产类只记命中不入列）
        ("level", "等级", "col:level"),
        ("delta_fenghuo_level_cnt", "烽火等级", "feat:delta_fenghuo_level_cnt"),
        ("delta_battlefield_level_cnt", "战场等级", "feat:delta_battlefield_level_cnt"),
        ("delta_battlepass_level_cnt", "通行证等级", "feat:delta_battlepass_level_cnt"),
        ("delta_red_skin_cnt", "红皮/大红", "feat:delta_red_skin_cnt"),
        ("delta_legendary_weapon_cnt", "传说武器", "feat:delta_legendary_weapon_cnt"),
        ("delta_epic_weapon_cnt", "史诗武器", "feat:delta_epic_weapon_cnt"),
        ("delta_weapon_skin_cnt", "武器皮肤", "feat:delta_weapon_skin_cnt"),
        ("delta_operator_skin_cnt", "干员皮肤", "feat:delta_operator_skin_cnt"),
        ("delta_melee_skin_cnt", "近战皮肤", "feat:delta_melee_skin_cnt"),
        ("delta_knife_skin_cnt", "刀皮", "feat:delta_knife_skin_cnt"),
        ("delta_charm_cnt", "挂饰", "feat:delta_charm_cnt"),
        ("delta_vehicle_cnt", "载具", "feat:delta_vehicle_cnt"),
        ("delta_bundle_cnt", "捆绑包", "feat:delta_bundle_cnt"),
        ("delta_triangle_coin_cnt", "三角币", "feat:delta_triangle_coin_cnt"),
        ("delta_mandela_coin_cnt", "曼德尔币", "feat:delta_mandela_coin_cnt"),
        ("delta_triangle_coupon_cnt", "三角券", "feat:delta_triangle_coupon_cnt"),
        ("delta_service_recall_flag", "找回包赔", "feat:delta_service_recall_flag"),
        ("delta_second_realname_flag", "可二次实名", "feat:delta_second_realname_flag"),
        ("delta_no_second_realname_flag", "实名受限（不可二次）", "feat:delta_no_second_realname_flag"),
        ("server", "区服", "col:server"),
    ),
}
_GENERIC_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("level", "等级", "col:level"),
    ("yellow_cnt", "黄数", "col:yellow_cnt"),
    ("server", "区服", "col:server"),
    ("mail_status", "邮箱", "col:mail_status"),
)

LISTINGS_PAGE_DEFAULT = 15
LISTINGS_PAGE_MAX = 200
# 鸣潮资源口径单一定义在 extract（与付费商品段常量同处）；此处别名引用保持看板渲染不动。
WUWA_RESOURCES = X.WUWA_RESOURCES


def columns_for_game(game_id: int | None) -> tuple[tuple[str, str, str], ...]:
    """该游戏的数据列定义；未注册的游戏用通用列（不硬套别家术语）。"""
    if game_id is None:
        return _GENERIC_COLUMNS
    return _GAME_COLUMNS.get(int(game_id), _GENERIC_COLUMNS)


def _columns_payload(columns: Sequence[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [{"key": key, "label": label, "source": source.split(":", 1)[0]}
            for key, label, source in columns]


def _parse_features(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def handle_listings(state: GatewayState, query: Mapping[str, Any]) -> dict[str, Any]:
    """入库数据分页查询（看板「数据浏览」）：`offset` / `limit` / `game_id`。

    只读、离线；参数非法即回落（limit 10–200、offset ≥0、game_id 未注册按全部）。
    列名按所选游戏取该游戏自己的说法，取值来源在 columns[].source 里如实标注
    （col=快照列 / feat=extracted_features 特征）。
    """
    def single(key: str, default: str = "") -> str:
        value = query.get(key)
        if isinstance(value, (list, tuple)):
            return str(value[0]) if value else default
        return str(value if value is not None else default)

    offset = _safe_int(single("offset", "0"), default=0, lo=0, hi=10_000_000)
    limit = _safe_int(single("limit", str(LISTINGS_PAGE_DEFAULT)),
                      default=LISTINGS_PAGE_DEFAULT, lo=10, hi=LISTINGS_PAGE_MAX)
    raw_game = _safe_int(single("game_id", "0"), default=0, lo=0, hi=2_000_000_000_000)
    game_id = raw_game or None
    columns = (*columns_for_game(game_id), ("published_at", "商品发布时间", "col:publish_time_text"))

    col_names = sorted({source.split(":", 1)[1] for _, _, source in columns
                        if source.startswith("col:")})
    select_cols = "".join(f" s.{name}," for name in col_names)
    where, params = "", []
    if game_id is not None:
        where, params = " WHERE l.game_id = ?", [game_id]

    base = (" FROM fct_listing_snapshot s"
            " JOIN dim_listing l ON l.listing_id = s.listing_id"
            " LEFT JOIN dim_game g ON g.game_id = l.game_id")

    payload: dict[str, Any] = {"ok": True, "offset": offset, "limit": limit,
                               "game_id": game_id, "columns": _columns_payload(columns)}
    try:
        conn = db.connect(state.settings.paths.db, read_only=True)
        try:
            total = conn.execute(f"SELECT count(*){base}{where}", params).fetchone()[0]
            mail_count, viewer_count, publish_count = conn.execute(
                "SELECT count(NULLIF(trim(s.mail_status), '')), count(s.viewers_masked),"
                f" count(NULLIF(trim(s.publish_time_text), '')){base}{where}", params).fetchone()
            columns = tuple(column for column in columns
                            if not (column[0] == "mail_status" and not mail_count)
                            and not (column[0] == "published_at" and not publish_count))
            payload["columns"] = _columns_payload(columns)
            payload["show_viewers"] = bool(viewer_count)
            games = [{"game_id": int(r[0]), "game_name": r[1] or str(r[0]), "rows": r[2]}
                     for r in conn.execute(
                         "SELECT l.game_id, max(g.game_name), count(*)"
                         f"{base} GROUP BY l.game_id ORDER BY count(*) DESC").fetchall()]
            rows = conn.execute(
                "SELECT s.listing_id, s.snapshot_at, s.price_yuan, s.viewers_masked,"
                " s.favorites_cnt, s.extracted_features, l.game_id, g.game_name,"
                f"{select_cols.rstrip(',')}{base}{where}"
                " ORDER BY s.snapshot_at DESC, s.listing_id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        finally:
            conn.close()
    except Exception as exc:                  # 库不可用不拖垮看板
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["rows"], payload["total"], payload["games"] = [], 0, []
        return payload

    out_rows: list[dict[str, Any]] = []
    for row in rows:
        listing_id, snapshot_at, price, viewers, favorites, features_raw, gid, gname = row[:8]
        col_values = dict(zip(col_names, row[8:]))
        features = _parse_features(features_raw)
        cells: dict[str, Any] = {}
        for key, _label, source in columns:
            kind, name = source.split(":", 1)
            value = col_values.get(name) if kind == "col" else features.get(name)
            if name == "wuwa_resources":
                resources = [{"name": label, "value": features.get(feature)}
                             for feature, label in WUWA_RESOURCES]
                value = resources if any(item["value"] is not None for item in resources) else None
            if name == "wuwa_paid_items":
                paid_items = [{"name": label, "value": "、".join(features[feature]) if features.get(feature) else None}
                              for feature, label in X.WUWA_PAID_ITEMS]
                value = paid_items if any(item["value"] is not None for item in paid_items) else None
            roster_key = {"constellation_cnt": "five_star_character_chains",
                          "five_star_weapon_refined": "five_star_weapon_refinements"}.get(name)
            if roster_key and features.get(roster_key):
                value = features[roster_key]
            if value is None and kind == "col":
                fallback = {"level": "account_level_cnt", "five_star_chars": "five_star_chars_cnt",
                            "five_star_weapons": "five_star_weapons_cnt", "yellow_cnt": "yellow_cnt"}.get(name)
                value = features.get(fallback) if fallback else None
            if hasattr(value, "isoformat"):
                value = value.isoformat(timespec="seconds")
            cells[key] = value
        out_rows.append({
            "listing_id": listing_id,
            "round": snapshot_at.isoformat(timespec="minutes") if snapshot_at else None,
            "price": float(price) if price is not None else None,
            "viewers": viewers, "favorites": favorites,
            "game_id": int(gid) if gid is not None else None,
            "game_name": gname or (str(gid) if gid is not None else "—"),
            "cells": cells,
        })
    payload.update({"total": total, "games": games, "rows": out_rows})
    return payload


def paths_payload(state: GatewayState) -> dict[str, Any]:
    """数据存放位置清单（展示 + 自定义入口）：绝对路径、是否被自定义、可否自定义。"""
    return {"items": cfgmod.paths_summary(state.settings),
            "override_file": str(cfgmod.PATHS_OVERRIDE_FILE),
            "overridable": list(cfgmod.OVERRIDABLE_PATH_KEYS),
            "note": "登录态/风控状态目录不随数据路径迁移（安全相关）；"
                    "改动即时对网关生效，CLI 下次运行生效"}


def _apply_config_paths(state: GatewayState, config: Mapping[str, Any]) -> None:
    """应用数据路径自定义：建目录 → 写覆写文件 → 重载设置（含"恢复默认"）→ 新库初始化。

    恢复默认（paths={}）时不能只清空覆写文件——必须从 settings.yaml 重新加载，
    否则运行中的网关会停留在旧的覆写路径上。任一步失败即回滚覆写文件，状态不变。"""
    overrides = {key: Path(value) for key, value in (config.get("paths") or {}).items()}
    previous = cfgmod.load_path_overrides()
    try:
        prospective = cfgmod.apply_path_overrides(state.settings, overrides)
        cfgmod.prepare_data_dirs(prospective)
    except cfgmod.PathOverrideError as exc:
        raise GatewayError(str(exc)) from exc

    def _restore_override_file() -> None:
        try:
            cfgmod.save_path_overrides({k: str(v) for k, v in previous.items()})
        except OSError:
            pass

    cfgmod.save_path_overrides({key: str(value) for key, value in overrides.items()})
    try:
        new_settings = cfgmod.load_settings(state.settings.source_path)
    except Exception as exc:                    # settings.yaml 不可读等 → 回滚
        _restore_override_file()
        raise GatewayError(f"路径设置加载失败：{type(exc).__name__}: {exc}") from exc
    new_db = Path(new_settings.paths.db)
    try:
        if not new_db.exists():                 # 新位置首次使用：建库 + 词表种子
            db.init_db(new_db, tasks=[t.as_row() for t in state.tasks if t.enabled])
            with db.connect(new_db) as conn:
                db.upsert_dim_keywords(conn, X.seed_db_rows())
    except Exception as exc:
        _restore_override_file()
        raise GatewayError(f"新数据库初始化失败：{type(exc).__name__}: {exc}") from exc
    state.settings = new_settings


def handle_config_post(state: GatewayState, raw: Any) -> tuple[int, dict[str, Any]]:
    """POST /config（HTTP 无关入口）：部分键更新（与当前配置合并）；
    paths 变化时即时应用（建目录 + 新库初始化 + 覆写文件落盘）。"""
    known = [t.task_id for t in state.tasks if t.enabled]
    try:
        with state.lock:
            old_paths = dict(state.config.get("paths") or {})
            merged = validate_plugin_config(raw, known_targets=known, base=state.config)
            if merged.get("paths", {}) != old_paths:
                _apply_config_paths(state, merged)
            state.config = save_plugin_config(state.settings, merged)
            config = dict(state.config)
            targets = targets_payload(state)
    except GatewayError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except OSError as exc:
        return 500, {"ok": False, "error": f"配置写入失败：{type(exc).__name__}: {exc}"}
    return 200, {"ok": True, "config": config, "targets": targets,
                 "note": "脚本会在下次拉取配置（≤5 分钟）或页面刷新时应用"}


# 可打开的目录键（本机动作端点白名单：路径一律来自当前设置，不接受请求传入路径）
_OPENABLE_KEYS: dict[str, str] = {"db": "file", "raw_root": "dir", "runs": "dir",
                                  "log_dir": "dir", "state_dir": "dir"}


def handle_open_folder(state: GatewayState, raw: Any) -> tuple[int, dict[str, Any]]:
    """在资源管理器中打开数据目录（仅本机回环/扩展来源；键白名单，杜绝任意路径）。"""
    if not isinstance(raw, dict):
        return 400, {"ok": False, "error": "body-must-be-object"}
    key = str(raw.get("key") or "")
    if key not in _OPENABLE_KEYS:
        return 400, {"ok": False, "error": f"key 必须是 {sorted(_OPENABLE_KEYS)} 之一"}
    path = Path(getattr(state.settings.paths, key))
    target = path.parent if _OPENABLE_KEYS[key] == "file" else path
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return 500, {"ok": False, "error": f"目录不可用：{target}（{exc}）"}
    if not hasattr(os, "startfile"):
        return 501, {"ok": False, "error": "当前平台不支持打开目录", "path": str(target)}
    try:
        os.startfile(str(target))          # 本机资源管理器；路径来自白名单键
    except OSError as exc:
        return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return 200, {"ok": True, "opened": str(target)}


def handle_status(state: GatewayState) -> dict[str, Any]:
    try:
        conn = db.connect(state.settings.paths.db, read_only=True)
        try:
            counts = db.table_counts(conn)
            latest = db.latest_round(conn)
        finally:
            conn.close()
        db_payload = {"latest_round": latest.isoformat(timespec="seconds") if latest else None,
                      "snapshot_rows": counts.get("fct_listing_snapshot", 0),
                      "listings": counts.get("dim_listing", 0)}
    except Exception as exc:                  # 库不可用不拖垮 /status
        db_payload = {"error": f"{type(exc).__name__}: {exc}"}
    with state.lock:
        stats = dict(state.stats)
    return {"ok": True, "task": state.default_task.task_id,
            "tasks": [t.task_id for t in state.tasks],
            "targets": state.effective_task_ids(),
            "gateway": "pxb7-frontend-b/0.1",
            "stats": stats, "pending_details": len(state.pending_details), "db": db_payload}


def _risk_summary(settings: Settings) -> dict[str, Any]:
    """管线通道（Playwright）的风控状态，看板只读展示（插件通道零请求、不受其约束）。"""
    rs = RiskStateStore(settings.paths.risk_state).load()
    now = _dt.datetime.now()
    return {
        "global_stop": rs.global_stop, "level": rs.level, "reason": rs.reason,
        "tasks": {tid: {
            "level": entry.level, "reason": entry.reason,
            "backoff_until": entry.backoff_until.isoformat(timespec="seconds")
            if entry.backoff_until else None,
            "backoff_remaining_s": round(entry.backoff_remaining(now), 1),
        } for tid, raw in rs.tasks.items() for entry in [TaskRisk.from_json(raw)]},
    }


def _group_keywords_by_game(rows: Sequence[Any], *, per_game_limit: int = 10
                            ) -> dict[str, Any]:
    """把「game_id, 游戏名, 关键词, 命中数」明细按游戏分组（每组取前 N 条）。"""
    grouped: dict[str, Any] = {}
    for game_id, game_name, keyword, hits in rows:
        key = str(game_id)
        entry = grouped.setdefault(key, {"game_id": game_id,
                                         "game_name": game_name or str(game_id),
                                         "top": []})
        if len(entry["top"]) < per_game_limit:
            entry["top"].append({"keyword": keyword, "hits": hits})
    return grouped


def build_stats_payload(state: GatewayState) -> dict[str, Any]:
    """看板 /stats 聚合：网关实时状态 + 插件配置 + DB 聚合 + 风控状态（只读，全离线）。"""
    with state.lock:
        stats = dict(state.stats)
        recent = list(state.recent_batches)
        config = dict(state.config)
        pending = len(state.pending_details)

    db_payload: dict[str, Any] = {}
    try:
        conn = db.connect(state.settings.paths.db, read_only=True)
        try:
            counts = db.table_counts(conn)
            latest = db.latest_round(conn)
            rounds = conn.execute(
                "SELECT snapshot_at, count(*) AS rows, count(DISTINCT listing_id) AS listings"
                " FROM fct_listing_snapshot GROUP BY 1 ORDER BY 1 DESC LIMIT 14"
            ).fetchall()
            hist = conn.execute(
                "SELECT CAST(floor(price_yuan / 200) AS INTEGER) AS b, count(*)"
                " FROM fct_listing_snapshot WHERE price_yuan IS NOT NULL AND price_yuan < 2000"
                " GROUP BY b ORDER BY b").fetchall()
            top_keywords = conn.execute(
                "SELECT k.keyword, count(*) AS hits FROM fct_listing_keyword f"
                " JOIN dim_keyword k ON k.keyword_id = f.keyword_id"
                " GROUP BY 1 ORDER BY hits DESC LIMIT 10").fetchall()
            # 词表命中按游戏分组（看板按所选游戏展示该游戏自己的词条，不混算别家）
            keywords_by_game = conn.execute(
                "SELECT l.game_id, g.game_name, k.keyword, count(*) AS hits"
                " FROM fct_listing_keyword f"
                " JOIN dim_listing l ON l.listing_id = f.listing_id"
                " JOIN dim_keyword k ON k.keyword_id = f.keyword_id"
                " LEFT JOIN dim_game g ON g.game_id = l.game_id"
                " GROUP BY 1, 2, 3 ORDER BY hits DESC").fetchall()
            latest_listings = conn.execute(
                "SELECT s.listing_id, s.snapshot_at, s.price_yuan, s.level, s.yellow_cnt,"
                " s.server, s.mail_status, s.viewers_masked, s.favorites_cnt,"
                " l.game_id, g.game_name"
                " FROM fct_listing_snapshot s"
                " LEFT JOIN dim_listing l ON l.listing_id = s.listing_id"
                " LEFT JOIN dim_game g ON g.game_id = l.game_id"
                " ORDER BY s.snapshot_at DESC, s.listing_id DESC LIMIT 15"
            ).fetchall()
            via = conn.execute(
                "SELECT collected_via, count(*) FROM fct_listing_snapshot GROUP BY 1"
            ).fetchall()
        finally:
            conn.close()
        db_payload = {
            "snapshot_rows": counts.get("fct_listing_snapshot", 0),
            "listings": counts.get("dim_listing", 0),
            "keyword_hits": counts.get("fct_listing_keyword", 0),
            "price_changes": counts.get("fct_price_change", 0),
            "delist_events": counts.get("fct_delist_event", 0),
            "latest_round": latest.isoformat(timespec="seconds") if latest else None,
            "rounds": [{"round": r[0].isoformat(timespec="minutes"), "rows": r[1],
                        "listings": r[2]} for r in rounds],
            "price_histogram": [{"bucket": (r[0] or 0) * 200, "count": r[1]} for r in hist],
            "top_keywords": [{"keyword": r[0], "hits": r[1]} for r in top_keywords],
            "top_keywords_by_game": _group_keywords_by_game(keywords_by_game),
            "latest_listings": [{
                "listing_id": r[0], "round": r[1].isoformat(timespec="minutes"),
                "price": float(r[2]) if r[2] is not None else None,
                "level": r[3], "yellow_cnt": r[4], "server": r[5], "mail_status": r[6],
                "viewers": r[7], "favorites": r[8],
                "game_id": r[9], "game_name": r[10],
            } for r in latest_listings],
            "collected_via": {str(r[0]): r[1] for r in via},
        }
    except Exception as exc:                  # 库不可用不拖垮看板
        db_payload = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "gateway": "pxb7-frontend-b/0.1",
        "task": {"task_id": state.default_task.task_id, "game_id": state.default_task.game_id,
                 "biz_prod": state.default_task.biz_prod, "name": state.default_task.name},
        "db_path": str(state.settings.paths.db),
        "stats": stats,
        "recent_batches": recent,
        "config": config,
        "targets": targets_payload(state),
        "paths": paths_payload(state),
        "script": script_info(state),
        "pending_details": pending,
        "risk": _risk_summary(state.settings),
        "db": db_payload,
    }


# --------------------------------------------------------------------------- #
# HTTP 层（薄封装；仅绑定 127.0.0.1）
# --------------------------------------------------------------------------- #
def _cors(headers: list[tuple[str, str]], origin: str | None) -> None:
    """仅对 pxb7 页面与浏览器扩展来源发 CORS 头；其余来源不发。"""
    if not origin or not origin_allowed(origin):
        return
    headers.append(("Access-Control-Allow-Origin", origin))
    headers.append(("Access-Control-Allow-Methods", "POST, GET, OPTIONS"))
    headers.append(("Access-Control-Allow-Headers", "Content-Type"))
    headers.append(("Vary", "Origin"))


class GatewayHandler(BaseHTTPRequestHandler):
    """薄 HTTP 封装；业务在 handle_ingest / handle_status（可离线测试）。"""
    state: GatewayState
    server_version = "pxb7-gateway/0.1"

    def _origin(self) -> str | None:
        return self.headers.get("Origin")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers: list[tuple[str, str]] = [("Content-Type", "application/json; charset=utf-8")]
        _cors(headers, self._origin())
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        headers: list[tuple[str, str]] = []
        _cors(headers, self._origin())
        self.send_response(204)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send(404, {"ok": False, "error": f"asset-missing: {path.name}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in ("/", "/dashboard"):
            self._serve_file(_DASHBOARD_FILE, "text/html; charset=utf-8")
        elif path == "/userscript.js":
            self._serve_file(_USERSCRIPT_FILE, "text/javascript; charset=utf-8")
        elif path == "/config":
            query = parse_qs(urlsplit(self.path).query)
            version = (query.get("version") or [""])[0]
            channel = (query.get("channel") or ["userscript"])[0]
            record_script_version(self.state, version, channel=channel)  # 插件自报版本
            with self.state.lock:
                self._send(200, {"ok": True, "config": dict(self.state.config),
                                 "targets": targets_payload(self.state),
                                 "script": script_info(self.state)})
        elif path == "/stats":
            self._send(200, build_stats_payload(self.state))
        elif path == "/listings":
            query = parse_qs(urlsplit(self.path).query)
            self._send(200, handle_listings(self.state, query))
        elif path == "/status":
            self._send(200, handle_status(self.state))
        else:
            self._send(404, {"ok": False, "error": "unknown-path"})

    def _read_json_body(self, limit: int) -> tuple[dict[str, Any] | None, int | None]:
        """读 JSON 请求体；返回 (payload, None) 或 (None, http_status)。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            self._send(413, {"ok": False, "error": "payload-too-large-or-empty"})
            return None, 413
        try:
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "invalid-json"})
            return None, 400
        return raw, None

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/shutdown":
            # 优雅停止（仅回环可达；由 run.py stop-gateway / 看板按钮触发）
            self._send(200, {"ok": True, "note": "网关正在停止"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path == "/open-folder":
            origin = self._origin()
            if not origin_allowed_local(origin):     # 本机动作端点：pxb7 网页来源不放行
                self._send(403, {"ok": False, "error": f"origin-not-allowed: {origin}"})
                return
            raw, err = self._read_json_body(4 * 1024)
            if err is not None:
                return
            self._send(*handle_open_folder(self.state, raw))
            return
        if path == "/config":
            origin = self._origin()
            if not origin_allowed(origin):
                self._send(403, {"ok": False, "error": f"origin-not-allowed: {origin}"})
                return
            raw, err = self._read_json_body(64 * 1024)
            if err is not None:
                return
            self._send(*handle_config_post(self.state, raw))
            return
        if path not in ("/ingest/cards", "/ingest/detail"):
            self._send(404, {"ok": False, "error": "unknown-path"})
            return
        origin = self._origin()
        if not origin_allowed(origin):
            self._send(403, {"ok": False, "error": f"origin-not-allowed: {origin}"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": "payload-too-large-or-empty"})
            return
        body = self.rfile.read(length)
        try:
            status, payload = handle_ingest(self.state, path, body)
        except Exception as exc:              # 网关不因单批失败崩溃
            status, payload = 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        self._send(status, payload)

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静：不刷屏
        pass


def serve(settings: Settings, *, task: Task | None = None,
          tasks: Sequence[Task] | None = None, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT, log_file: str | None = None) -> None:
    """启动网关（阻塞）；仅绑定回环地址。log_file 非空时把输出重定向到文件
    （分离进程/无控制台场景——pythonw 下 sys.stdout 为 None，必须落盘）。

    task=T 作为默认采集目标（未勾选 targets 时生效）；tasks 传入全部任务供「目标采集」选择。"""
    log_handle = None
    if log_file or sys.stdout is None or sys.stderr is None:
        target = Path(log_file) if log_file else Path(settings.paths.log_dir) / "gateway.log"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(target, "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = log_handle
            if sys.stderr is None:
                sys.stderr = log_handle
        except OSError:
            log_handle = None
    task_list = list(tasks or ([task] if task else []))
    if not task_list:
        raise ValueError("serve 需要 task 或 tasks 之一")
    state = GatewayState(settings, task_list,
                         default_task_id=(task.task_id if task else None))
    handler = type("BoundHandler", (GatewayHandler,), {"state": state})
    server = ThreadingHTTPServer((host, port), handler)
    version = ""
    if _USERSCRIPT_FILE.is_file():
        m = _SCRIPT_VERSION_RE.search(_USERSCRIPT_FILE.read_text(encoding="utf-8", errors="replace"))
        version = f"（用户脚本 v{m.group(1)}）" if m else ""
    print(f"[gateway] 前端 B 采集网关已启动：http://{host}:{port}（仅本机回环）")
    print(f"[gateway] 采集看板：http://{host}:{port}/   用户脚本{version}：http://{host}:{port}/userscript.js")
    print(f"[gateway] 默认任务：{state.default_task.task_id}"
          f"（game={state.default_task.game_id}/{state.default_task.biz_prod}）；"
          f"可选目标 {len(task_list)} 个：{', '.join(t.task_id for t in task_list)}")
    print(f"[gateway] 采集目标（勾选生效，看板「采集目标」多选）："
          f"{', '.join(state.effective_task_ids())}；DB：{settings.paths.db}")
    print("[gateway] 等待插件推送 /ingest/cards 与 /ingest/detail … Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[gateway] 已退出")
    finally:
        server.server_close()
        if log_handle is not None:
            log_handle.close()
