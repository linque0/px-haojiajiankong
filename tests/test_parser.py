"""解析器夹具测试：用手工构造的假 HTML 验证（不访问真实站点）。

真机样本在冒烟步骤到来后替换/补充为 tests/fixtures/ 下的真实 DOM。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import parser as P  # noqa: E402

# 卡片 1：语义属性齐全（L1 命中）
CARD1 = """
<li class="product-card" data-listing-id="1000000001">
  <a href="/product/1000000001/1" class="card-link">
    <div class="card-title">原神 官服 满命钟离 胡桃 活邮</div>
    <div class="card-price">¥1,280</div>
    <div class="card-time">21分钟内发布</div>
    <ul class="card-attrs">
      <li class="attr-level">等级 60</li>
      <li class="attr-yellow">黄数 45</li>
      <li class="attr-star-char">五星角色 12</li>
      <li class="attr-star-weapon">五星武器 5</li>
      <li class="attr-primogems">原石 32000</li>
      <li class="attr-fate">纠缠之源 120</li>
      <li class="attr-artifact">圣遗物 300</li>
      <li class="attr-skin">时装 8</li>
      <li class="attr-server">官服</li>
      <li class="attr-mail">邮箱出售</li>
      <li class="attr-tap">TAP未绑定</li>
      <li class="attr-psn">PSN未绑定</li>
      <li class="attr-trade-code">提供换绑码</li>
      <li class="attr-baopei">找回包赔</li>
      <li class="attr-verified">官方验号</li>
      <li class="attr-gender">男主</li>
    </ul>
    <div class="card-img-count">9图</div>
    <div class="featured-chars"><span>钟离</span><span>胡桃</span></div>
  </a>
</li>
"""

# 卡片 2：无逐字段类名，只能靠 L2 文本模式（回退层验证）
CARD2 = """
<li class="product-card" data-listing-id="1000000002">
  <a href="/product/1000000002/1">
    <p>崩坏：星穹铁道 官服 冒险等级 70 黄数60 五星角色 20 五星武器 9 原石 50000
       纠缠之源 88 圣遗物 400 时装 3 邮箱实名 TAP已绑定 PSN已绑定 无换绑码 找回包赔</p>
    <span>¥ 8,888</span>
    <span>2小时前发布</span>
    <span>女主</span>
    <span>特色角色：流萤、镜流</span>
  </a>
</li>
"""

# 卡片 3：坏卡片（无价格）——只计数、不中断
CARD3 = """
<li class="product-card" data-listing-id="1000000003">
  <div class="card-title">这是解析不了的卡片</div>
