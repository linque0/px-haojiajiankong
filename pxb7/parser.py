"""DOM 解析：列表卡片 → 结构化字段（以运行期契约 cardFields 为准）。

契约来源：工作流 specReader 节点产出的 ImplContract.cardFields（20 个字段，
生成依据 docs/01 §2 卡片实测 + §4 快照列 + docs/02/03 属性语义）。字段清单见 CONTRACT_FIELDS。

红线与口径：
- **只解析渲染后的 DOM**（page.content() 落盘的 raw HTML），不调用任何带签名的接口（docs/01 §2/§3.2）；
- 缺失字段写显式 NULL 并计入缺失明细，**不猜、不填默认值**（docs/01 §4）；
- 单张卡片解析失败只计数、不中断整轮（契约：卡片级解析失败计数但不中断整轮）；
- 每张卡片带 parser_version，站点改版可重放 raw。

选择器回退策略（每字段 ≥2 级，集中在 CARD_SELECTORS / FIELD_SPECS）：
  L1 语义属性/类名：data-* 属性、class*=语义片段、itemprop —— 站内改名时最稳的一层；
  L2 文本模式：对整卡文本跑正则（『¥1,234』『五星角色 12』『邮箱出售』类）；
  L3 兜底（仅个别字段）：宽松正则/结构启发（如 listing_id 从 /product/{id} 链接取）。

冒烟步骤将用真实 DOM 校准这两层；校准只改本文件的常量，不改调用方。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as _dc_field
from typing import Any, Iterable, Sequence

from bs4 import BeautifulSoup, Tag

try:  # soupsieve 随 bs4 安装；缺失时退化为「不含卡片自身」的选择
    from soupsieve import match as _ss_match
except Exception:  # pragma: no cover
    _ss_match = None

PARSER_VERSION = "v0.2.0"   # v0.2.0：真实 DOM 校准（语义属性优先 + 隐藏弹层剔除），2026-10-03
_SAMPLE_LIMIT = 3          # 每个字段保留的样例条数（校准用）

# 契约 cardFields：字段名 → 快照列名（顺序即契约顺序）
CONTRACT_FIELDS: tuple[str, ...] = (
    "price_yuan", "publish_time_text", "level", "yellow_cnt", "mc_gender",
    "five_star_chars", "five_star_weapons", "intertwined_fate", "primogems",
    "artifacts", "skins", "server", "mail_status", "tap_status", "psn_status",
    "trade_code_status", "has_compensation", "official_verified", "img_cnt",
    "featured_chars",
)
# 契约 required=true 的字段（缺失即视为关键字段缺失，进 QC 缺失率）
REQUIRED_FIELDS: tuple[str, ...] = (
    "price_yuan", "publish_time_text", "level", "yellow_cnt",
    "five_star_chars", "five_star_weapons", "server", "mail_status", "featured_chars",
)
# 非契约但入库/后续环节必需
EXTRA_FIELDS: tuple[str, ...] = ("listing_id", "title")


# --------------------------------------------------------------------------- #
# 选择器 / 正则（集中定义；冒烟按真实 DOM 校准）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Strategy:
    """单条取值策略。kind: css（取属性或文本）| regex（对整卡文本）。"""
    kind: str
    name: str
    selector: str | None = None
    attr: str | None = None
    pattern: str | None = None
    group: int = 1
    all_matches: bool = False      # list 型字段：收集全部命中节点文本
    max_len: int | None = None     # 取到的原文超过该长度视为噪声，跳过该节点
    scale: float = 1.0             # 数值缩放（如 price 属性单位为分 → ×0.01）
    note: str = ""


def _css(name: str, selector: str, *, attr: str | None = None,
         max_len: int | None = 16, scale: float = 1.0, note: str = "") -> Strategy:
    return Strategy(kind="css", name=name, selector=selector, attr=attr,
                    max_len=max_len, scale=scale, note=note)


def _re(name: str, pattern: str, *, group: int = 1, note: str = "") -> Strategy:
    return Strategy(kind="regex", name=name, pattern=pattern, group=group, note=note)


# 卡片容器候选（按序尝试，取第一个能选出 ≥1 个非嵌套节点的）
CARD_SELECTORS: tuple[str, ...] = (
    "[data-listing-id]",                                   # L1 语义属性
    "a[href*='/product/']",                                # L1 详情链接（Spa 列表卡片多为整卡链接）
    "li[class*='card'], div[class*='card']",               # L2 类名
    "div[class*='list'] > div[class*='item'], div[class*='product']",  # L3 结构兜底
)

# 价格：¥ 符号最强（契约口径：挂牌价，非成交价）
_PRICE_NUM = r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)"

FIELD_SPECS: dict[str, tuple[Strategy, ...]] = {
    # 2026-10-03 真实 DOM 校准：卡片节点自带语义数据属性（div.smallCardTitle 上的
    # price[分]/productname/important/pcimgcount、div.middleCard 上的 productid/createtime），
    # 属性层最稳，放最前；文本正则保留为回退层。
    "price_yuan": (
        _css("L1-attr", "[price]", attr="price", scale=0.01,
             note="smallCardTitle price 属性（单位：分，÷100=元）"),
        _css("L1-class", "[class*='price'], [class*='Price'], [itemprop='price']",
             note="价格区类名"),
        _re("L2-text", r"[¥￥]\s*" + _PRICE_NUM, note="¥ 数字（隐藏弹层已剔除，首个 ￥ 即挂牌价）"),
        _re("L3-text", r"([0-9][0-9,]{2,})\s*元", note="『N 元』兜底"),
    ),
    "publish_time_text": (
        _css("L1-attr", "[createtime]", attr="createtime", max_len=None,
             note="middleCard createtime（精确发布时间，优于『N分钟内发布』）"),
        _css("L1-class", "[class*='time'], [class*='Time'], [class*='publish'], [class*='date']",
             note="时间标签类名，原文入 raw"),
        _re("L2-text",
            r"((?:\d+\s*(?:秒|分钟|小时|天)(?:前|内)?发布)|刚刚发布|今天发布|\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            note="『21分钟内发布』型原文"),
    ),
    "level": (
        _css("L1-class", "[class*='level'], [class*='Level'], [class*='grade']", note="等级类名"),
        _re("L2-text", r"(?:冒险等级|联觉等级|等级|Lv\.?|LV)\s*[:：]?\s*(\d{1,3})(?!\d)", note="『等级 60』"),
        _re("L3-text", r"(?<!\d)(\d{1,3})\s*级", note="『80级』"),
    ),
    "yellow_cnt": (
        _css("L1-attr", "[data-yellow], [class*='yellow'], [class*='Yellow']", note="黄数类名/属性"),
        _re("L2-text", r"黄数\s*[:：]?\s*(\d{1,4})", note="『黄数 12』"),
        _re("L3-text", r"(?<!\d)(\d{1,4})\s*黄(?!数)", note="『12黄』兜底"),
    ),
    "mc_gender": (
        _css("L1-class", "[class*='gender'], [class*='Gender'], [class*='sex']", note="性别类名"),
        _re("L2-text", r"(男主|女主|男号|女号)", note="主角性别（契约 required=false，缺则 NULL）"),
    ),
    "five_star_chars": (
        _css("L1-class", "[class*='five-star-char'], [class*='fiveStarChar'], [class*='star-char']",
             note="五星角色类名"),
        _re("L2-text", r"五星角色(?:数量)?\s*[:：]?\s*(\d{1,3})(?!\d|\s*(?:命|链|个\s*(?:五星|四星)))", note="『五星角色 12』"),
        _re("L3-text", r"(?<!\d)(\d{1,3})\s*个?\s*五星角色(?!\s*\d)", note="『12个五星角色』兜底"),
    ),
    "five_star_weapons": (
        _css("L1-class", "[class*='five-star-weapon'], [class*='fiveStarWeapon'], [class*='star-weapon']",
             note="五星武器类名"),
        _re("L2-text", r"五星武器(?:数量)?\s*[:：]?\s*(\d{1,3})(?!\d|\s*(?:精|阶|个\s*(?:五星|四星)))", note="『五星武器 5』"),
        _re("L3-text", r"(?<!\d)(\d{1,3})\s*个?\s*五星武器(?!\s*\d)", note="兜底"),
    ),
    "intertwined_fate": (
        _css("L1-class", "[class*='intertwined'], [class*='fate']", note="纠缠之源类名"),
        _re("L2-text", r"纠缠(?:之源)?\s*[:：]?\s*(\d{1,6})", note="『纠缠之源 120』"),
    ),
    "primogems": (
        _css("L1-class", "[class*='primogem'], [class*='stone']", note="原石类名"),
        _re("L2-text", r"原石\s*[:：]?\s*(\d{1,7})", note="『原石 32000』"),
    ),
    "artifacts": (
        _css("L1-class", "[class*='artifact'], [class*='relic']", note="圣遗物类名"),
        _re("L2-text", r"圣遗物\s*[:：]?\s*(\d{1,4})", note="『圣遗物 300』"),
    ),
    "skins": (
        _css("L1-class", "[class*='skin'], [class*='fashion']", note="时装类名"),
        # 真实 DOM 文案是「时装数量2」（校准：数量二字可选）
        _re("L2-text", r"时装(?:数量)?\s*[:：]?\s*(\d{1,3})", note="『时装数量 2』/『时装 8』"),
    ),
    "server": (
        _css("L1-class", "[class*='server'], [class*='Server'], [class*='region'], [class*='zone']",
             note="区服类名（分域硬键）"),
        _re("L2-text",
            r"(官服|B服|b服|小米服|国际服|渠道服|QQ服|微信服|华为服|OPPO服|vivo服|应用宝服|九游服)",
            note="已知区服枚举（§2 实测：官服/B服/小米服/国际服及子区）"),
        _re("L3-text", r"([\u4e00-\u9fa5]{1,4}服)", note="『X服』兜底"),
    ),
    "mail_status": (
        _css("L1-class", "[class*='mail'], [class*='email'], [class*='Mail']", note="邮箱状态类名"),
        # 2026-10-03 校准：真实卡片状态枚举（长词在前，防止子串抢先命中）
        _re("L2-text",
            r"(网易邮箱出售|QQ邮箱不出售|邮箱不出售|邮箱未绑定|邮箱已注销|邮箱实名|邮箱出售"
            r"|网易邮箱|QQ邮箱|未实名|可二次实名|二次实名|死邮|活邮|不出邮|不送邮|送邮)",
            note="邮箱状态（8 态 + 站点实际文案；校准自真实卡片）"),
    ),
    "tap_status": (
        _css("L1-class", "[class*='tap']", note="TAP 绑定类名"),
        # 校准：必须捕获**完整状态短语**（此前只捕获到 "TAP"，丢失 未绑定/送/已注销 状态）
        _re("L2-text", r"(已注销TAP|未绑定TAP|送TAP|不送TAP|已绑定TAP|可换绑TAP|TAP)",
            note="TAP 绑定状态（完整短语）"),
    ),
    "psn_status": (
        _css("L1-class", "[class*='psn']", note="PSN 绑定类名"),
        _re("L2-text", r"(未绑定PSN|送PSN|不送PSN|已绑定PSN|可换绑PSN|PSN)",
            note="PSN 绑定状态（完整短语）"),
    ),
    "trade_code_status": (
        _css("L1-class", "[class*='trade-code'], [class*='tradeCode'], [class*='change-bind']",
             note="换绑码类名"),
        _re("L2-text", r"(提供换绑码|无换绑CD|可换绑|无换绑码|不可换绑|换绑码)",
            note="换绑码状态（溢价结构项；无换绑CD 为 2026-10-03 校准新增）"),
    ),
    "has_compensation": (
        _css("L1-class", "[class*='compensation'], [class*='baopei'], [class*='guarantee']",
             note="保障标签类名"),
        _re("L2-text", r"(找回包赔|永久包赔|包赔)", note="找回包赔标签"),
    ),
    "official_verified": (
        _css("L1-class", "[class*='verified'], [class*='official'], [class*='yanhao']",
             note="官方验号类名"),
        _re("L2-text", r"(官方验号|已验号|验号)", note="官方验号标签"),
    ),
    "img_cnt": (
        _css("L1-attr", "[pcimgcount]", attr="pcimgcount",
             note="smallCardTitle pcimgcount（PC 端图数）"),
        _css("L1-class", "[class*='img-count'], [class*='imgCount'], [class*='pic-count']",
             note="图片数类名"),
        _re("L2-text", r"(\d{1,3})\s*图", note="『9图』"),
    ),
    "featured_chars": (
        _css("L1-attr", "[important]", attr="important", max_len=None,
             note="smallCardTitle important 属性（特色角色，逗号分隔）"),
        Strategy(kind="css", name="L1-class",
                 selector="[class*='featured'], [class*='char-tag'], [class*='role-tag']",
                 all_matches=True, note="特色角色标签组（取全部命中节点文本）"),
        _re("L2-text", r"(?:特色角色|亮点|角色)\s*[:：]\s*([^\s|,，;；]{1,20}(?:\s*[|、,，;；]\s*[^\s|,，;；]{1,20})*)",
            note="『特色角色：钟离、胡桃』"),
        Strategy(kind="css", name="L3-class",
                 selector="[class*='role'], [class*='char'], [class*='tag']",
                 all_matches=True, note="宽松兜底（噪声风险，冒烟校准）"),
    ),
}

# 契约外的必要字段：listing_id（入库主键）、title（词表抽取输入）
EXTRA_SPECS: dict[str, tuple[Strategy, ...]] = {
    "favorites_cnt": (
        _css("L1-attr", "[collectcount]", attr="collectcount"),
        _re("L2-text", r"(\d{1,6})\s*人已收藏"),
    ),
    "listing_id": (
        _css("L1-attr", "[productid]", attr="productid", note="middleCard productid（校准）"),
        _css("L1-attr", "[data-listing-id]", attr="data-listing-id", note="语义属性"),
        _css("L1-attr", "[data-id]", attr="data-id", note="通用 data-id"),
        _css("L1-href", "a[href*='/product/']", attr="href", note="详情链接 href"),
        _re("L2-href", r"/product/(\d+)", note="从 /product/{listingId}/1 取 id"),
    ),
    "title": (
        _css("L1-attr", "[productname]", attr="productname", max_len=None,
             note="smallCardTitle productname（站点完整标题，校准）"),
        _css("L1-title", ".smallCardTitle", max_len=None,
             note="卡片标题全文；没有 productname 属性时不再裁成80字"),
        _css("L1-class", "[class*='title'], [class*='Title'], [class*='name'], h3, h4",
             note="标题类名"),
        _re("L2-text", r"([^\n]{6,80})", note="首行文本兜底"),
    ),
}

# 便于冒烟校准的集中出口
SELECTORS = {
    "cards": CARD_SELECTORS,
    "fields": FIELD_SPECS,
    "extra": EXTRA_SPECS,
}

_INT_RE = re.compile(r"\d+")
_FLOAT_CLEAN_RE = re.compile(r"[^0-9.]")
# 不可信 HTML 加固：解析前剥离 DOCTYPE/ENTITY 声明（不启用外部实体；html.parser 本身不解析 DTD）
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)
_ENTITY_DECL_RE = re.compile(r"<!ENTITY[^>]*>", re.IGNORECASE)
_XML_DECL_RE = re.compile(r"<\?xml[^>]*\?>", re.IGNORECASE)


def sanitize_dom_text(html: str) -> str:
    """规范化不可信 DOM 文本：去掉 DOCTYPE/ENTITY/XML 声明。

    raw 层 HTML 来自渲染页面（可能被篡改），或由离线回放自磁盘读入。这里显式剥离
    实体声明，保证解析器不使用任何外部实体（XXE 防护；本解析器基于 html.parser，
    本就不展开 DTD，属于纵深防御）。
    """
    if not html:
        return ""
    cleaned = _DOCTYPE_RE.sub("", html)
    cleaned = _ENTITY_DECL_RE.sub("", cleaned)
    cleaned = _XML_DECL_RE.sub("", cleaned)
    return cleaned


# 不可见子树特征（2026-10-03 真实 DOM 校准）：卡片内嵌隐藏优惠券弹层
# （t-dialog__ctx + display:none），文本含「￥ 0.00 满NaN可用」，会被价格正则误命中。
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_HIDDEN_CLASS_MARKERS: tuple[str, ...] = ("t-dialog__ctx", "t-popup__ctx")


def prune_hidden(soup: BeautifulSoup) -> BeautifulSoup:
    """剔除**不可见**子树：display:none 内联样式与弹层容器。

    用户看不见的内容也不应参与解析与风控文本判定（与 visible_text 的口径一致）。
    """
    for node in list(soup.find_all(attrs={"style": _HIDDEN_STYLE_RE})):
        node.decompose()
    for marker in _HIDDEN_CLASS_MARKERS:
        for node in list(soup.find_all(class_=marker)):
            node.decompose()
    return soup


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass
class FieldValue:
    value: Any
    strategy: str
    raw: str


@dataclass
class ParsedCard:
    listing_id: str | None
    title: str | None
    fields: dict[str, Any] = _dc_field(default_factory=dict)
    hits: dict[str, str] = _dc_field(default_factory=dict)       # 字段 → 命中策略名
    missing: list[str] = _dc_field(default_factory=list)         # 契约字段中未命中的
    missing_required: list[str] = _dc_field(default_factory=list)
    parse_ok: bool = False
    fail_reason: str | None = None
    parser_version: str = PARSER_VERSION

    def hit_count(self) -> int:
        return sum(1 for f in CONTRACT_FIELDS if self.fields.get(f) is not None)

    def as_snapshot_row(self) -> dict[str, Any]:
        """转 fct_listing_snapshot 行（列名与 pxb7.db.SNAPSHOT_COLUMNS 对齐）。"""
        row = {k: v for k, v in self.fields.items()}
        if isinstance(row.get("featured_chars"), (list, tuple)):
            row["featured_chars"] = json.dumps(list(row["featured_chars"]), ensure_ascii=False)
        row["listing_id"] = self.listing_id
        row["parser_version"] = self.parser_version
        return row


@dataclass
class PageParseResult:
    url: str | None
    cards_seen: int
    cards_parsed: int
    cards_failed: int
    cards: list[ParsedCard]
    field_hits: dict[str, int]
    field_missing: dict[str, int]
    card_selector_used: str | None
    parse_success_rate: float
    parser_version: str = PARSER_VERSION
    board_text_len: int = 0
    # 字段级诊断（校准用）：字段 → 前若干条样例 {value, strategy, listing_id}
    field_samples: dict[str, list[dict[str, Any]]] = _dc_field(default_factory=dict)
    cards_without_id: int = 0            # 无 listing_id（不可入库，已排除出 cards_parsed）
    cards_required_incomplete: int = 0   # 缺 required 字段的卡片数（进 QC 缺失率）

    def failed_cards(self) -> list[ParsedCard]:
        return [c for c in self.cards if not c.parse_ok]

    def field_diagnostics(self) -> list[dict[str, Any]]:
        """逐字段命中/缺失 + 样例值（冒烟校准选择器时直接看这张表）。"""
        total = self.cards_seen
        rows: list[dict[str, Any]] = []
        for name in CONTRACT_FIELDS:
            hits = self.field_hits.get(name, 0)
            rows.append({
                "field": name,
                "required": name in REQUIRED_FIELDS,
                "hits": hits,
                "missing": self.field_missing.get(name, 0),
                "hit_rate": round((hits / total) if total else 0.0, 4),
                "strategies": sorted({s.get("strategy") for s in self.field_samples.get(name, [])
                                      if s.get("strategy")}),
                "samples": self.field_samples.get(name, []),
            })
        return rows

    def failing_examples(self, limit: int = 3) -> list[dict[str, Any]]:
        """解析失败的卡片样例（含命中数），便于定位是缺价还是字段不足。"""
        out = []
        for card in self.failed_cards()[:limit]:
            out.append({"listing_id": card.listing_id, "reason": card.fail_reason,
                        "hit_count": card.hit_count(), "missing_required": card.missing_required,
                        "title": card.title})
        return out


# --------------------------------------------------------------------------- #
# 取值
# --------------------------------------------------------------------------- #
def _to_int(raw: str) -> int | None:
    m = _INT_RE.search(raw.replace(",", ""))
    return int(m.group(0)) if m else None


def _to_price(raw: str) -> float | None:
    cleaned = _FLOAT_CLEAN_RE.sub("", raw.replace(",", ""))
    if not cleaned or cleaned == ".":
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value >= 0 else None


def _to_str(raw: str) -> str | None:
    text = raw.strip()
    return text or None


def _to_bool(raw: str) -> bool | None:
    return True if raw.strip() else None


def _to_list(raw: str) -> list[str] | None:
    parts = re.split(r"[|、,，;；/\s]+", raw)
    items = [p.strip() for p in parts if p.strip()]
    return items or None


_CONVERTERS = {
    "price_yuan": _to_price,
    "level": _to_int,
    "yellow_cnt": _to_int,
    "five_star_chars": _to_int,
    "five_star_weapons": _to_int,
    "intertwined_fate": _to_int,
    "primogems": _to_int,
    "artifacts": _to_int,
    "skins": _to_int,
    "img_cnt": _to_int,
    "favorites_cnt": _to_int,
    "has_compensation": _to_bool,
    "official_verified": _to_bool,
    "featured_chars": _to_list,
}
_BOOL_FIELDS = ("has_compensation", "official_verified")


def _convert(field: str, raw: str) -> Any:
    converter = _CONVERTERS.get(field, _to_str)
    return converter(raw)


def _extract(card: Tag, field: str, strategies: Sequence[Strategy],
             card_text: str | None = None) -> FieldValue | None:
    """按策略顺序取值，返回首个成功命中。"""
    text = card_text if card_text is not None else card.get_text(" ", strip=True)
    for st in strategies:
        if st.kind == "css":
            nodes = _select(card, st.selector)
            if not nodes:
                continue
            if st.all_matches:
                pieces = []
                for node in nodes:
                    raw = node.get(st.attr) if st.attr else node.get_text(" ", strip=True)
                    if raw and raw.strip():
                        pieces.append(raw.strip())
                value = _to_list(" | ".join(dict.fromkeys(pieces))) if pieces else None
                if value:
                    return FieldValue(value, st.name, " | ".join(pieces))
                continue
            for node in nodes:
                raw = node.get(st.attr) if st.attr else node.get_text(" ", strip=True)
                if not raw:
                    continue
                raw = raw.strip()
                if st.attr is None and st.max_len is not None and len(raw) > st.max_len:
                    continue          # 命中的是外层容器（噪声），换下一个节点/策略
                value = _convert(field, raw)
                if value is not None:
                    if st.scale != 1.0 and isinstance(value, (int, float)):
                        value = round(value * st.scale, 2)
                    return FieldValue(value, st.name, raw)
        elif st.kind == "regex":
            if not st.pattern:
                continue
            m = re.search(st.pattern, text)
            if not m:
                continue
            try:
                raw = m.group(st.group)
            except IndexError:
                continue
            value = _convert(field, raw)
            if value is not None:
                return FieldValue(value, st.name, m.group(0).strip())
    return None


def _select(node: Tag, selector: str | None) -> list[Tag]:
    """在卡片内选择节点；卡片自身命中选择器时也计入（列表卡片常整卡就是一个 <a>）。"""
    if not selector:
        return []
    try:
        nodes = [n for n in node.select(selector) if isinstance(n, Tag)]
    except Exception:
        return []
    if nodes:
        return nodes
    if _ss_match is not None and getattr(node, "name", None):
        try:
            if _ss_match(selector, node):
                return [node]
        except Exception:
            return []
    return []


def _dedupe_nested(nodes: Iterable[Tag]) -> list[Tag]:
    """去掉被其它命中节点**包含**的节点，保留最外层（卡片容器）。

    命中集合里既有卡片容器也可能有容器内的价格/时间子节点：只要某个命中节点的祖先
    也在命中集合里，它就是内层节点，应丢弃。（按 id 比较，避免 bs4 结构化 __eq__ 误判。）
    """
    nodes = list(nodes)
    matched_ids = {id(node) for node in nodes}
    kept: list[Tag] = []
    for node in nodes:
        if any(id(parent) in matched_ids for parent in node.parents):
            continue
        kept.append(node)
    return kept


def visible_text(html: str, *, limit: int | None = None) -> str:
    """页面**可见文本**（剔除 script/style/noscript/template 与 DOCTYPE/ENTITY）。

    风控信号检测必须走这条口径：直接对整页 HTML 取文本会把内联脚本里的字样
    （组件库、埋点、说明文案）算进来，造成误报。
    """
    if not html:
        return ""
    soup = BeautifulSoup(sanitize_dom_text(html), "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    prune_hidden(soup)
    text = soup.get_text(" ", strip=True)
    return text[:limit] if limit else text


def _pick_cards(soup: BeautifulSoup) -> tuple[list[Tag], str | None]:
    for selector in CARD_SELECTORS:
        try:
            nodes = [n for n in soup.select(selector) if isinstance(n, Tag)]
        except Exception:
            continue
        nodes = _dedupe_nested(nodes)
        if nodes:
            return nodes, selector
    return [], None


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def parse_card(card: Tag | str, *, parser_version: str = PARSER_VERSION,
               full_title: str | None = None, full_title_source: str = "L1-tooltip") -> ParsedCard:
    """解析单张卡片（Tag 或 HTML 字符串）。任何字段失败都不抛异常。"""
    if isinstance(card, str):
        soup = BeautifulSoup(sanitize_dom_text(card), "html.parser")
        prune_hidden(soup)
        node = soup.body or soup
    else:
        node = card
    card_text = node.get_text(" ", strip=True)
    if full_title:
        card_text = full_title + " " + card_text
    fields: dict[str, Any] = {}
    hits: dict[str, str] = {}
    for name, strategies in FIELD_SPECS.items():
        hit = _extract(node, name, strategies, card_text)
        if hit is not None:
            fields[name] = hit.value
            hits[name] = hit.strategy
    extra: dict[str, Any] = {}
    for name, strategies in EXTRA_SPECS.items():
        hit = _extract(node, name, strategies, card_text)
        if hit is not None:
            extra[name] = hit.value
            hits[name] = hit.strategy
    if "favorites_cnt" in extra:
        fields["favorites_cnt"] = extra["favorites_cnt"]
    if full_title:
        extra["title"] = full_title
        hits["title"] = full_title_source
    if isinstance(extra.get("listing_id"), str):
        m = re.search(r"(\d{3,})", extra["listing_id"])
        extra["listing_id"] = m.group(1) if m else None

    missing = [f for f in CONTRACT_FIELDS if fields.get(f) is None]
    missing_required = [f for f in REQUIRED_FIELDS if fields.get(f) is None]
    other_ok = sum(1 for f in CONTRACT_FIELDS
                   if f != "price_yuan" and fields.get(f) is not None)
    # 成功解析（契约口径 = 价格 + ≥3 个其他字段）**且**卡片可用（有 listing_id，否则入不了库：
    # 把不可入库的卡片算作「解析成功」会同时抬高 cards_parsed 与 extract_hit_rate 分母）
    has_id = bool(extra.get("listing_id"))
    parse_ok = fields.get("price_yuan") is not None and other_ok >= 3 and has_id
    fail_reason = None
    if not parse_ok:
        if fields.get("price_yuan") is None:
            fail_reason = "missing-price"
        elif other_ok < 3:
            fail_reason = f"too-few-fields:{other_ok}"
        elif not has_id:
            fail_reason = "missing-listing-id"

    return ParsedCard(
        listing_id=extra.get("listing_id"),
        title=extra.get("title"),
        fields=fields,
        hits=hits,
        missing=missing,
        missing_required=missing_required,
        parse_ok=parse_ok,
        fail_reason=fail_reason,
        parser_version=parser_version,
    )


def parse_list_page(html: str, *, url: str | None = None,
                    parser_version: str = PARSER_VERSION) -> PageParseResult:
    """解析列表页 HTML：逐卡片解析 + 字段命中/缺失统计；单卡失败不中断。"""
    soup = BeautifulSoup(sanitize_dom_text(html or ""), "html.parser")
    # TDesign 将悬浮文案挂到卡片之外，关闭后 display:none，但已取得的公开全文仍在 DOM。
    # 只保留这一种标题节点；优惠券及其他隐藏内容仍按原规则剔除。
    popup_titles = [node.get_text("", strip=True) for node in
                    soup.select(".t-popup .longTitle > div:not(.more)")]
    prune_hidden(soup)
    nodes, selector = _pick_cards(soup)
    def normalized(text: str) -> str:
        return "".join(c for c in text if c.isalnum())
    prefixes = []
    for node in nodes:
        hit = _extract(node, "title", EXTRA_SPECS["title"])
        prefixes.append(normalized(hit.value) if hit else "")
    full_titles: dict[int, str] = {}
    full_sources: dict[int, str] = {}
    for title in popup_titles:
        normalized_title = normalized(title)
        matches = [index for index, prefix in enumerate(prefixes)
                   if len(prefix) >= 24 and normalized_title.startswith(prefix)]
        if len(matches) == 1:
            index = matches[0]
            previous = full_titles.get(index)
            if previous is None or normalized(previous) == normalized_title:
                full_titles[index] = title
            else:
                full_titles[index] = ""  # 同前缀但有冲突全文，拒绝猜测。
    # v0.4.4 将直接获取的公开全文写在上传 DOM 副本，不依赖悬浮节点。
    # ID 和原短标题双校验；价格/收藏/发布时间仍取原卡片，全文只补账号字段。
    for index, node in enumerate(nodes):
        id_hit = _extract(node, "listing_id", EXTRA_SPECS["listing_id"])
        candidates = ([node] if node.has_attr("data-pxb7-full-title") else []) + list(node.select("[data-pxb7-full-title]"))
        valid = set()
        for candidate in candidates:
            title = candidate.get("data-pxb7-full-title", "")
            if (id_hit and str(id_hit.value) == candidate.get("data-pxb7-full-title-id")
                    and isinstance(title, str) and 0 < len(title) <= 50000
                    and len(prefixes[index]) >= 24 and normalized(title).startswith(prefixes[index])):
                valid.add(title)
        if len(valid) == 1:
            full_titles[index] = valid.pop()
            full_sources[index] = "L1-title-api"
    cards: list[ParsedCard] = []
    field_hits = {f: 0 for f in CONTRACT_FIELDS}
    field_missing = {f: 0 for f in CONTRACT_FIELDS}
    field_samples: dict[str, list[dict[str, Any]]] = {f: [] for f in CONTRACT_FIELDS}

    for index, node in enumerate(nodes):
        try:
            parsed = parse_card(node, parser_version=parser_version, full_title=full_titles.get(index),
                                full_title_source=full_sources.get(index, "L1-tooltip"))
        except Exception as exc:                      # 单卡异常不中断整轮
            parsed = ParsedCard(listing_id=None, title=None, parse_ok=False,
                                fail_reason=f"card-exception:{type(exc).__name__}",
                                parser_version=parser_version)
            parsed.missing = list(CONTRACT_FIELDS)
            parsed.missing_required = list(REQUIRED_FIELDS)
        cards.append(parsed)
        for name in CONTRACT_FIELDS:
            value = parsed.fields.get(name)
            if value is None:
                field_missing[name] += 1
                continue
            field_hits[name] += 1
            bucket = field_samples[name]
            if len(bucket) < _SAMPLE_LIMIT:
                bucket.append({"value": value, "strategy": parsed.hits.get(name),
                               "listing_id": parsed.listing_id})

    seen = len(cards)
    parsed_ok = sum(1 for c in cards if c.parse_ok)
    without_id = sum(1 for c in cards if not c.listing_id)
    required_incomplete = sum(1 for c in cards if c.missing_required)
    return PageParseResult(
        url=url,
        cards_seen=seen,
        cards_parsed=parsed_ok,
        cards_failed=seen - parsed_ok,
        cards=cards,
        field_hits=field_hits,
        field_missing=field_missing,
        card_selector_used=selector,
        parse_success_rate=(parsed_ok / seen) if seen else 0.0,
        parser_version=parser_version,
        board_text_len=len(soup.get_text(" ", strip=True)),
        field_samples=field_samples,
        cards_without_id=without_id,
        cards_required_incomplete=required_incomplete,
    )


def describe_selectors() -> dict[str, Any]:
    """冒烟校准时打印用：当前卡片容器与每字段的策略名。"""
    return {
        "parser_version": PARSER_VERSION,
        "card_selectors": list(CARD_SELECTORS),
        "fields": {name: [st.name for st in specs] for name, specs in FIELD_SPECS.items()},
        "extra": {name: [st.name for st in specs] for name, specs in EXTRA_SPECS.items()},
    }


def parse_detail_attributes(html: str) -> tuple[str | None, dict[str, Any]]:
    """只解析当前商品，剔除推荐卡片；逐字 span 标题拼回完整文字。"""
    soup = BeautifulSoup(sanitize_dom_text(html), "html.parser")
    prune_hidden(soup)
    for node in soup.select("script, style, noscript, .smallCard, .middleCard, [productid]"):
        node.decompose()
    root = soup.select_one(".product-detail") or soup
    title_node = root.select_one("[data-product-title], .product-title, .line-clamp-5, h1")
    title = title_node.get_text("", strip=True) if title_node else None
    text = root.get_text(" ", strip=True)
    values: dict[str, Any] = {}
    for name in ("level", "yellow_cnt", "five_star_chars", "five_star_weapons"):
        strategies = tuple(st for st in FIELD_SPECS[name] if st.kind == "regex")
        if name == "level":
            # 详情有角色/武器 Lv.90，不能当作账号等级。
            strategies = (_re("detail-account-level", r"(?:冒险等级|联觉等级|账号等级|等级)\s*[:：]?\s*(\d{1,3})(?!\d)"),)
        hit = _extract(root, name, strategies, title or "")
        if hit is None and title:
            hit = _extract(root, name, tuple(st for st in FIELD_SPECS[name]
                                           if st.kind == "regex" and st.name == "L3-text"), title)
        if hit is None:
            hit = _extract(root, name, strategies, text)
        if hit is not None:
            values[name] = hit.value
    return title, values
