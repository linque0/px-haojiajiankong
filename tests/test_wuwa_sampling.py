"""采样计划与条件核对的未知/零值、边界和组合回归。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from prepare_wuwa_sampling_plan import matches, specifications
from audit_wuwa_sampling_batch import v2_team_specifications


def record(**changes):
    result = {"raw": {"区服": "官服", "黄数": "50", "等级": "80", "五星角色": "20", "额外付费商品": ""},
              "price": 299.99, "resources": {}, "chars": {"爱弥斯": 6}, "weapons": [("光影双生", 1)]}
    result.update(changes)
    return result


def task(key):
    return next(t for t in specifications() if t["task_id"] == key)


def test_missing_resource_does_not_become_zero_or_success():
    assert matches(record(), task("R02")) is None
    assert matches(record(resources={"浮金波纹": 0}), task("R02")) is False
    assert matches(record(resources={"浮金波纹": 20}), task("R02")) is True


def test_price_bands_cover_cent_boundaries():
    for price, expected in [(299.99, "P01"), (300, "P02"), (999.99, "P02"), (1000, "P03")]:
        matched = [key for key in ["P01", "P02", "P03"] if matches(record(price=price), task(key))]
        assert matched == [expected]


def test_combo_checks_every_dimension():
    assert matches(record(resources={"星声": 6400}), task("X01")) is True
    assert matches(record(resources={"星声": 100}), task("X01")) is False
    assert matches(record(chars={"嘉贝莉娜": 0}), task("X03")) is True
    assert matches(record(chars={"嘉贝莉娜": 0}, weapons=[("其他武器", 1)]), task("X03")) is False


def test_full_chain_and_unverified_team_are_distinct():
    assert matches(record(), task("E02")) is True
    assert matches(record(chars={"爱弥斯": 5}), task("E02")) is False
    assert matches(record(), task("E04")) is None


def test_unlisted_skin_is_unknown_and_wrong_server_excluded():
    assert matches(record(), task("E05")) is None
    raw = {**record()["raw"], "额外付费商品": "人物皮肤：桃夭灼灼；车架模组：云帛机骑"}
    assert matches(record(raw=raw), task("X05")) is True
    assert matches(record(raw={**raw, "区服": "国际服"}), task("X05")) is False


def test_v2_full_team_requires_all_three_owned_with_any_chain_count():
    spec = next(t for t in v2_team_specifications() if t["task_id"] == "V2-T01")
    assert spec["team_members"] == ["爱弥斯", "达妮娅", "千咲"]
    assert matches(record(chars={"爱弥斯": 6, "达妮娅": 0, "千咲": 2}), spec) is True
    assert matches(record(chars={"爱弥斯": 6, "达妮娅": 0}), spec) is False
    assert len(v2_team_specifications()) == 6