</li>
"""

FAKE_LIST_HTML = f"<html><body><ul class='product-list'>{CARD1}{CARD2}{CARD3}</ul></body></html>"


# --------------------------------------------------------------------------- #
# 列表页解析
# --------------------------------------------------------------------------- #
def test_parse_list_page_counts_and_failures() -> None:
    res = P.parse_list_page(FAKE_LIST_HTML, url="https://www.pxb7.com/buy/10026/1")
    assert res.cards_seen == 3
    assert res.cards_parsed == 2, "坏卡片只计数、不中断整轮"
    assert res.cards_failed == 1
    assert res.parse_success_rate == pytest.approx(2 / 3)
    assert res.card_selector_used in P.CARD_SELECTORS
    assert res.parser_version == P.PARSER_VERSION


def test_card1_semantic_attributes() -> None:
    res = P.parse_list_page(FAKE_LIST_HTML)
    card = res.cards[0]
    f = card.fields
    assert card.parse_ok is True
    assert card.listing_id == "1000000001"
    assert f["price_yuan"] == pytest.approx(1280.0), "挂牌价（非成交价），¥ 与千分位需清理"
    assert f["publish_time_text"] == "21分钟内发布"
    assert f["level"] == 60
    assert f["yellow_cnt"] == 45
    assert f["five_star_chars"] == 12
    assert f["five_star_weapons"] == 5
    assert f["primogems"] == 32000
    assert f["intertwined_fate"] == 120
    assert f["artifacts"] == 300
    assert f["skins"] == 8
    assert f["server"] == "官服"
    assert f["mail_status"] == "邮箱出售"
    assert f["has_compensation"] is True
    assert f["official_verified"] is True
    assert f["img_cnt"] == 9
    assert f["mc_gender"] == "男主"
    assert "钟离" in f["featured_chars"] and "胡桃" in f["featured_chars"]
    assert card.hits["price_yuan"].startswith("L1")
    assert card.missing == [], f"契约 20 字段应全命中，实际缺失 {card.missing}"


def test_card1_snapshot_row_matches_db_columns() -> None:
    from pxb7 import db
    res = P.parse_list_page(FAKE_LIST_HTML)
    row = res.cards[0].as_snapshot_row()
    assert row["listing_id"] == "1000000001"
    assert row["parser_version"] == P.PARSER_VERSION
    import json
    assert json.loads(row["featured_chars"]) == ["钟离", "胡桃"]
    unknown = set(row) - set(db.SNAPSHOT_COLUMNS)
    assert not unknown, f"parser 输出列不在快照表列内：{unknown}"


def test_card2_text_fallback() -> None:
    res = P.parse_list_page(FAKE_LIST_HTML)
    card = res.cards[1]
    f = card.fields
    assert card.parse_ok is True
    assert f["price_yuan"] == pytest.approx(8888.0)
    assert f["level"] == 70
    assert f["yellow_cnt"] == 60
    assert f["five_star_chars"] == 20
    assert f["five_star_weapons"] == 9
    assert f["primogems"] == 50000
    assert f["intertwined_fate"] == 88
    assert f["mail_status"] == "邮箱实名"
    assert f["has_compensation"] is True
    assert "2小时前发布" == f["publish_time_text"]
    assert set(f["featured_chars"]) == {"流萤", "镜流"}
    # 无逐字段类名 → 价格应来自 L2 文本模式
    assert card.hits["level"].startswith("L2")
    unknown = [k for k, v in card.hits.items() if v.startswith("L1") and k in
               ("level", "yellow_cnt", "five_star_chars", "primogems")]
    assert unknown == [], f"这些字段本应走文本回退：{unknown}"


def test_card3_failure_is_reported_not_raised() -> None:
    res = P.parse_list_page(FAKE_LIST_HTML)
    bad = res.cards[2]
    assert bad.parse_ok is False
    assert bad.fail_reason and "missing-price" in bad.fail_reason
    assert "price_yuan" in bad.missing_required
    assert res.field_missing["price_yuan"] == 1
    assert res.field_hits["price_yuan"] == 2


def test_field_hit_and_miss_stats_cover_contract() -> None:
    res = P.parse_list_page(FAKE_LIST_HTML)
    assert set(res.field_hits) == set(P.CONTRACT_FIELDS)
    assert set(res.field_missing) == set(P.CONTRACT_FIELDS)
    for name in P.CONTRACT_FIELDS:
        assert res.field_hits[name] + res.field_missing[name] == 3


# --------------------------------------------------------------------------- #
# 结构变体与健壮性
# --------------------------------------------------------------------------- #
def test_card_is_anchor_with_href_only() -> None:
    """整卡就是 <a>（无 data-* 属性）时，listing_id 仍应从 href 取到。"""
    html = """
    <div class="list">
      <a href="/product/154643349307491/1"><span class="price">¥ 520</span>
        <span>官服</span><span>等级 50</span><span>黄数 20</span><span>五星角色 6</span>
        <span>五星武器 2</span><span>邮箱出售</span></a>
    </div>"""
    res = P.parse_list_page(html)
    assert res.cards_seen == 1
    assert res.cards[0].listing_id == "154643349307491"
    assert res.cards[0].fields["price_yuan"] == pytest.approx(520.0)


@pytest.mark.parametrize("html", ["", "   ", "<html></html>", "<div>没有卡片</div>", "not html at all"])
def test_parse_empty_or_garbage_html_does_not_crash(html: str) -> None:
    res = P.parse_list_page(html)
    assert res.cards_seen == 0
    assert res.cards_parsed == 0
    assert res.parse_success_rate == 0.0
    assert res.card_selector_used is None


def test_parse_card_accepts_raw_string() -> None:
    card = P.parse_card(CARD1)
    assert card.parse_ok is True
    assert card.fields["price_yuan"] == pytest.approx(1280.0)
    assert card.listing_id == "1000000001"


def test_describe_selectors_lists_two_levels_per_field() -> None:
    info = P.describe_selectors()
    assert info["parser_version"] == P.PARSER_VERSION
    assert len(info["card_selectors"]) >= 2, "卡片容器需 ≥2 级候选"
    for name in P.CONTRACT_FIELDS:
        assert len(info["fields"][name]) >= 2, f"{name} 需要至少两级回退策略"


def test_price_is_never_fabricated() -> None:
    """缺失字段一律 NULL：不得用 0 或猜测值冒充。"""
    res = P.parse_list_page(FAKE_LIST_HTML)
    bad = res.cards[2]
    assert bad.fields.get("price_yuan") is None
    assert bad.parse_ok is False


# --------------------------------------------------------------------------- #
# 真实 DOM 校准回归（2026-10-03，内置浏览器抓取 pxb7.com/buy/10026/1）
# --------------------------------------------------------------------------- #
REAL_CARD_FIXTURE = Path(__file__).parent / "fixtures" / "real_card_pxb7_20261003.html"


def test_real_dom_calibration_v0_2_0() -> None:
    """【校准回归】v0.2.0 选择器对真实卡片结构必须全部生效。

    关键点：price 属性单位为**分**（÷100）；隐藏优惠券弹层（￥0.00 满NaN可用）
    在 v0.1.0 曾把 16/16 张卡的价格全部解析成 0.00——隐藏子树必须被剔除。
    """
    html = REAL_CARD_FIXTURE.read_text(encoding="utf-8")
    page = P.parse_list_page(html)
    assert page.cards_seen == 2 and page.cards_parsed == 2
    c1 = next(c for c in page.cards if c.listing_id == "2428022628844555568")
    assert c1.fields["price_yuan"] == pytest.approx(300.0), \
        f"price 属性（分）必须 ÷100，实际 {c1.fields.get('price_yuan')}"
    assert c1.hits["price_yuan"] == "L1-attr", "语义属性层必须优先于文本正则"
    assert c1.fields["publish_time_text"] == "2026-10-03 00:19:01", "createtime 精确发布时间"
    assert c1.fields["img_cnt"] == 32
    assert c1.fields["skins"] == 2, "『时装数量2』文案必须可解析"
    assert c1.fields["mail_status"] == "邮箱未绑定", "真实邮箱状态枚举"
    assert c1.fields["tap_status"] == "未绑定TAP", "必须捕获完整状态短语而非裸词 TAP"
    assert c1.fields["psn_status"] == "未绑定PSN"
    assert c1.fields["trade_code_status"] == "提供换绑码"
    assert c1.fields.get("featured_chars") is None, "important 为空 ⇒ 特色角色 NULL（不猜）"
    assert c1.title is not None and "58级" in c1.title, "title 取 productname 属性"
    c2 = next(c for c in page.cards if c.listing_id == "2427746247312293031")
    assert c2.fields["featured_chars"] == ["伊涅芙", "菈乌玛"], "important 属性=特色角色"
    assert c2.fields["price_yuan"] == pytest.approx(850.0)


def test_hidden_dialog_text_is_not_parsed() -> None:
    """【回归】display:none / t-dialog 弹层文本不得参与解析（￥0.00 噪声来源）。"""
    html = ('<div class="card-price">¥ 120</div>'
            '<div class="t-dialog__ctx" style="display: none;">￥ 0.00 满NaN可用</div>')
    card = P.parse_card(f'<li class="product-card" data-listing-id="9">{html}</li>')
    assert card.fields["price_yuan"] == pytest.approx(120.0)
    # 可见文本口径同样剔除隐藏层（风控信号检测不误报）
    text = P.visible_text('<div>正常文案</div><div style="display:none">￥ 0.00 满NaN可用</div>')
    assert "0.00" not in text and "正常文案" in text
