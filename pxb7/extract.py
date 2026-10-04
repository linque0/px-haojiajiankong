"""词表抽取：config/keywords_seed.yaml → dim_keyword → listing 级命中与结构化特征。

职责（契约第 1 项）：
1. 读 ``config/keywords_seed.yaml``（docs/02 §4.A 原神词表 v0 的保真抄录），落 dim_keyword；
2. 对每条 listing 的**标题 + 卡片属性**跑规则，产出：
   - ``extracted_features``：JSON（如命座数/精炼等级/资源存量/地板与折价标记）；
   - ``fct_listing_keyword`` 命中记录（含 hit_text 原文，保证可复核）；
3. 输出命中率统计（契约口径：**标题/卡片文本命中 ≥1 个词表关键词**的 listing 数 / cards_parsed）。

口径说明（终审发现项 #13/#38 的修正）：
- [卡/筛] 词条（原石/纠缠之源）从卡片字段直取时**仍写入** fct_listing_keyword（它们是词表
  条目、hit_text 如实标注来源），但**不进入**契约口径 extract_hit_rate——几乎所有卡片都有
  原石/纠缠数值，若计入，命中率会被稀释成「有资源字段的比例」，掩盖标题抽取的真实能力；
- 卡片字段通道的覆盖面另以 ``card_field_coverage`` 披露（诊断口径）。

口径与红线：
- 只做词表匹配与数值归一，**不臆造文档未定义的锚点**（种子逐条对应 docs/02 §4.A0/A1）；
- 未命中的特征不写入 JSON（不用 0 冒充）；
- [卡/筛] 类词条（原石/纠缠之源）优先读卡片字段，hit_text 记录来源字段名而非伪造原文；
- 正则来自本项目种子文件，编译失败只告警跳过，不中断整轮。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field as _dc_field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .config import PROJECT_ROOT

KEYWORDS_SEED_PATH = PROJECT_ROOT / "config" / "keywords_seed.yaml"

KEYWORD_TYPES = ("ceiling", "floor", "risk", "resource", "segment")
PRICE_ANCHORS = ("ceiling", "floor", "none")
KEYWORD_COLUMNS = (
    "keyword_id", "profile_id", "keyword", "keyword_type", "extract_pattern",
    "feature_map", "price_anchor", "weight_v0", "weight_v1", "weight_v2",
    "source", "enabled", "updated_at",
)

# [卡/筛] 词条的取值通道：命中优先读卡片字段（docs/02 §4.A1 标注）
CARD_FEATURE_SOURCES: dict[str, str] = {
    "account_level_cnt": "level",
    "yellow_cnt": "yellow_cnt",
    "five_star_chars_cnt": "five_star_chars",
    "five_star_weapons_cnt": "five_star_weapons",
    "primogems_cnt": "primogems",
    "intertwined_fate_cnt": "intertwined_fate",
}

# 满命语义：§4.A0 把「命座/满命/6命」列为同一槽位 → 满/六 归一为 6（原神五星满命）
_MAX_CONSTELLATION = 6
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_INT_RE = re.compile(r"\d+")


class ExtractError(RuntimeError):
    """词表种子/抽取配置错误。"""


@dataclass(frozen=True)
class Keyword:
    keyword_id: str
    profile_id: str
    keyword: str
    keyword_type: str
    extract_pattern: str | None = None
    feature_map: str | None = None
    price_anchor: str | None = None
    weight_v0: float | None = None
    weight_v1: float | None = None
    weight_v2: float | None = None
    source: str | None = None
    enabled: bool = True
    updated_at: str | None = None

    def as_db_row(self) -> dict[str, Any]:
        row = {c: getattr(self, c) for c in KEYWORD_COLUMNS}
        row["enabled"] = bool(self.enabled)
        return row


@dataclass
class KeywordHit:
    listing_id: str
    keyword_id: str
    keyword: str
    keyword_type: str
    feature_map: str | None
    hit_text: str
    value: Any = None
    via: str = "text"          # text（标题/卡片文本命中）| card_field（[卡] 字段取值）

    def as_db_row(self, snapshot_at: _dt.datetime) -> dict[str, Any]:
        return {"snapshot_at": snapshot_at, "listing_id": self.listing_id,
                "keyword_id": self.keyword_id, "hit_text": self.hit_text}


@dataclass
class Extraction:
    listing_id: str
    features: dict[str, Any] = _dc_field(default_factory=dict)
    hits: list[KeywordHit] = _dc_field(default_factory=list)

    @property
    def hit(self) -> bool:
        """任一通道命中（文本或卡片字段）：决定是否写特征/桥表行。"""
        return bool(self.hits) or bool(self.features)

    @property
    def text_hit(self) -> bool:
        """契约口径：标题/卡片**文本**命中 ≥1 个词表关键词（card_field 通道不计入）。"""
        return any(h.via == "text" for h in self.hits)

    def features_json(self) -> str | None:
        return json.dumps(self.features, ensure_ascii=False, sort_keys=True) if self.features \
            else None

    def hit_rows(self, snapshot_at: _dt.datetime) -> list[dict[str, Any]]:
        return [h.as_db_row(snapshot_at) for h in self.hits]


# --------------------------------------------------------------------------- #
# 种子加载
# --------------------------------------------------------------------------- #
def load_seed(path: str | Path | None = None) -> dict[str, Any]:
    """读 keywords_seed.yaml（顶层 version/generated_from + profiles）。"""
    src = Path(path) if path else KEYWORDS_SEED_PATH
    if not src.is_file():
        raise ExtractError(f"词表种子不存在：{src}")
    try:
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExtractError(f"词表种子 YAML 解析失败：{src}：{exc}") from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("profiles"), list):
        raise ExtractError(f"词表种子缺少 profiles 列表：{src}")
    return dict(data)


def seed_keywords(path: str | Path | None = None,
                  *, profile_id: str | None = None,
                  game_id: int | None = None,
                  include_disabled: bool = True) -> list[Keyword]:
    """展开种子为 Keyword 列表（默认含 disabled，便于落库保留占位行）。

    game_id 过滤：只取该游戏画像的词表；该游戏尚无画像时返回空列表
    （docs/02 §G：未建词表的游戏不预填、不用别的游戏的词表顶替）。
    """
    data = load_seed(path)
    out: list[Keyword] = []
    seen: set[str] = set()
    for profile in data["profiles"]:
        if not isinstance(profile, Mapping):
            raise ExtractError("profiles 项必须是映射")
        pid = str(profile.get("profile_id") or "").strip()
        if not pid:
            raise ExtractError("profile 缺少 profile_id")
        if profile_id and pid != profile_id:
            continue
        if game_id is not None:
            try:
                profile_game = int(profile.get("game_id"))
            except (TypeError, ValueError):
                continue
            if profile_game != int(game_id):
                continue
        for item in profile.get("keywords") or []:
            if not isinstance(item, Mapping):
                raise ExtractError(f"{pid} 的 keywords 项必须是映射")
            kw = Keyword(
                keyword_id=str(item.get("keyword_id") or "").strip(),
                profile_id=pid,
                keyword=str(item.get("keyword") or "").strip(),
                keyword_type=str(item.get("keyword_type") or "").strip(),
                extract_pattern=(str(item["extract_pattern"]).strip()
                                 if item.get("extract_pattern") else None),
                feature_map=(str(item["feature_map"]).strip() if item.get("feature_map") else None),
                price_anchor=(str(item["price_anchor"]).strip()
                              if item.get("price_anchor") is not None else "none"),
                weight_v0=_as_float(item.get("weight_v0")),
                weight_v1=_as_float(item.get("weight_v1")),
                weight_v2=_as_float(item.get("weight_v2")),
                source=(str(item["source"]).strip() if item.get("source") else None),
                enabled=bool(item.get("enabled", True)),
                updated_at=(str(item["updated_at"]) if item.get("updated_at") else None),
            )
            _validate_keyword(kw, source=str(path or KEYWORDS_SEED_PATH))
            if kw.keyword_id in seen:
                raise ExtractError(f"keyword_id 重复：{kw.keyword_id}")
            seen.add(kw.keyword_id)
            out.append(kw)
    if not out and game_id is None:
        raise ExtractError("词表种子未展开出任何关键词")
    return out


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_keyword(kw: Keyword, *, source: str) -> None:
    if not kw.keyword_id or not kw.keyword:
        raise ExtractError(f"{source}：keyword_id/keyword 不得为空（{kw!r}）")
    if kw.keyword_type not in KEYWORD_TYPES:
        raise ExtractError(
            f"{source}：{kw.keyword_id} 的 keyword_type={kw.keyword_type!r} 不在 {KEYWORD_TYPES}")
    if kw.price_anchor not in PRICE_ANCHORS:
        raise ExtractError(
            f"{source}：{kw.keyword_id} 的 price_anchor={kw.price_anchor!r} 不在 {PRICE_ANCHORS}")
    if kw.extract_pattern:
        try:
            re.compile(kw.extract_pattern)
        except re.error as exc:
            raise ExtractError(f"{source}：{kw.keyword_id} 正则非法：{exc}") from exc


def compile_keywords(keywords: Sequence[Keyword]) -> tuple[list[tuple[Keyword, re.Pattern]],
                                                           list[str]]:
    """编译启用中的关键词；返回 (已编译, 告警列表)。"""
    compiled: list[tuple[Keyword, re.Pattern]] = []
    warnings: list[str] = []
    for kw in keywords:
        if not kw.enabled:
            continue
        if not kw.extract_pattern:
            warnings.append(f"{kw.keyword_id} 已启用但无 extract_pattern，跳过")
            continue
        try:
            compiled.append((kw, re.compile(kw.extract_pattern)))
        except re.error as exc:
            warnings.append(f"{kw.keyword_id} 正则编译失败（{exc}），跳过")
    return compiled, warnings


def seed_db_rows(path: str | Path | None = None) -> list[dict[str, Any]]:
    """种子 → dim_keyword 行（与 db.upsert_dim_keywords 对齐）。"""
    return [kw.as_db_row() for kw in seed_keywords(path)]


# --------------------------------------------------------------------------- #
# 取值归一
# --------------------------------------------------------------------------- #
def _cn_to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _CN_DIGITS.get(token)


def _convert_value(feature_map: str | None, raw: str) -> Any:
    """按 feature_map 语义把命中原文归一为特征值（1 个捕获组即命中值）。"""
    text = (raw or "").strip()
    if feature_map == "constellation_cnt":
        if "满" in text:
            return _MAX_CONSTELLATION          # §4.A0：满命 = 6 命
        m = _INT_RE.search(text)
        if m:
            return int(m.group(0))
        for ch in text:
            val = _cn_to_int(ch)
            if val:
                return val
        return None
    if feature_map == "five_star_weapon_refined":
        if "满" in text:
            return 5                            # 满精 = 精 5（§4.A0：满叠影=5 阶语义）
        m = _INT_RE.search(text)
        if m:
            return int(m.group(0))
        for ch in text:
            val = _cn_to_int(ch)
            if val:
                return val
        return None
    if feature_map and feature_map.endswith("_cnt"):
        m = _INT_RE.search(text.replace(",", ""))
        return int(m.group(0)) if m else None
    if feature_map and feature_map.endswith("_flag"):
        return True
    return True if text else None


def listing_text(title: str | None, card_fields: Mapping[str, Any] | None) -> str:
    """抽取输入：标题 + 卡片属性文本（卡片数值也参与，供「原石 32000」类模式回落）。"""
    parts: list[str] = []
    if title:
        parts.append(str(title))
    for key, value in (card_fields or {}).items():
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (list, tuple)):
            parts.append(" ".join(str(v) for v in value))
        else:
            parts.append(f"{key} {value}")
    return " ".join(parts)


WUWA_PAID_ITEMS = (
    ("vehicle_frame_modules", "车架模组"),
    ("motorcycle_ornaments", "摩托饰品"),
    ("character_skins", "人物皮肤"),
)

# 鸣潮资源五件套（feature 键 → 展示名）。资源字段口径见 docs/02 §4.A4：
# 采到的 0 是值（docs/09 零值纪律），未采到为 None（不填 0）。
WUWA_RESOURCES = (
    ("astrite_cnt", "星声"),
    ("lunite_cnt", "月相"),
    ("afterglow_coral_cnt", "余波珊瑚"),
    ("lustrous_tide_cnt", "浮金波纹"),
    ("radiant_tide_cnt", "铸潮波纹"),
)


def wuwa_paid_item_features(text: str) -> dict[str, Any]:
    """仅从明确的具名段提取商品；服饰是站点的人物皮肤段名。"""
    aliases = {"车架模组": "vehicle_frame_modules", "摩托饰品": "motorcycle_ornaments",
               "人物皮肤": "character_skins", "角色皮肤": "character_skins", "服饰": "character_skins"}
    headings = list(re.finditer(r"(车架模组|摩托饰品|人物皮肤|角色皮肤|服饰|涂装)\s*[:：]", text))
    features: dict[str, Any] = {}
    for i, heading in enumerate(headings):
        key = aliases.get(heading.group(1))
        if key is None:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section = re.split(r"[;；\n]|详情看图|官方截图|点击查看更多|【|\[", text[heading.end():end])[0]
        names = [name.strip() for name in re.split(r"[,，、]", section) if name.strip()]
        if names:
            features[key] = list(dict.fromkeys([*features.get(key, []), *names]))
    return features


_ROSTER_TAIL_NOISE = re.compile(
    r"[【\[（(].*$|官方截图.*|详情看图.*|点击查看更多.*|[。．.,，、;；\s]+$")
_ROSTER_NAME = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9·‧・]{1,24}$")
_ROSTER_NON_NAME = re.compile(
    r"[五四]星|武器|角色|车架|摩托|涂装|服饰|皮肤|音擎|光锥|等|共|售|价|浏览")
_ROSTER_HEADING = re.compile(r"(?:\d+\s*个?\s*)?([五四]星(?:角色|武器))\s*[:：]")


def _zero_chain_name(item: str) -> str | None:
    """五星角色段内未标注升格的名字清洗（2026-10-04 用户规则：未标注即 0命）。

    像角色名才收；纯数字、杂词、被截断的具名升格条目（如孤立的「3命」）不冒充 0命。
    """
    if re.search(r"[.…⋯]", item):
        return None                    # 截断的角色名不按完整名字补入。
    name = _ROSTER_TAIL_NOISE.sub("", item).strip()
    if not name or name.isdigit() or name in {"无", "暂无", "没有", "未知", "未提供", "未标注"}:
        return None
    if re.match(r"^(?:满命|满链|(?:[0-6零一二三四五六])\s*(?:命|链)|共鸣链\s*[0-6])", name):
        return None
    if not _ROSTER_NAME.match(name) or _ROSTER_NON_NAME.search(name):
        return None
    return name


def roster_features(text: str) -> dict[str, Any]:
    """按五星/四星段解析具名升格；不把四星满命写成五星链数。重复武器保留。

    五星角色段内未标注 N命/满命 的角色按 0命 记入链数列表（值 0，保持原文顺序）；
    五星武器段不猜精0，仍只收具名精炼。
    """
    headings = list(_ROSTER_HEADING.finditer(text))
    features: dict[str, Any] = {}
    for i, heading in enumerate(headings):
        kind = heading.group(1)
        if kind not in ("五星角色", "五星武器"):
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section = re.split(r"[;；]|车架模组|摩托饰品|详情看图|(?:服饰|人物皮肤|角色皮肤|涂装)\s*[:：]", text[heading.end():end])[0]
        entries = []
        pattern = (r"^(满命|满链|(?:[0-6零一二三四五六])\s*(?:命|链)|共鸣链\s*[0-6])\s*(.+)$"
                   if kind == "五星角色" else
                   r"^(满精|精\s*[1-5一二三四五]|谐振\s*[1-5一二三四五](?:阶)?)\s*(.+)$")
        feature = "constellation_cnt" if kind == "五星角色" else "five_star_weapon_refined"
        for item in re.split(r"[,，、]", section):
            match = re.match(pattern, item.strip())
            if match:
                value = _convert_value(feature, match.group(1))
                if value is not None:
                    entries.append({"name": match.group(2).strip(), "value": value})
            elif kind == "五星角色":
                name = _zero_chain_name(item)
                if name:
                    entries.append({"name": name, "value": 0})
        if entries:
            key = "five_star_character_chains" if kind == "五星角色" else "five_star_weapon_refinements"
            features[key] = entries
            features[feature] = max(item["value"] for item in entries)
    return features


def extract_listing(keywords: Sequence[Keyword], *, listing_id: str,
                    title: str | None = None,
                    card_fields: Mapping[str, Any] | None = None) -> Extraction:
    """对单条 listing 跑词表：标题/卡片文本命中 + [卡] 字段取值 → 特征 + 命中记录。"""
    compiled, _warnings = compile_keywords(keywords)
    text = listing_text(title, card_fields)
    cards = dict(card_fields or {})
    out = Extraction(listing_id=listing_id)

    for kw, pattern in compiled:
        matched = None
        try:
            m = pattern.search(text)
        except Exception:                       # 单条规则异常不影响其它规则
            m = None
        if m:
            raw = m.group(1) if (m.groups() and m.group(1) is not None) else m.group(0)
            value = _convert_value(kw.feature_map, raw)
            matched = KeywordHit(listing_id=listing_id, keyword_id=kw.keyword_id,
                                 keyword=kw.keyword, keyword_type=kw.keyword_type,
                                 feature_map=kw.feature_map, hit_text=raw, value=value,
                                 via="text")
        # [卡/筛] 通道：文本未命中但卡片字段有值 → 仍算词表命中（来源如实标注）
        if matched is None and kw.feature_map in CARD_FEATURE_SOURCES:
            field = CARD_FEATURE_SOURCES[kw.feature_map]
            raw_value = cards.get(field)
            if raw_value not in (None, ""):
                try:
                    value = int(str(raw_value).replace(",", ""))
                except ValueError:
                    value = raw_value
                matched = KeywordHit(listing_id=listing_id, keyword_id=kw.keyword_id,
                                     keyword=kw.keyword, keyword_type=kw.keyword_type,
                                     feature_map=kw.feature_map,
                                     hit_text=f"{kw.keyword}=卡片字段 {field} 值 {raw_value}",
                                     value=value, via="card_field")
        if matched is None:
            continue
        out.hits.append(matched)
        if kw.feature_map:
            if matched.value is None:
                continue                        # 归一失败不写特征（不猜）
            if kw.feature_map in out.features and not isinstance(out.features[kw.feature_map], bool):
                continue                        # 首次命中优先，避免同类词条互相覆盖
            out.features[kw.feature_map] = matched.value
    if any(kw.profile_id in ("wuwa_10302", "genshin_10026") for kw, _ in compiled):
        # 存量标量保留兼容；有段式标题时，只允许五星段贡献角色链数。
        if any(heading.group(1).endswith("角色") for heading in _ROSTER_HEADING.finditer(title or "")):
            out.features.pop("constellation_cnt", None)
        out.features.update(roster_features(title or ""))
    if any(kw.profile_id == "wuwa_10302" for kw, _ in compiled):
        out.features.update(wuwa_paid_item_features(title or ""))
    return out


def extract_many(keywords: Sequence[Keyword],
                 listings: Iterable[Mapping[str, Any]]) -> list[Extraction]:
    """批量抽取；listings 每项含 listing_id/title/card_fields。"""
    return [extract_listing(keywords, listing_id=str(item.get("listing_id")),
                            title=item.get("title"), card_fields=item.get("card_fields"))
            for item in listings]


# --------------------------------------------------------------------------- #
# 命中率统计
# --------------------------------------------------------------------------- #
@dataclass
class ExtractStats:
    listings: int = 0
    hit_listings: int = 0                  # 任一通道命中（文本 或 卡片字段）
    text_hit_listings: int = 0             # 词表文本命中（契约口径分子）
    card_field_listings: int = 0           # 有 [卡] 字段通道命中的 listing 数（诊断口径）
    feature_listings: int = 0
    hits_by_keyword: dict[str, int] = _dc_field(default_factory=dict)
    hits_by_type: dict[str, int] = _dc_field(default_factory=dict)
    hits_by_via: dict[str, int] = _dc_field(default_factory=dict)   # text vs card_field

    @property
    def extract_hit_rate(self) -> float:
        """契约口径：文本命中 ≥1 词表关键词 / listings（分母 0 记 0）。

        卡片字段通道（原石/纠缠之源等结构性数值）不计入——否则几乎所有卡片都会
        「命中」，指标被稀释成『有资源字段的比例』（终审发现项 #13）。
        """
        return (self.text_hit_listings / self.listings) if self.listings else 0.0

    @property
    def any_hit_rate(self) -> float:
        """诊断口径：任一通道命中（文本 或 卡片字段）/ listings。"""
        return (self.hit_listings / self.listings) if self.listings else 0.0

    @property
    def keyword_hit_rate(self) -> float:
        """兼容别名（= any_hit_rate）：任一通道的关键词命中（含 card_field）。"""
        return self.any_hit_rate

    @property
    def card_field_coverage(self) -> float:
        """诊断口径：有 [卡] 字段通道命中的 listing 占比（揭示契约口径被稀释的程度）。"""
        return (self.card_field_listings / self.listings) if self.listings else 0.0

    @property
    def card_field_share(self) -> float:
        """命中条目里来自卡片字段直取的比例（原石/纠缠之源等 [卡/筛] 词条）。"""
        total = sum(self.hits_by_via.values())
        return (self.hits_by_via.get("card_field", 0) / total) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "listings": self.listings,
            "hit_listings": self.hit_listings,
            "extract_hit_rate": round(self.extract_hit_rate, 4),
            "text_hit_listings": self.text_hit_listings,
            "card_field_listings": self.card_field_listings,
            "card_field_coverage": round(self.card_field_coverage, 4),
            "any_hit_rate": round(self.any_hit_rate, 4),
            "feature_listings": self.feature_listings,
            "hits_by_via": dict(sorted(self.hits_by_via.items())),
            "card_field_share": round(self.card_field_share, 4),
            "hits_by_keyword": dict(sorted(self.hits_by_keyword.items(),
                                           key=lambda kv: (-kv[1], kv[0]))),
            "hits_by_type": dict(sorted(self.hits_by_type.items())),
        }


def summarize(extractions: Sequence[Extraction]) -> ExtractStats:
    stats = ExtractStats(listings=len(extractions))
    for item in extractions:
        if item.hit:
            stats.hit_listings += 1
        if item.text_hit:
            stats.text_hit_listings += 1
        if any(h.via == "card_field" for h in item.hits):
            stats.card_field_listings += 1
        if item.features:
            stats.feature_listings += 1
        for hit in item.hits:
            stats.hits_by_keyword[hit.keyword] = stats.hits_by_keyword.get(hit.keyword, 0) + 1
            stats.hits_by_type[hit.keyword_type] = stats.hits_by_type.get(hit.keyword_type, 0) + 1
            stats.hits_by_via[hit.via] = stats.hits_by_via.get(hit.via, 0) + 1
    return stats


def extra_hit_rate(hit_listings: int, cards_parsed: int) -> float:
    """契约公式的独立实现（供 pipeline 直接使用，分母 0 记 0）。"""
    return (hit_listings / cards_parsed) if cards_parsed else 0.0
