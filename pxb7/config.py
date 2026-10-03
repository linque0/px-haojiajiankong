"""配置加载与校验。

路径语义（与 docs/01 §3/§4 的约定一致）：
- **内部路径**（settings.paths.*：DB、raw 根、状态文件、日志目录）一律以 **项目根** 为基准解析，
  不依赖调用时的 CWD —— 从任何目录运行 run.py 结果相同。
- **CLI 显式传入的路径参数**（如 `init-db --db D:/x.duckdb`）按 **调用时 CWD** 解析，
  尊重用户在命令行里的相对路径直觉（resolve_cli_path）。
- 环境变量覆写（PXB7_DB_PATH / PXB7_RAW_ROOT）视同 CLI 显式输入，同样按 CWD 解析。
- 凭据（webhook 等）只从环境变量读取，本模块不落盘、不打印任何凭据字面量。

校验失败抛 ConfigError；不阻断但需提醒的问题收进 Settings.warnings。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .urlguard import UnsafeUrlError, assert_safe_url

# 项目根：pxb7/config.py -> pxb7/ -> 项目根（与 CWD 无关）
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS_PATH: Path = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_TASKS_PATH: Path = PROJECT_ROOT / "config" / "tasks.yaml"

ENV_DB_PATH = "PXB7_DB_PATH"
ENV_RAW_ROOT = "PXB7_RAW_ROOT"
ENV_NOTIFY_WEBHOOK = "PXB7_NOTIFY_WEBHOOK"

# 数据路径自定义（看板/扩展设置界面写入；CLI: run.py paths）
#   运行时覆写文件放在 config/ 下（不随数据根迁移，否则"搬家"后会丢失覆写本身）；
#   优先级：settings.yaml < paths_override.json < 环境变量（PXB7_DB_PATH/PXB7_RAW_ROOT）。
PATHS_OVERRIDE_FILE: Path = PROJECT_ROOT / "config" / "paths_override.json"
OVERRIDABLE_PATH_KEYS: tuple[str, ...] = ("db", "raw_root", "runs", "log_dir")
PLUGIN_CONFIG_FILENAME = "plugin_config.json"

# 路径展示清单（看板/弹窗/`run.py paths` 共用）：(键, 标签, 类型)
PATH_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("db", "数据库（快照/词表/listing）", "file"),
    ("raw_root", "原始页面 DOM（bronze，可重放）", "dir"),
    ("runs", "轮次 summary 与健康告警", "dir"),
    ("log_dir", "日志", "dir"),
    ("state_dir", "登录态目录（安全相关，不可经界面迁移）", "dir"),
    ("storage_primary", "主登录态文件", "file"),
    ("storage_backup", "备登录态文件", "file"),
    ("risk_state", "风控状态机", "file"),
)

# docs/01 §2：bizProd=1 账号 / 4 道具 / 2 充值
BIZ_PROD_ACCOUNT = 1
BIZ_PROD_VALUES = (1, 2, 4)
SORT_VALUES = ("comprehensive", "newest", "price", "favorite")

# docs/01 §2 实测筛选器即属性字典（未知键只告警，不报错——站点可能新增筛选项）
FILTER_KEYS = (
    "price_min", "price_max",
    "level_min", "level_max",
    "five_star_chars_min", "five_star_chars_max",
    "yellow_min", "yellow_max",
    "primogems_min", "primogems_max",
    "intertwined_min", "intertwined_max",
)

# docs/01 §3.1-4 限速硬下限 / 上限（低于/高于即视为放松风控档，直接拒绝）
MIN_PAGE_INTERVAL_SEC = 3.0
MIN_TASK_INTERVAL_SEC = 60.0
MAX_PAGES_PER_RUN = 5
DOC_RUNS_PER_DAY = (2, 4)
# 翻页 query 参数名白名单（防止把奇怪字符拼进 URL）
_SAFE_PARAM_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")


class ConfigError(RuntimeError):
    """配置缺失、类型错误或违反文档硬约束。"""


class PathOverrideError(ConfigError):
    """数据路径自定义不合法（键、绝对路径、可写性）。"""


# --------------------------------------------------------------------------- #
# 路径工具
# --------------------------------------------------------------------------- #
def project_root() -> Path:
    """项目根（从 __file__ 推导，不读 CWD）。"""
    return PROJECT_ROOT


def resolve_internal(path_like: str | os.PathLike[str]) -> Path:
    """内部路径：相对路径按项目根解析（不依赖 CWD）。"""
    p = Path(path_like).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def resolve_cli_path(path_like: str | os.PathLike[str]) -> Path:
    """CLI 显式传入的路径：相对路径按调用时 CWD 解析。"""
    p = Path(path_like).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


# --------------------------------------------------------------------------- #
# 数据路径自定义（paths_override.json）
# --------------------------------------------------------------------------- #
def _override_file(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(path) if path else PATHS_OVERRIDE_FILE


def validate_path_overrides(raw: Any) -> dict[str, Path]:
    """校验覆写映射：键必须可自定义、值必须是有长度上限的绝对路径。

    只校验结构（不碰文件系统）；目录创建/可写性由 prepare_data_dirs 负责。"""
    if not isinstance(raw, Mapping):
        raise PathOverrideError("路径覆写必须是 {键: 绝对路径} 对象")
    out: dict[str, Path] = {}
    for key, value in raw.items():
        if key not in OVERRIDABLE_PATH_KEYS:
            raise PathOverrideError(
                f"不支持自定义的路径键：{key!r}（可自定义：{'、'.join(OVERRIDABLE_PATH_KEYS)}）")
        if not isinstance(value, str) or not value.strip():
            raise PathOverrideError(f"路径 {key} 必须是非空字符串")
        text = value.strip()
        if "\x00" in text or len(text) > 300:
            raise PathOverrideError(f"路径 {key} 非法（空字符或长度 >300）")
        p = Path(text).expanduser()
        if not p.is_absolute():
            raise PathOverrideError(f"路径 {key}={text!r} 必须是绝对路径（如 D:/pxb7-data）")
        out[key] = p.resolve()
    return out


def load_path_overrides(path: str | os.PathLike[str] | None = None) -> dict[str, Path]:
    """读覆写文件；缺失/损坏/非法条目按"无覆写"容忍（不阻断启动）。"""
    fp = _override_file(path)
    if not fp.is_file():
        return {}
    try:
        raw = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Path] = {}
    for key, value in raw.items():
        try:
            out.update(validate_path_overrides({key: value}))
        except PathOverrideError:
            continue
    return out


def save_path_overrides(raw: Any, path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """整包写入覆写文件；空对象 = 恢复默认（删除覆写文件）；返回落盘的规范化映射。"""
    overrides = validate_path_overrides(raw)
    fp = _override_file(path)
    if not overrides:
        if fp.is_file():
            fp.unlink()
        return {}
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: str(value) for key, value in sorted(overrides.items())}
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def clear_path_overrides(path: str | os.PathLike[str] | None = None) -> bool:
    """删除覆写文件（恢复 settings.yaml 默认路径）；返回是否真的删除了文件。"""
    fp = _override_file(path)
    if not fp.is_file():
        return False
    fp.unlink()
    return True


def apply_path_overrides(settings: Settings, overrides: Mapping[str, os.PathLike[str] | str]
                         ) -> Settings:
    """把覆写映射应用到 Settings（不改盘上文件；未知键由 validate 拦截）。"""
    if not overrides:
        return settings
    clean = {key: Path(value).expanduser().resolve() for key, value in overrides.items()}
    return dataclasses.replace(
        settings, paths=dataclasses.replace(settings.paths, **clean))


def prepare_data_dirs(settings: Settings) -> list[str]:
    """确保数据目录存在且可写（建目录 + 写探针）；失败抛 PathOverrideError。

    覆盖：db 的父目录、raw_root、runs、log_dir（即"采集产物"落点）。
    登录态/风控状态目录不在此列——它们不允许经界面迁移。"""
    dirs = {Path(settings.paths.db).parent, Path(settings.paths.raw_root),
            Path(settings.paths.runs), Path(settings.paths.log_dir)}
    for folder in dirs:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".pxb7-write-probe.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise PathOverrideError(f"数据目录不可写：{folder}（{type(exc).__name__}: {exc}）") from exc
    return sorted(str(folder) for folder in dirs)


def paths_summary(settings: Settings,
                  overrides: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """当前生效数据路径清单（看板/弹窗/CLI 展示；customized=被覆写文件改写）。"""
    active = load_path_overrides() if overrides is None else overrides
    items: list[dict[str, Any]] = []
    for key, label, kind in PATH_ITEMS:
        items.append({
            "key": key, "label": label, "kind": kind,
            "path": str(getattr(settings.paths, key)),
            "overridable": key in OVERRIDABLE_PATH_KEYS,
            "customized": key in active,
        })
    items.append({
        "key": "plugin_config", "label": "插件配置（采集目标/插件设置）", "kind": "file",
        "path": str(Path(settings.paths.state_dir) / PLUGIN_CONFIG_FILENAME),
        "overridable": False, "customized": False,
    })
    return items


# --------------------------------------------------------------------------- #
# YAML 读取
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败：{path}：{exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置文件顶层必须是映射：{path}")
    return data


def _require_map(data: Mapping[str, Any], key: str, ctx: str) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        raise ConfigError(f"{ctx} 缺少必填段：{key}")
    if not isinstance(value, Mapping):
        raise ConfigError(f"{ctx}.{key} 必须是映射，实际 {type(value).__name__}")
    return value


def _require_str(data: Mapping[str, Any], key: str, ctx: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{ctx}.{key} 必须是非空字符串，实际 {value!r}")
    return value.strip()


def _as_number(value: Any, ctx: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{ctx} 必须是数字，实际 {value!r}")
    return float(value)


def _as_pair(value: Any, ctx: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{ctx} 必须是 [下限, 上限] 两元素数组，实际 {value!r}")
    lo, hi = (_as_number(value[0], f"{ctx}[0]"), _as_number(value[1], f"{ctx}[1]"))
    if lo <= 0 or hi <= 0:
        raise ConfigError(f"{ctx} 必须为正数，实际 {value!r}")
    if lo > hi:
        raise ConfigError(f"{ctx} 下限不得大于上限，实际 {value!r}")
    return (lo, hi)


# --------------------------------------------------------------------------- #
# 数据类
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Paths:
    project_root: Path
    db: Path
    raw_root: Path
    state_dir: Path
    storage_primary: Path
    storage_backup: Path
    risk_state: Path
    log_dir: Path
    runs: Path = PROJECT_ROOT / "data" / "runs"   # 轮次 summary / A4 告警落盘（QC 数据源）


@dataclass(frozen=True)
class RateLimit:
    page_interval_sec: tuple[float, float]
    task_interval_sec: float
    max_pages_per_run: int
    runs_per_day: tuple[int, int]
    login_factor: float
    backup_state_factor: float
    guest_factor: float
    probe_factor: float = 4.0

    def factor_for(self, mode: str) -> float:
        """采集姿态 → 间隔系数（>1 表示更慢）。

        docs/01 §3.3：登录态与游客态**同一档**（登录只解锁可见性，不加压）；
        只有「主登录态被踢切备用」才是 1/2 档。
        """
        return {"login": self.login_factor,
                "primary": self.login_factor,
                "backup": self.backup_state_factor,
                "guest": self.guest_factor}.get(mode, self.login_factor)


@dataclass(frozen=True)
class RiskControl:
    captcha_stop_immediately: bool
    captcha_probe_after_hours: float
    captcha_escalate_after: int
    empty_response_ratio: float
    empty_response_backoff_hours: float
    ip_blocked_action: str
    login_switch_to_backup: bool
    observe_hours_after_both_invalid: float
    retry_login_forbidden: bool
    allow_concurrent_tasks: bool
    parse_success_ratio: float


@dataclass(frozen=True)
class Pagination:
    """列表页翻页策略（契约 openQuestions：翻页参数属 W1 冒烟实测项）。"""
    strategy: str = "url"                                   # url | click
    page_param: str = "page"                                # url 策略的 query 参数名
    first_page_param: str | None = None                     # 首页是否带参；None=不带
    next_button_selectors: tuple[str, ...] = ()             # click 策略的候选选择器（≥2 级回退）
    page_settle_ms: int = 800                               # 渲染稳定等待


@dataclass(frozen=True)
class CollectOptions:
    headless: bool
    default_mode: str
    parser_version: str
    navigation_timeout_ms: int
    selector_timeout_ms: int
    pagination: Pagination
    detail_enabled: bool
    detail_store_raw: bool
    dump_html: bool
    delist_window_hours: int = 48


@dataclass(frozen=True)
class Site:
    base_url: str
    allowed_hosts: tuple[str, ...]
    listing_path: str
    detail_path: str
    respect_robots: bool
    resolve_dns: bool = True      # 导航前解析主机名并拒绝内网结果（SSRF 纵深防御）

    def _abs(self, path: str) -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def listing_url(self, game_id: int, biz_prod: int) -> str:
        url = self._abs(self.listing_path).format(game_id=game_id, biz_prod=biz_prod)
        return assert_safe_url(url, allowed_hosts=self.allowed_hosts, require_https=True,
                               resolve_dns=self.resolve_dns)

    def detail_url(self, listing_id: str) -> str:
        url = self._abs(self.detail_path).format(listing_id=listing_id)
        return assert_safe_url(url, allowed_hosts=self.allowed_hosts, require_https=True,
                               resolve_dns=self.resolve_dns)


@dataclass(frozen=True)
class Task:
    task_id: str
    name: str
    game_id: int
    biz_prod: int
    enabled: bool
    sort: str
    filter: Mapping[str, Any]
    keyword_filter: tuple[str, ...]
    pages_per_run: int
    runs_per_day: int
    notes: str = ""

    def filter_json(self) -> str:
        import json
        return json.dumps(dict(self.filter), ensure_ascii=False, sort_keys=True)

    def as_row(self) -> dict[str, Any]:
        """落 dim_task 的行（列名与 pxb7.db.DIM_TASK_COLUMNS 对齐）。"""
        return {
            "task_id": self.task_id,
            "game_id": self.game_id,
            "biz_prod": self.biz_prod,
            "task_name": self.name,
            "filter_json": self.filter_json(),
            "keyword_filter": list(self.keyword_filter),
            "sort": self.sort,
            "pages_per_run": self.pages_per_run,
            "runs_per_day": self.runs_per_day,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class TaskSet:
    tasks: tuple[Task, ...]
    source_path: Path
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def by_id(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(f"未知 task_id：{task_id}（可用：{[t.task_id for t in self.tasks]}）")

    def enabled(self) -> tuple[Task, ...]:
        return tuple(t for t in self.tasks if t.enabled)


@dataclass(frozen=True)
class Settings:
    project_root: Path
    source_path: Path
    paths: Paths
    site: Site
    rate_limit: RateLimit
    risk_control: RiskControl
    quality: Mapping[str, Any]
    snapshot_round_minutes: int
    collect: Mapping[str, Any]
    collect_opts: CollectOptions
    notify: Mapping[str, Any]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    raw: Mapping[str, Any] = field(default_factory=dict)

    # 凭据只在调用时从环境变量读取，不落内存常驻字段
    def notify_webhook(self) -> str:
        """业务/兜底 webhook；优先级：环境变量 > settings（留空即不推送）。"""
        env_name = str(self.notify.get("webhook_env") or ENV_NOTIFY_WEBHOOK)
        return (os.environ.get(env_name) or os.environ.get(ENV_NOTIFY_WEBHOOK)
                or str(self.notify.get("webhook_url") or ""))

    def channel_env(self, key: str, default: str) -> str:
        return str(self.notify.get(key) or default)

    @property
    def parser_version(self) -> str:
        return self.collect_opts.parser_version


# --------------------------------------------------------------------------- #
# settings 加载
# --------------------------------------------------------------------------- #
def _build_collect_opts(raw: Mapping[str, Any], *, settings_source: str,
                        warnings: list[str]) -> CollectOptions:
    """校验并展开 settings.collect（含翻页/详情/超时）。"""
    ctx = f"{settings_source}.collect"
    pag_raw = raw.get("pagination") or {}
    if not isinstance(pag_raw, Mapping):
        raise ConfigError(f"{ctx}.pagination 必须是映射")
    strategy = str(pag_raw.get("strategy") or "url").strip().lower()
    if strategy not in ("url", "click"):
        raise ConfigError(f"{ctx}.pagination.strategy 只能是 url / click，实际 {strategy!r}")
    page_param = str(pag_raw.get("page_param") or "page").strip()
    if not _SAFE_PARAM_RE.match(page_param):
        raise ConfigError(f"{ctx}.pagination.page_param 只允许字母数字._-（≤32 字符），实际 {page_param!r}")
    selectors_raw = pag_raw.get("next_button_selectors") or []
    if not isinstance(selectors_raw, (list, tuple)) or any(
            not isinstance(s, str) or not s.strip() for s in selectors_raw):
        raise ConfigError(f"{ctx}.pagination.next_button_selectors 必须是字符串数组")
    selectors = tuple(s.strip() for s in selectors_raw if s.strip())
    if strategy == "click" and not selectors:
        raise ConfigError(f"{ctx}.pagination.strategy=click 但 next_button_selectors 为空")
    if strategy == "url":
        warnings.append(
            f"{ctx}.pagination 使用 url 策略，参数名 '{page_param}' 待 W1 冒烟实测校准"
            "（契约 openQuestions：翻页深度与参数名属实测项）")
    first_param = pag_raw.get("first_page_param")
    pagination = Pagination(
        strategy=strategy,
        page_param=page_param,
        first_page_param=(str(first_param).strip() if first_param not in (None, "") else None),
        next_button_selectors=selectors,
        page_settle_ms=max(0, int(_as_number(pag_raw.get("page_settle_ms", 800), f"{ctx}.pagination.page_settle_ms"))),
    )

    det_raw = raw.get("detail") or {}
    if not isinstance(det_raw, Mapping):
        raise ConfigError(f"{ctx}.detail 必须是映射")
    collect_opts = CollectOptions(
        headless=bool(raw.get("headless", True)),
        default_mode=str(raw.get("default_mode") or "login"),
        parser_version=str(raw.get("parser_version") or "v0"),
        navigation_timeout_ms=int(_as_number(raw.get("navigation_timeout_ms", 45000),
                                             f"{ctx}.navigation_timeout_ms")),
        selector_timeout_ms=int(_as_number(raw.get("selector_timeout_ms", 20000),
                                           f"{ctx}.selector_timeout_ms")),
        pagination=pagination,
        detail_enabled=bool(det_raw.get("enabled", True)),
        detail_store_raw=bool(det_raw.get("store_raw", True)),
        dump_html=bool(raw.get("dump_html", True)),
        delist_window_hours=int(_as_number(raw.get("delist_window_hours", 48),
                                           f"{ctx}.delist_window_hours")),
    )
    for name, value in (("navigation_timeout_ms", collect_opts.navigation_timeout_ms),
                        ("selector_timeout_ms", collect_opts.selector_timeout_ms)):
        if value < 1000:
            raise ConfigError(f"{ctx}.{name}={value} 过小（≥1000ms）")
    if collect_opts.delist_window_hours < 1:
        raise ConfigError(f"{ctx}.delist_window_hours 必须 ≥1 小时（下架推断需保守）")
    if collect_opts.default_mode not in ("login", "guest"):
        raise ConfigError(f"{ctx}.default_mode 只能是 login / guest")
    return collect_opts


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """加载并校验 config/settings.yaml（path 为 CLI 传入时按 CWD 解析）。"""
    src = DEFAULT_SETTINGS_PATH if path is None else resolve_cli_path(path)
    data = _read_yaml(src)
    warnings: list[str] = []

    paths_raw = _require_map(data, "paths", src.name)

    def p(key: str) -> Path:
        return resolve_internal(_require_str(paths_raw, key, "settings.paths"))

    db_path = p("db")
    raw_root = p("raw_root")
    state_dir = p("state_dir")
    storage_primary = p("storage_primary")
    storage_backup = p("storage_backup")
    risk_state = p("risk_state")
    log_dir = p("log_dir")
    runs_raw = paths_raw.get("runs")
    runs = (resolve_internal(str(runs_raw)) if runs_raw
            else PROJECT_ROOT / "data" / "runs")     # 缺省兼容旧 settings.yaml

    # 数据路径自定义（看板/扩展/run.py paths 写入；优先级 settings.yaml < 覆写文件 < 环境变量）
    overrides = load_path_overrides()
    if overrides:
        db_path = Path(overrides.get("db", db_path))
        raw_root = Path(overrides.get("raw_root", raw_root))
        runs = Path(overrides.get("runs", runs))
        log_dir = Path(overrides.get("log_dir", log_dir))
        warnings.append("已应用自定义数据路径（config/paths_override.json）："
                        + "、".join(f"{k}={v}" for k, v in sorted(
                            (k, str(v)) for k, v in overrides.items())))

    # 环境变量覆写（视同 CLI 显式输入 → CWD 解析）
    if os.environ.get(ENV_DB_PATH):
        db_path = resolve_cli_path(os.environ[ENV_DB_PATH])
        warnings.append(f"{ENV_DB_PATH} 覆写了 DB 路径 → {db_path}")
    if os.environ.get(ENV_RAW_ROOT):
        raw_root = resolve_cli_path(os.environ[ENV_RAW_ROOT])
        warnings.append(f"{ENV_RAW_ROOT} 覆写了 raw 落盘根 → {raw_root}")

    if storage_primary == storage_backup:
        raise ConfigError("settings.paths 主备登录态文件不得相同（docs/01 §3.1-2 主备双号冗余）")

    site_raw = _require_map(data, "site", src.name)
    base_url = _require_str(site_raw, "base_url", "settings.site")
    hosts = site_raw.get("allowed_hosts")
    if not isinstance(hosts, (list, tuple)) or not hosts:
        raise ConfigError("settings.site.allowed_hosts 必须是非空数组（URL 守卫白名单）")
    allowed_hosts = tuple(str(h).strip() for h in hosts if str(h).strip())
    if not allowed_hosts:
        raise ConfigError("settings.site.allowed_hosts 去空后为空")
    site_paths = _require_map(site_raw, "paths", "settings.site")
    try:
        base_url = assert_safe_url(base_url, allowed_hosts=allowed_hosts, require_https=True)
    except UnsafeUrlError as exc:
        raise ConfigError(f"settings.site.base_url 未通过 URL 守卫：{exc}") from exc
    site = Site(
        base_url=base_url,
        allowed_hosts=allowed_hosts,
        listing_path=_require_str(site_paths, "listing", "settings.site.paths"),
        detail_path=_require_str(site_paths, "detail", "settings.site.paths"),
        respect_robots=bool(site_raw.get("respect_robots", True)),
        resolve_dns=bool(site_raw.get("resolve_dns", True)),
    )
    if not site.respect_robots:
        warnings.append(
            "settings.site.respect_robots=false：/_nuxt/、/assets/ 将允许作为页面采集对象；"
            "docs/01 §2 的 robots 口径是禁抓这些路径，如非明确需要请保持 true")

    rl_raw = _require_map(data, "rate_limit", src.name)
    page_interval = _as_pair(rl_raw.get("page_interval_sec"), "settings.rate_limit.page_interval_sec")
    if page_interval[0] < MIN_PAGE_INTERVAL_SEC:
        raise ConfigError(
            f"settings.rate_limit.page_interval_sec 下限 {page_interval[0]}s "
            f"低于 docs/01 §3.1-4 规定下限 {MIN_PAGE_INTERVAL_SEC}s（限速档不放松）")
    task_interval = _as_number(rl_raw.get("task_interval_sec"), "settings.rate_limit.task_interval_sec")
    if task_interval < MIN_TASK_INTERVAL_SEC:
        raise ConfigError(
            f"settings.rate_limit.task_interval_sec={task_interval} "
            f"低于 docs/01 §3.1-4 规定下限 {MIN_TASK_INTERVAL_SEC}s")
    max_pages = int(_as_number(rl_raw.get("max_pages_per_run"), "settings.rate_limit.max_pages_per_run"))
    if max_pages > MAX_PAGES_PER_RUN:
        raise ConfigError(
            f"settings.rate_limit.max_pages_per_run={max_pages} "
            f"超过 docs/01 §3.1-4 规定上限 {MAX_PAGES_PER_RUN} 页/任务/轮")
    if max_pages < 1:
        raise ConfigError("settings.rate_limit.max_pages_per_run 必须 ≥1")
    runs_pair = _as_pair(rl_raw.get("runs_per_day"), "settings.rate_limit.runs_per_day")
    runs_per_day = (int(runs_pair[0]), int(runs_pair[1]))
    if runs_per_day[1] > DOC_RUNS_PER_DAY[1]:
        warnings.append(
            f"rate_limit.runs_per_day={runs_per_day} 超出 docs/01 §3.1-4 的 "
            f"{DOC_RUNS_PER_DAY[0]}–{DOC_RUNS_PER_DAY[1]} 轮；仅捡漏类任务允许加密轮次并须同步缩小页数")

    def factor(key: str) -> float:
        val = _as_number(rl_raw.get(key, 1.0), f"settings.rate_limit.{key}")
        if val <= 0:
            raise ConfigError(f"settings.rate_limit.{key} 必须为正数")
        return val

    rate_limit = RateLimit(
        page_interval_sec=page_interval,
        task_interval_sec=task_interval,
        max_pages_per_run=max_pages,
        runs_per_day=runs_per_day,
        login_factor=factor("login_factor"),
        backup_state_factor=factor("backup_state_factor"),
        guest_factor=factor("guest_factor"),
        probe_factor=factor("probe_factor"),
    )
    if rate_limit.guest_factor != rate_limit.login_factor:
        raise ConfigError(
            "settings.rate_limit.guest_factor 必须等于 login_factor——docs/01 §3.3 要求"
            "「限速档与游客态完全一致」（登录只解锁可见性，不加压）；"
            "风控后的降频请用 probe_factor / backup_state_factor")
    if rate_limit.backup_state_factor < rate_limit.login_factor:
        raise ConfigError("settings.rate_limit.backup_state_factor 不得低于常规档（切备用只应更慢）")
    if rate_limit.probe_factor < rate_limit.login_factor:
        raise ConfigError("settings.rate_limit.probe_factor 不得低于常规档（试探只应更慢）")

    rc_raw = _require_map(data, "risk_control", src.name)

    def sub(key: str) -> Mapping[str, Any]:
        return _require_map(rc_raw, key, "settings.risk_control")

    def ratio(value: Any, ctx: str) -> float:
        v = _as_number(value, ctx)
        if not 0 < v < 1:
            raise ConfigError(f"{ctx} 必须在 (0,1) 区间，实际 {v}")
        return v

    captcha = sub("captcha")
    empty = sub("empty_response")
    ip_blocked = sub("ip_blocked")
    login_state = sub("login_state")
    parse_health = sub("parse_health")
    risk_control = RiskControl(
        captcha_stop_immediately=bool(captcha.get("stop_task_immediately", True)),
        captcha_probe_after_hours=_as_number(
            captcha.get("probe_after_hours"), "settings.risk_control.captcha.probe_after_hours"),
        captcha_escalate_after=int(_as_number(
            captcha.get("escalate_after_consecutive"),
            "settings.risk_control.captcha.escalate_after_consecutive")),
        empty_response_ratio=ratio(empty.get("ratio_threshold"),
                                   "settings.risk_control.empty_response.ratio_threshold"),
        empty_response_backoff_hours=_as_number(
            empty.get("backoff_hours"), "settings.risk_control.empty_response.backoff_hours"),
        ip_blocked_action=str(ip_blocked.get("action") or "stop_and_manual"),
        login_switch_to_backup=bool(login_state.get("switch_to_backup", True)),
        observe_hours_after_both_invalid=_as_number(
            login_state.get("observe_hours_after_both_invalid"),
            "settings.risk_control.login_state.observe_hours_after_both_invalid"),
        retry_login_forbidden=bool(login_state.get("retry_login_forbidden", True)),
        allow_concurrent_tasks=bool(login_state.get("allow_concurrent_tasks", False)),
        parse_success_ratio=ratio(parse_health.get("success_ratio_threshold"),
                                  "settings.risk_control.parse_health.success_ratio_threshold"),
    )

    quality = dict(data.get("quality") or {})
    if "snapshot_gap_max_hours" not in quality:
        warnings.append("settings.quality.snapshot_gap_max_hours 未设置，QC 默认按 36h（docs/01 §5）")

    snap_raw = data.get("snapshot") or {}
    round_minutes = int(_as_number(snap_raw.get("round_minutes", 30), "settings.snapshot.round_minutes"))
    if round_minutes < 1 or 60 % round_minutes != 0:
        raise ConfigError("settings.snapshot.round_minutes 必须能整除 60（≥1），保证轮次边界对齐整点")

    collect = dict(data.get("collect") or {})
    if str(collect.get("default_mode") or "login") not in ("login", "guest"):
        raise ConfigError("settings.collect.default_mode 只能是 login / guest")
    collect_opts = _build_collect_opts(collect, settings_source=src.name, warnings=warnings)
    notify = dict(data.get("notify") or {})
    if notify.get("webhook_url"):
        warnings.append("settings.notify.webhook_url 非空：建议留空、改从环境变量注入（凭据不入库不入 git）")

    return Settings(
        project_root=PROJECT_ROOT,
        source_path=src,
        paths=Paths(project_root=PROJECT_ROOT, db=db_path, raw_root=raw_root, state_dir=state_dir,
                    storage_primary=storage_primary, storage_backup=storage_backup,
                    risk_state=risk_state, log_dir=log_dir, runs=runs),
        site=site,
        rate_limit=rate_limit,
        risk_control=risk_control,
        quality=quality,
        snapshot_round_minutes=round_minutes,
        collect=collect,
        collect_opts=collect_opts,
        notify=notify,
        warnings=tuple(warnings),
        raw=dict(data),
    )


# --------------------------------------------------------------------------- #
# tasks 加载
# --------------------------------------------------------------------------- #
def load_tasks(path: str | os.PathLike[str] | None = None, *,
               settings: Settings | None = None) -> TaskSet:
    """加载并校验 config/tasks.yaml（path 为 CLI 传入时按 CWD 解析）。"""
    src = DEFAULT_TASKS_PATH if path is None else resolve_cli_path(path)
    settings = settings or load_settings()
    data = _read_yaml(src)
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, (list, tuple)) or not raw_tasks:
        raise ConfigError(f"{src.name} 缺少非空 tasks 列表")

    warnings: list[str] = []
    tasks: list[Task] = []
    seen: set[str] = set()

    for idx, item in enumerate(raw_tasks):
        ctx = f"{src.name}.tasks[{idx}]"
        if not isinstance(item, Mapping):
            raise ConfigError(f"{ctx} 必须是映射")
        task_id = _require_str(item, "task_id", ctx)
        if task_id in seen:
            raise ConfigError(f"{ctx}.task_id 重复：{task_id}")
        seen.add(task_id)

        game_id = int(_as_number(item.get("game_id"), f"{ctx}.game_id"))
        if game_id <= 0:
            raise ConfigError(f"{ctx}.game_id 必须为正整数")
        biz_prod = int(_as_number(item.get("biz_prod", BIZ_PROD_ACCOUNT), f"{ctx}.biz_prod"))
        if biz_prod not in BIZ_PROD_VALUES:
            warnings.append(f"{ctx}.biz_prod={biz_prod} 不在 docs/01 §2 实测取值 {BIZ_PROD_VALUES} 内")

        sort = str(item.get("sort") or "comprehensive")
        if sort not in SORT_VALUES:
            raise ConfigError(f"{ctx}.sort={sort!r} 非法，可选 {SORT_VALUES}")

        filter_map = item.get("filter") or {}
        if not isinstance(filter_map, Mapping):
            raise ConfigError(f"{ctx}.filter 必须是映射（站内筛选器配置）")
        unknown = [k for k in filter_map if k not in FILTER_KEYS]
        if unknown:
            warnings.append(f"{ctx}.filter 含未登记筛选键 {unknown}，请对照 docs/01 §2 筛选面板补登")

        keyword_filter = item.get("keyword_filter")
        if keyword_filter is None:
            keyword_filter = []
        if not isinstance(keyword_filter, (list, tuple)) or any(
                not isinstance(k, str) or not k.strip() for k in keyword_filter):
            raise ConfigError(f"{ctx}.keyword_filter 必须是字符串数组（可为空数组）")

        pages_per_run = int(_as_number(item.get("pages_per_run"), f"{ctx}.pages_per_run"))
        if pages_per_run < 1:
            raise ConfigError(f"{ctx}.pages_per_run 必须 ≥1")
        if pages_per_run > settings.rate_limit.max_pages_per_run:
            raise ConfigError(
                f"{ctx}.pages_per_run={pages_per_run} 超过 settings.rate_limit.max_pages_per_run="
                f"{settings.rate_limit.max_pages_per_run}（docs/01 §3.1-4：每任务每轮 ≤5 页）")

        runs_per_day = int(_as_number(item.get("runs_per_day"), f"{ctx}.runs_per_day"))
        if runs_per_day < 1:
            raise ConfigError(f"{ctx}.runs_per_day 必须 ≥1")
        if runs_per_day > DOC_RUNS_PER_DAY[1]:
            warnings.append(
                f"{ctx}.runs_per_day={runs_per_day} 超出 docs/01 §3.1-4 的 2–4 轮档")

        task = Task(
            task_id=task_id,
            name=_require_str(item, "name", ctx),
            game_id=game_id,
            biz_prod=biz_prod,
            enabled=bool(item.get("enabled", True)),
            sort=sort,
            filter=dict(filter_map),
            keyword_filter=tuple(k.strip() for k in keyword_filter),
            pages_per_run=pages_per_run,
            runs_per_day=runs_per_day,
            notes=str(item.get("notes") or ""),
        )
        # 任务 URL 必须过守卫（构建即校验）
        settings.site.listing_url(task.game_id, task.biz_prod)
        tasks.append(task)

    if not any(t.enabled for t in tasks):
        warnings.append("tasks.yaml 中没有任何 enabled 任务")

    return TaskSet(tasks=tuple(tasks), source_path=src, warnings=tuple(warnings))


def load_config(settings_path: str | os.PathLike[str] | None = None,
                tasks_path: str | os.PathLike[str] | None = None) -> tuple[Settings, TaskSet]:
    """一次加载 settings + tasks（tasks 校验依赖 settings）。"""
    settings = load_settings(settings_path)
    tasks = load_tasks(tasks_path, settings=settings)
    return settings, tasks


def override_db_path(settings: Settings, db_path: str | os.PathLike[str]) -> Settings:
    """CLI --db：按 CWD 解析后覆写（其余字段不变）。"""
    resolved = resolve_cli_path(db_path)
    return dataclasses.replace(settings, paths=dataclasses.replace(settings.paths, db=resolved))


def all_warnings(settings: Settings, tasks: TaskSet | None = None) -> list[str]:
    """汇总 settings 与 tasks 加载阶段的非阻断告警。"""
    out = list(settings.warnings)
    if tasks is not None:
        out.extend(tasks.warnings)
    return out
