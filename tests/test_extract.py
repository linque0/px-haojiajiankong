"""词表种子与抽取器测试（离线，不访问站点）。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import extract as X  # noqa: E402

SEED_PATH = PROJECT_ROOT / "config" / "keywords_seed.yaml"


@pytest.fixture(scope="module")
def keywords() -> list[X.Keyword]:
    """原神画像（本文件既有断言全部针对 docs/02 §4.A1 原神词表 v0）。"""
    return X.seed_keywords(profile_id="genshin_10026")


@pytest.fixture(scope="module")
def wuwa_keywords() -> list[X.Keyword]:
    """鸣潮画像 wuwa_v0（2026-10-03 站内实样 MVNGK0804 校准，docs/02 §4.A4）。"""
    return X.seed_keywords(profile_id="wuwa_10302")


@pytest.fixture(scope="module")
def all_keywords() -> list[X.Keyword]:
    """全库（原神 18 + 鸣潮 25，2026-10-03 起同一份种子两个画像）。"""
    return X.seed_keywords()


# --------------------------------------------------------------------------- #
# 种子保真（docs/02 §4.A0/A1）
# --------------------------------------------------------------------------- #
def test_seed_structure_and_metadata() -> None:
    data = X.load_seed()
    assert data["version"]
    assert "§4.A" in data["generated_from"]
    profiles = data["profiles"]
    assert len(profiles) == 3, "原神 10026 + 鸣潮 10302 + 三角洲 10371（docs/02 §4.A1 + §4.A4 + §4.B）"
    profile = profiles[0]
    assert profile["profile_id"] == "genshin_10026"
    assert profile["game_id"] == 10026
    assert profile["keyword_profile"] == "genshin_v0"
    wuwa = profiles[1]
    assert wuwa["profile_id"] == "wuwa_10302"
    assert wuwa["game_id"] == 10302, "db.py:92 dim_game 已注册 10302 → wuwa_v0"
    assert wuwa["game_name"] == "鸣潮"
    assert wuwa["keyword_profile"] == "wuwa_v0"
    assert "实样" in wuwa["source"], "鸣潮画像 source 须注明站内实样校准"


def test_seed_covers_documented_type_distribution(keywords: list[X.Keyword],
                                                   wuwa_keywords: list[X.Keyword]) -> None:
    """按画像分别断言：原神（docs/02 §4.A1）ceiling 7 / resource 4 / risk 1 + §4.A0 共享 floor 6；
    鸣潮（docs/02 §4.A4）ceiling 7 / segment 10 / resource 5 / floor 3。"""
    def counts(kws: list[X.Keyword]) -> dict[str, int]:
        out: dict[str, int] = {}
        for kw in kws:
            out[kw.keyword_type] = out.get(kw.keyword_type, 0) + 1
        return out

    genshin = counts(keywords)
    assert genshin.get("ceiling") == 7
    assert genshin.get("resource") == 4
    assert genshin.get("risk") == 1
    assert genshin.get("floor") == 6
    assert sum(genshin.values()) == len(keywords) == 18
    assert set(genshin) <= set(X.KEYWORD_TYPES)

    wuwa = counts(wuwa_keywords)
    assert wuwa.get("ceiling") == 7
    assert wuwa.get("segment") == 10
    assert wuwa.get("resource") == 5
    assert wuwa.get("floor") == 3
    assert sum(wuwa.values()) == len(wuwa_keywords) == 25, "wuwa_v0 共 25 条"
    assert set(wuwa) <= set(X.KEYWORD_TYPES)


def test_seed_fields_valid(keywords: list[X.Keyword],
                           wuwa_keywords: list[X.Keyword]) -> None:
    for kw in keywords:
        assert kw.profile_id == "genshin_10026"
        assert kw.keyword_type in X.KEYWORD_TYPES
        assert kw.price_anchor in X.PRICE_ANCHORS
        assert kw.weight_v1 is None and kw.weight_v2 is None, "v1/v2 权重上线后才回填"
        if kw.enabled:
            assert kw.extract_pattern, f"{kw.keyword_id} 启用时必须给出抽取正则"
            re.compile(kw.extract_pattern)          # 正则必须可编译
    anchors = {kw.keyword for kw in keywords if kw.price_anchor == "ceiling"}
    assert {"满命", "专武", "精五", "全图鉴", "双爆毕业", "深渊满星"} <= anchors

    for kw in wuwa_keywords:
        assert kw.profile_id == "wuwa_10302"
        assert kw.keyword_id.startswith("wuwa_"), "keyword_id 前缀隔离，避免与原神串号"
        assert kw.keyword_type in X.KEYWORD_TYPES
        assert kw.price_anchor in X.PRICE_ANCHORS
        assert kw.weight_v1 is None and kw.weight_v2 is None, "v1/v2 权重上线后才回填"
        if kw.enabled:
            assert kw.extract_pattern, f"{kw.keyword_id} 启用时必须给出抽取正则"
            re.compile(kw.extract_pattern)          # 正则必须可编译
    wuwa_anchors = {kw.keyword for kw in wuwa_keywords if kw.price_anchor == "ceiling"}
    assert {"满链", "专武", "高谐振", "卡提希娅", "弗洛洛", "声骸毕业"} <= wuwa_anchors
    # 样文最关键的一条口径：四星满命 ≠ 天花板（12 个四星中 11 个标满命）
    manming = next(kw for kw in wuwa_keywords if kw.keyword_id == "wuwa_seg_manming")
    assert manming.keyword_type == "segment" and manming.price_anchor == "none"
    nming = next(kw for kw in wuwa_keywords if kw.keyword_id == "wuwa_seg_nming")
    assert nming.keyword_type == "segment" and nming.price_anchor == "none"
    jingn = next(kw for kw in wuwa_keywords if kw.keyword_id == "wuwa_seg_jingn")
    assert jingn.price_anchor == "none", "精1–4 不是溢价项（样文 15 条全为精1）"


def test_genshin_profile_rows_unchanged(keywords: list[X.Keyword]) -> None:
    """2026-10-03 新增鸣潮画像时，原神 18 条 keyword_id 逐条不变。"""
    assert {kw.keyword_id for kw in keywords} == {
        "ys_ceiling_manming", "ys_ceiling_zhuanwu", "ys_ceiling_jingwu", "ys_ceiling_quantujian",
        "ys_ceiling_up_role", "ys_ceiling_shuangbao", "ys_ceiling_abyss",
        "ys_resource_primogem", "ys_resource_fate", "ys_resource_resin", "ys_resource_fuel",
        "ys_floor_initial", "ys_floor_selfdraw", "ys_floor_empty", "ys_floor_deadmail",
        "ys_floor_unverified", "ys_floor_channel_server", "ys_risk_intl_server",
    }


def test_up_role_placeholder_is_disabled(keywords: list[X.Keyword]) -> None:
    """『当期/复刻人气 UP 角色』文档未给名单 → 占位行且不参与抽取。"""
    row = next(kw for kw in keywords if kw.keyword_id == "ys_ceiling_up_role")
    assert row.enabled is False
    assert row.source == "template"


def test_floor_words_match_docs_a0(keywords: list[X.Keyword]) -> None:
    floor_words = {kw.keyword for kw in keywords if kw.keyword_type == "floor"}
    assert floor_words == {"初始号", "自抽号", "空号", "死邮", "未实名", "渠道服"}
    # §4.A0 明确「渠道服/国际服（强制分域）」：国际服在 A1 归类为 risk
    risk_words = {kw.keyword for kw in keywords if kw.keyword_type == "risk"}
    assert risk_words == {"国际服"}


def test_no_credentials_in_seed() -> None:
    text = SEED_PATH.read_text(encoding="utf-8").lower()
    for bad in ("token", "cookie", "password", "secret", "authorization", "webhook", "storage_state"):
        assert bad not in text, f"词表种子不得包含凭据/登录态相关内容：{bad}"


def test_seed_db_rows_shape() -> None:
    rows = X.seed_db_rows()
    assert len(rows) == 75, "原神 18 条 + 鸣潮 25 条 + 三角洲 32 条（docs/02 §4.A1 + §4.A4 + §4.B）"
    from pxb7 import db
    for row in rows:
        assert set(row) == set(db.KEYWORD_COLUMNS)
        assert row["enabled"] in (True, False)
    per_profile: dict[str, int] = {}
    for row in rows:
        per_profile[row["profile_id"]] = per_profile.get(row["profile_id"], 0) + 1
    assert per_profile == {"genshin_10026": 18, "wuwa_10302": 25, "delta_10371": 32}
    assert len({row["keyword_id"] for row in rows}) == len(rows), "keyword_id 不得重复"


def test_seed_profiles_scoped_by_game_id() -> None:
    """按 game_id 取词表：各游戏只拿自己的画像（docs/02 §4.B、§G）。"""
    genshin = X.seed_keywords(game_id=10026)
    wuwa = X.seed_keywords(game_id=10302)
    delta = X.seed_keywords(game_id=10371)
    assert {kw.profile_id for kw in genshin} == {"genshin_10026"} and len(genshin) == 18
    assert {kw.profile_id for kw in wuwa} == {"wuwa_10302"} and len(wuwa) == 25
    assert {kw.profile_id for kw in delta} == {"delta_10371"} and len(delta) == 32
    assert X.seed_keywords(game_id=99999) == [], "未建画像的游戏须返回空表、不用别家顶替"


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #
def test_extract_text_hits_and_features(keywords: list[X.Keyword]) -> None:
    title = "原神 官服 满命钟离 精5专武 双爆毕业 深渊满星 原石 32000 纠缠之源 120"
    got = X.extract_listing(keywords, listing_id="1000000001", title=title, card_fields={})
    assert got.hit is True
    assert got.features["constellation_cnt"] == 6, "§4.A0：满命 = 6 命"
    assert got.features["five_star_weapon_refined"] == 5, "满精/精5 → 5"
    assert got.features["signature_weapon_flag"] is True
    assert got.features["abyss_full_star_flag"] is True
    assert got.features["primogems_cnt"] == 32000
    assert got.features["intertwined_fate_cnt"] == 120
    ids = {h.keyword_id for h in got.hits}
    assert {"ys_ceiling_manming", "ys_ceiling_jingwu", "ys_resource_primogem"} <= ids
    import json
    payload = json.loads(got.features_json())
    assert payload["constellation_cnt"] == 6


def test_extract_numeric_constellation(keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(keywords, listing_id="L1", title="原神 6命胡桃 官服")
    assert got.features["constellation_cnt"] == 6
    assert any(h.keyword_id == "ys_ceiling_manming" for h in got.hits)
    assert got.hits[0].hit_text == "6命", "hit_text 必须保留命中原文"


def test_extract_card_field_channel(keywords: list[X.Keyword]) -> None:
    """[卡/筛] 词条（原石/纠缠之源）在标题无词时按卡片字段取值，来源如实标注。"""
    got = X.extract_listing(keywords, listing_id="L2", title="原神 官服 冒险等级60",
                            card_fields={"primogems": 8000, "intertwined_fate": 30})
    assert got.features["primogems_cnt"] == 8000
    assert got.features["intertwined_fate_cnt"] == 30
    hit = next(h for h in got.hits if h.keyword_id == "ys_resource_primogem")
    assert hit.via == "card_field"
    assert "卡片字段 primogems" in hit.hit_text, "不得伪造原文，须标注来源字段"


def test_extract_floor_and_risk_words(keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(keywords, listing_id="L3",
                            title="原神 官服 初始号 未实名 死邮 渠道服 国际服")
    types = {h.keyword_type for h in got.hits}
    assert "floor" in types and "risk" in types
    assert got.features["floor_dead_mail_flag"] is True
    assert got.features["server_domain_risk"] is True


def test_extract_no_hit_is_not_fabricated(keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(keywords, listing_id="L4", title="原神 官服 冒险等级60",
                            card_fields={})
    assert got.hit is False
    assert got.features == {}
    assert got.features_json() is None, "无特征时必须写 NULL，不得用 0/空对象冒充"


def test_extract_does_not_match_disabled_row(keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(keywords, listing_id="L5", title="原神 当期UP角色 全图鉴")
    assert "up_character_flag" not in got.features


def test_hit_rate_contract_definition() -> None:
    """【口径修正】契约 extract_hit_rate 只统计**词表文本命中**（终审发现项 #13/#38）：
    [卡] 字段通道（原石/纠缠直取）写入特征与桥表，但不得稀释命中率。"""
    text_hit = X.Extraction(listing_id="A", features={"x_flag": True},
                            hits=[X.KeywordHit("A", "k1", "满命", "ceiling", "f", "满命",
                                               value=6, via="text")])
    card_field_only = X.Extraction(
        listing_id="B", features={"primogems_cnt": 8000},
        hits=[X.KeywordHit("B", "k2", "原石", "resource", "f", "原石=卡片字段 primogems 值 8000",
                           value=8000, via="card_field")])
    feature_only = X.Extraction(listing_id="C", features={"y_cnt": 1})
    empty = X.Extraction(listing_id="D")
    stats = X.summarize([text_hit, card_field_only, feature_only, empty])
    assert stats.listings == 4
    assert stats.hit_listings == 3, "任一通道命中 = 有 hits 或有 features（A/B/C）"
    assert stats.text_hit_listings == 1, "契约口径分子：仅文本命中"
    assert stats.extract_hit_rate == pytest.approx(1 / 4)
    assert stats.card_field_listings == 1 and stats.card_field_coverage == pytest.approx(1 / 4)
    assert stats.any_hit_rate == pytest.approx(3 / 4)
    assert X.extra_hit_rate(2, 0) == 0.0, "分母 0 记 0"
    assert X.extra_hit_rate(stats.text_hit_listings, 4) == pytest.approx(1 / 4)


def test_compile_warnings_for_enabled_without_pattern() -> None:
    kw = X.Keyword(keyword_id="k", profile_id="p", keyword="w", keyword_type="segment",
                   extract_pattern=None, enabled=True)
    compiled, warnings = X.compile_keywords([kw])
    assert compiled == [] and warnings


# --------------------------------------------------------------------------- #
# 鸣潮画像 wuwa_v0（2026-10-03 站内实样 MVNGK0804 校准，docs/02 §4.A4）
# --------------------------------------------------------------------------- #
# 样文片段（摘自 MVNGK0804 的商品文字，按段拼合；段间「；」、段内「，」的段式结构见 §4.A4）
WUWA_SAMPLE = (
    "80级，40黄，星声：942，月相：3，余波珊瑚：62，浮金波纹：11，铸潮波纹：1；"
    "19个五星角色：3命弗洛洛, 2命安可, 1命鉴心；"
    "12个四星角色：满命散华, 满命丹瑾, 2命卜灵；"
    "15个五星武器：精1星序协响, 精1千古洑流；"
    "车架模组：云帛机骑；摩托饰品：绯雪团子"
)


def test_wuwa_sample_fragment_features(wuwa_keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(wuwa_keywords, listing_id="W1", title=WUWA_SAMPLE)
    assert got.hit is True
    # 结构计数（§4.A4 样文实测要点）
    assert got.features["account_level_cnt"] == 80
    assert got.features["yellow_cnt"] == 40
    assert got.features["five_star_chars_cnt"] == 19
    assert got.features["four_star_chars_cnt"] == 12
    assert got.features["five_star_weapons_cnt"] == 15
    assert got.features["constellation_cnt"] == 3, "N命 行在 满命 行之前 → 取五星段「3命弗洛洛」"
    assert got.features["five_star_weapon_refined"] == 1, "样文 15 条五星武器全为精1"
    # 五类货币（样文「名：数」全角冒号）
    assert got.features["astrite_cnt"] == 942
    assert got.features["lunite_cnt"] == 3
    assert got.features["afterglow_coral_cnt"] == 62
    assert got.features["lustrous_tide_cnt"] == 11
    assert got.features["radiant_tide_cnt"] == 1
    # 样文新增结构槽位（文档零覆盖，v0 只记命中不进锚点层）
    assert got.features["vehicle_frame_module_flag"] is True
    assert got.features["motorcycle_ornament_flag"] is True
    ids = {h.keyword_id for h in got.hits}
    assert {"wuwa_seg_nming", "wuwa_seg_jingn", "wuwa_resource_xingsheng",
            "wuwa_seg_vehicle_frame", "wuwa_seg_moto_ornament"} <= ids
    assert "wuwa_ceiling_t0_fuluoluo" in ids, "T0 梯队角色按名命中（docs/02 §4.A4）"


def test_wuwa_manming_stays_out_of_ceiling_domain(wuwa_keywords: list[X.Keyword]) -> None:
    """样文最关键发现：12 个四星中 11 个标「满命」——四星满命不得进锚点层（§4.A4/§6）。"""
    got = X.extract_listing(wuwa_keywords, listing_id="W2",
                            title="鸣潮 官服 12个四星角色：满命散华, 满命丹瑾")
    assert "constellation_cnt" not in got.features, "四星满命不能当作五星共鸣链"
    ceiling_ids = {kw.keyword_id for kw in wuwa_keywords if kw.price_anchor == "ceiling"}
    assert {h.keyword_id for h in got.hits} & ceiling_ids == set(), "满命/N命 均非 ceiling 锚点"


def test_wuwa_ceiling_anchors_and_refine(wuwa_keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(wuwa_keywords, listing_id="W3",
                            title="鸣潮 官服 满链卡提希娅 专武 精5千古洑流 高谐振 声骸毕业")
    assert got.features["constellation_cnt"] == 6, "满链 = 6 命（文档支撑词，样文未出现）"
    assert got.features["five_star_weapon_refined"] == 5, "满精/精5 → 5（§4.A0 同构先验）"
    assert got.features["signature_weapon_flag"] is True
    assert got.features["echo_graduation_flag"] is True
    ceiling_ids = {kw.keyword_id for kw in wuwa_keywords if kw.price_anchor == "ceiling"}
    hit_ids = {h.keyword_id for h in got.hits}
    assert {"wuwa_ceiling_manlian", "wuwa_ceiling_zhuanwu", "wuwa_ceiling_gaoxiezhen",
            "wuwa_ceiling_shenghai", "wuwa_ceiling_t0_kateixiya"} <= (hit_ids & ceiling_ids)


def test_wuwa_floor_words(wuwa_keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(wuwa_keywords, listing_id="W4",
                            title="鸣潮 官服 初始号 自抽号 死邮 邮箱不出售 渠道服")
    assert got.features["floor_initial_account_flag"] is True
    assert got.features["floor_dead_mail_flag"] is True
    assert got.features["floor_channel_server_flag"] is True


def test_wuwa_no_hit_is_not_fabricated(wuwa_keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(wuwa_keywords, listing_id="W5", title="鸣潮 官服 出号")
    assert got.hit is False
    assert got.features == {} and got.features_json() is None


# --------------------------------------------------------------------------- #
# 三角洲行动 delta_v0（docs/02 §4.B 2026-10-03 站内实样校准）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def delta_keywords() -> list[X.Keyword]:
    """三角洲画像 delta_v0（详情页全文实样 + 4 份列表 raw 真实标题）。"""
    return X.seed_keywords(profile_id="delta_10371")


def _hits_map(got: X.Extraction) -> dict[str, str]:
    return {h.keyword: h.hit_text for h in got.hits}


def test_delta_seed_profile_shape() -> None:
    delta = X.load_seed()["profiles"][2]
    assert delta["profile_id"] == "delta_10371" and delta["game_id"] == 10371
    assert delta["game_name"] == "三角洲行动" and delta["keyword_profile"] == "delta_v0"
    assert "实样" in delta["source"]
    keywords = X.seed_keywords(profile_id="delta_10371")
    assert len(keywords) == 32
    assert {k.keyword_type for k in keywords} <= set(X.KEYWORD_TYPES)
    assert all(k.price_anchor in X.PRICE_ANCHORS for k in keywords)
    assert {k.keyword for k in keywords if k.price_anchor == "ceiling"} >= {
        "红皮", "典藏枪皮", "进阶安全箱", "曼德尔砖"}


def test_delta_asset_units_are_not_misconverted(delta_keywords: list[X.Keyword]) -> None:
    """W/M 单位不做数值换算：hit_text 保留原值，且资产类不得写成 _cnt（会把 57.4M 读成 57）。"""
    got = X.extract_listing(delta_keywords, listing_id="D1",
                            title="总资产：57.4M，哈夫币：100W，流动资产35.9M，不动资产21.4M")
    assert not any(k.startswith("delta_") and k.endswith("_cnt") for k in got.features), \
        f"资产类不得进 _cnt（单位未换算）：{got.features}"
    hits = _hits_map(got)
    assert hits["总资产"] == "57.4M" and hits["哈夫币"] == "100W"
    assert hits["流动资产"] == "35.9M" and hits["不动资产"] == "21.4M"


def test_delta_real_list_title_extraction(delta_keywords: list[X.Keyword]) -> None:
    """列表标题实测（4 份 raw dump）：「红皮2/刀皮4/传说武器52/史诗武器75/烽火60级：铂金」。"""
    title = ("31图 找回包赔 官方验号 总资产：136.8M，哈夫币：2511W，红皮2，刀皮4，传说武器52，"
             "史诗武器75，烽火60级：铂金，战场50级：上等兵，通行证1")
    got = X.extract_listing(delta_keywords, listing_id="D2", title=title)
    feat = got.features
    assert feat["delta_legendary_weapon_cnt"] == 52 and feat["delta_epic_weapon_cnt"] == 75
    assert feat["delta_knife_skin_cnt"] == 4 and feat["delta_red_skin_cnt"] == 2
    assert feat["delta_fenghuo_level_cnt"] == 60 and feat["delta_battlefield_level_cnt"] == 50
    hits = _hits_map(got)
    assert hits["烽火段位"] == "铂金" and hits["战场段位"] == "上等兵", "段位不枚举、原样进 hit_text"
    assert hits["总资产"] == "136.8M" and "找回包赔" in hits


def test_delta_zero_coin_is_a_value_not_missing(delta_keywords: list[X.Keyword]) -> None:
    """样文 `0曼德尔币`：0 是有效值；没写该段才是不命中（不猜 0）。"""
    got = X.extract_listing(delta_keywords, listing_id="D3",
                            title="【货币】26三角币，0曼德尔币，169三角券")
    assert got.features["delta_mandela_coin_cnt"] == 0
    assert got.features["delta_triangle_coin_cnt"] == 26
    assert got.features["delta_triangle_coupon_cnt"] == 169
    missing = X.extract_listing(delta_keywords, listing_id="D4", title="【货币】26三角币，169三角券")
    assert "delta_mandela_coin_cnt" not in missing.features, "未写该段 = 不命中，不编造 0"


def test_delta_detail_sections_and_positive_tags(delta_keywords: list[X.Keyword]) -> None:
    """详情页段式：皮肤分门计数 + 挂饰/载具 + 战损比 + 正面标签（进阶安全箱/可二次实名/QQ登录）。"""
    text = ("【安全箱】进阶安全箱；【特勤处等级】仓库LV.8，训练中心LV.6；"
            "【干员皮肤5】露娜黑天际线；【已有捆绑包5】黑天际线捆绑包；【近战皮肤3】近战武器-处刑者；"
            "【挂饰34】挂饰-无人机；【载具3】轮式突击炮-荣耀；战损比0.4/1.1/1.5；"
            "【QQ登录】【可二次实名】")
    got = X.extract_listing(delta_keywords, listing_id="D5", title=text)
    feat = got.features
    assert feat["delta_operator_skin_cnt"] == 5 and feat["delta_bundle_cnt"] == 5
    assert feat["delta_melee_skin_cnt"] == 3 and feat["delta_charm_cnt"] == 34
    assert feat["delta_vehicle_cnt"] == 3
    assert feat["delta_second_realname_flag"] is True, "可二次实名是正面标签（不吃折价系数）"
    assert "delta_service_recall_flag" not in feat, "样文没有 找回包赔 就不命中"
    assert {"进阶安全箱", "安全箱", "战损比", "QQ登录", "可二次实名"} <= set(_hits_map(got))


def test_delta_no_hit_is_not_fabricated(delta_keywords: list[X.Keyword]) -> None:
    got = X.extract_listing(delta_keywords, listing_id="D6", title="三角洲行动 官服 出号")
    assert got.hit is False and got.features == {}


def test_delta_footer_boilerplate_is_not_a_hit(delta_keywords: list[X.Keyword]) -> None:
    """页尾提示语（每张详情页都有）不得当命中：典藏/安全箱 须带数量或【】/档位前缀。"""
    boiler = ("【典藏皮肤颜色请以典藏展示为准，安全箱等时效性道具请以游戏内数据验号为准】"
              "【QQ登录】【可二次实名】【官方截图】")
    got = X.extract_listing(delta_keywords, listing_id="D7", title=boiler)
    hit_keywords = set(_hits_map(got))
    assert "典藏枪皮" not in hit_keywords and "安全箱" not in hit_keywords, \
        "提示语刷 100% 假阳性：典藏/安全箱 必须带数量或【】/档位前缀"
    # 正例：真实写法仍命中，且典藏数量进 hit_text
    real = X.extract_listing(delta_keywords, listing_id="D8",
                             title="【安全箱】进阶安全箱；典藏枪皮3；【干员皮肤5】露娜黑天际线")
    real_hits = _hits_map(real)
    assert real_hits["安全箱"] == "【安全箱】" and real_hits["进阶安全箱"] == "进阶安全箱"
    assert real_hits["典藏枪皮"] == "3"


def test_delta_second_realname_negation_not_confused(delta_keywords: list[X.Keyword]) -> None:
    """非法字串陷阱：`不可二次实名` 含子串"可二次实名"，正向词必须 lookbehind 排除。"""
    negative = X.extract_listing(delta_keywords, listing_id="D9",
                                 title="【QQ登录】【不可二次实名】【官方截图】")
    assert negative.features.get("delta_no_second_realname_flag") is True
    assert "delta_second_realname_flag" not in negative.features, \
        "不可二次实名 ≠ 可二次实名"
    positive = X.extract_listing(delta_keywords, listing_id="D10",
                                 title="【QQ登录】【可二次实名】")
    assert positive.features.get("delta_second_realname_flag") is True
    assert "delta_no_second_realname_flag" not in positive.features


def test_delta_kd_ratio_bracket_form_and_special_weapon_skin(
        delta_keywords: list[X.Keyword]) -> None:
    """实测写法带右括号（【烽火地带战损比】11.3/1.1/1.5）；
    特殊武器皮肤是独立类目，不得被读成武器皮肤总数。"""
    got = X.extract_listing(delta_keywords, listing_id="D11",
                            title="武器皮肤13；【特殊武器皮肤】复合弓-黑-天际线；"
                                  "【烽火地带战损比】11.3/1.1/1.5")
    assert got.features["delta_weapon_skin_cnt"] == 13, "特殊武器皮肤不计入总数"
    assert _hits_map(got)["战损比"] == "11.3/1.1/1.5", "战损比须吃掉右括号】再取值"
    special_only = X.extract_listing(delta_keywords, listing_id="D12",
                                     title="【特殊武器皮肤2】复合弓-黑-天际线")
    assert "delta_weapon_skin_cnt" not in special_only.features, \
        "只有特殊武器段时不得编造武器皮肤总数"
