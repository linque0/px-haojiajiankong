"""估值的重要口径与泄漏边界；不访问网络。"""
import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("wuwa_valuation", Path(__file__).parents[1] / "tools" / "wuwa_valuation.py")
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)


def row(**changes):
    result = {"listing_id": "123", "游戏": "鸣潮", "价格 ¥": "500", "等级": "80",
              "黄数": "13", "五星角色": "2", "五星武器": "2",
              "共鸣链（N命）": "6命卡提希娅、1命维里奈",
              "武器精炼（精N）": "精3千古洑流、精1千古洑流",
              "资源": "星声：160；月相：160；余波珊瑚：8；浮金波纹：0",
              "额外付费商品": "人物皮肤：桃夭灼灼、叱妖诰", "区服": "官服",
              "商品发布时间": "2026-10-05 10:00:00", "收藏": "3"}
    result.update(changes)
    return result


def test_yellow_copies_do_not_deduplicate_identical_weapons():
    r = v.parse_row(row())
    assert r["eligible"]
    assert r["char_copies"] == 9 and r["weapon_copies"] == 4
    assert len(r["weapons"]) == 2
    assert r["features"]["standard_weapon_refine_extra"] == 2


def test_missing_resources_are_unknown_not_zero():
    r = v.parse_row(row())
    assert r["resources"]["浮金波纹"] == 0
    assert "铸潮波纹" not in r["resources"]
    assert r["features"]["missing_resource:铸潮波纹"] == 1
    assert r["features"]["missing_resource:浮金波纹"] == 0
    assert r["features"]["observed_flexible_pulls"] == 2
    assert r["features"]["coral_pulls"] == 1


def test_character_duplicates_flagged_without_inflating_copies():
    r = v.parse_row(row(**{"共鸣链（N命）": "6命卡提希娅、0命卡提希娅、1命维里奈"}))
    assert not r["eligible"] and r["char_copies"] == 9
    assert any(f.startswith("duplicate_character") for f in r["flags"])


@pytest.mark.parametrize("changes,flag", [
    ({"共鸣链（N命）": "7命卡提希娅"}, "character_unparsed"),
    ({"武器精炼（精N）": "精0千古洑流"}, "weapon_unparsed"),
    ({"武器精炼（精N）": ""}, "weapon_detail_missing"),
    ({"黄数": "99"}, "yellow_mismatch"),
    ({"区服": "国际服"}, "unsupported_server"),
    ({"游戏": "原神"}, "wrong_game"),
])
def test_bad_or_unsupported_rows_are_not_training_inputs(changes, flag):
    r = v.parse_row(row(**changes))
    assert not r["eligible"] and flag in r["flags"]


def test_prices_favorites_ids_and_dates_are_not_features_or_group_keys():
    a = v.parse_row(row())
    b = v.parse_row(row(**{"listing_id": "456", "价格 ¥": "10000", "收藏": "999", "商品发布时间": "2026-09-01"}))
    assert a["features"] == b["features"]
    assert a["fingerprint"] == b["fingerprint"]


def test_group_split_keeps_same_configuration_together():
    records = [v.parse_row(row(listing_id=str(i), **{"资源": f"星声：{i * 160}"})) for i in range(15)]
    duplicate = copy.deepcopy(records[4])
    duplicate["id"] = "duplicate"
    records.append(duplicate)
    folds = v.group_folds(records, 5, 42)
    assert len({j for j, fold in enumerate(folds) if 4 in fold or 15 in fold}) == 1
    assert sorted(i for fold in folds for i in fold) == list(range(len(records)))


def test_conformal_order_statistic_and_price_independent_prediction():
    assert v.quantile(range(1, 58), .8) == 47
    model = {"family": "yellow", "intercept": 5., "keys": ["yellow"], "medians": [1.], "mean": [1.], "scale": [1.], "coef": [.2]}
    a = v.parse_row(row())
    b = v.parse_row(row(**{"价格 ¥": "99999"}))
    assert v.predict_log(a, model) == v.predict_log(b, model)


def test_unverified_weapon_pair_is_not_invented():
    r = v.parse_row(row())
    assert r["features"]["verified_pair_count"] == 0
    paired = v.parse_row(row(**{"共鸣链（N命）": "0命嘉贝莉娜", "武器精炼（精N）": "精1光影双生"}))
    assert paired["features"]["verified_pair_count"] == 1


def test_new_name_and_extreme_configuration_require_review():
    r = v.parse_row(row())
    model = {"known_characters": ["维里奈"], "known_weapons": ["千古洑流"], "raw_max": {"other_chain_6": 0}}
    flags = v.support_warnings(r, model)
    assert "unseen_character:卡提希娅" in flags
    assert any(f.startswith("outside_training_range") for f in flags)
