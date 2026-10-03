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
    assert len(profiles) == 2, "原神 10026 + 鸣潮 10302（docs/02 §4.A1 + §4.A4）"
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
    assert len(rows) == 43, "原神 18 条 + 鸣潮 25 条（docs/02 §4.A1 + §4.A4）"
    from pxb7 import db
    for row in rows:
        assert set(row) == set(db.KEYWORD_COLUMNS)
        assert row["enabled"] in (True, False)
    per_profile: dict[str, int] = {}
    for row in rows:
        per_profile[row["profile_id"]] = per_profile.get(row["profile_id"], 0) + 1
    assert per_profile == {"genshin_10026": 18, "wuwa_10302": 25}
    assert len({row["keyword_id"] for row in rows}) == len(rows), "keyword_id 不得重复"


def test_seed_profiles_scoped_by_game_id() -> None:
    """gateway.py:572 按 game_id 取词表；未建画像的游戏返回空表、不用别家顶替（docs/02 §G）。"""
    genshin = X.seed_keywords(game_id=10026)
    wuwa = X.seed_keywords(game_id=10302)
    assert {kw.profile_id for kw in genshin} == {"genshin_10026"} and len(genshin) == 18
    assert {kw.profile_id for kw in wuwa} == {"wuwa_10302"} and len(wuwa) == 25
    assert X.seed_keywords(game_id=10371) == [], "三角洲尚无画像（种子只有 10026/10302）"


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
    assert got.features["constellation_cnt"] == 6, "满 → 6（extract.py 归一约定）"
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
