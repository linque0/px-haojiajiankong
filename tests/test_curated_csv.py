"""看板列精选导出测试（analysis.export_curated_csv，2026-10-04 用户指令驱动）。

覆盖：鸣潮表头 = 用户指定列序；链/精炼/资源/付费商品的渲染口径与看板 dashboard.html
cellText 一致（0 是值、未采到省略、缺失回退计数）；同步护栏——资源/付费商品口径
单一定义在 extract（gateway 引用不复制）；导出集成（BOM/行数/只含该游戏/缺版式报错）。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from pxb7 import analysis as A  # noqa: E402
from pxb7 import db  # noqa: E402
from pxb7 import extract as X  # noqa: E402
from pxb7 import gateway as G  # noqa: E402

from tests.test_analysis import _listing, _snapshot, seeded, settings  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# 口径常量（单一来源 = extract；gateway 只准引用）
# --------------------------------------------------------------------------- #
def test_wuwa_resources_and_paid_items_single_source() -> None:
    expected_resources = (("astrite_cnt", "星声"), ("lunite_cnt", "月相"),
                          ("afterglow_coral_cnt", "余波珊瑚"),
                          ("lustrous_tide_cnt", "浮金波纹"), ("radiant_tide_cnt", "铸潮波纹"))
    expected_paid = (("vehicle_frame_modules", "车架模组"),
                     ("motorcycle_ornaments", "摩托饰品"),
                     ("character_skins", "人物皮肤"))
    assert X.WUWA_RESOURCES == expected_resources
    assert X.WUWA_PAID_ITEMS == expected_paid
    assert G.WUWA_RESOURCES is X.WUWA_RESOURCES, "gateway 必须引用 extract，不得复制口径"


def test_wuwa_curated_headers_match_user_spec() -> None:
    assert A.WUWA_CURATED_HEADERS == (
        "listing_id", "游戏", "价格 ¥", "等级", "黄数", "五星角色", "五星武器",
        "共鸣链（N命）", "武器精炼（精N）", "资源", "额外付费商品", "区服",
        "商品发布时间", "收藏")


# --------------------------------------------------------------------------- #
# 单元格渲染（与 dashboard.html cellText 同口径）
# --------------------------------------------------------------------------- #
def test_roster_text_chains_and_refinements() -> None:
    chains = [{"name": "忌炎", "value": 6}, {"name": "鉴心", "value": 4},
              {"name": "守岸人", "value": 0}]
    assert A._roster_text(chains, suffix="命") == "6命忌炎、4命鉴心、0命守岸人"
    refs = [{"name": "千古洑流", "value": 1}, {"name": "停驻之烟", "value": 5}]
    assert A._roster_text(refs, prefix="精") == "精1千古洑流、精5停驻之烟"
    assert A._roster_text([], suffix="命") is None
    assert A._roster_text(None, prefix="精") is None


def _bare_row(**overrides: object) -> dict[str, object]:
    """_fmt_wuwa_cells 输入的最小行（全部键都有，特征为空）。"""
    row = {"listing_id": "W0", "game_name": "鸣潮", "price_yuan": None, "level": None,
           "yellow_cnt": None, "five_star_chars": None, "five_star_weapons": None,
           "feat_constellation_cnt": None, "feat_five_star_weapon_refined": None,
           "extracted_features": None, "server": None, "publish_time": None,
           "favorites_cnt": None}
    row.update(overrides)
    return row


def test_wuwa_cells_resources_zero_is_value_none_omitted() -> None:
    row = _bare_row(extracted_features=json.dumps({
        "astrite_cnt": 0, "afterglow_coral_cnt": 22,
        "lunite_cnt": None, "lustrous_tide_cnt": 14,
    }))
    cells = A._fmt_wuwa_cells(row)
    assert cells[9] == "星声：0；余波珊瑚：22；浮金波纹：14"   # 0 照列；月相未采到省略
    assert cells[10] is None                                    # 无付费商品 → 空
    assert cells[12] is None                                    # 无发布时间 → 空


def test_wuwa_cells_fallbacks_roster_first() -> None:
    rich = _bare_row(extracted_features=json.dumps({
        "constellation_cnt": 9, "five_star_character_chains":
            [{"name": "长离", "value": 6}],
        "five_star_weapon_refinements": [{"name": "音曦", "value": 3}],
        "vehicle_frame_modules": ["云帛机骑"], "motorcycle_ornaments": ["绯雪团子"],
        "character_skins": ["桃夭灼灼"],
    }))
    cells = A._fmt_wuwa_cells(rich)
    assert cells[7] == "6命长离"                                # 具名清单优先于计数
    assert cells[8] == "精3音曦"
    assert cells[10] == "车架模组：云帛机骑；摩托饰品：绯雪团子；人物皮肤：桃夭灼灼"
    bare = _bare_row(extracted_features=json.dumps(
        {"constellation_cnt": 5, "five_star_weapon_refined": 2}))
    cells = A._fmt_wuwa_cells(bare)
    assert cells[7] == 5                                        # 无清单回退计数
    assert cells[8] == 2


# --------------------------------------------------------------------------- #
# 导出集成
# --------------------------------------------------------------------------- #
def test_export_curated_csv_writes_bom_and_exact_rows(seeded, tmp_path) -> None:  # noqa: F811
    # 鸣潮行补充完整特征（含 0 值资源、具名链、付费商品、发布时间）
    with db.connect(seeded.paths.db) as conn:
        _listing(conn, "W9", 10302, "鸣潮 满命长离 星声0 官服", "2026-10-04 09:00:00")
        _snapshot(conn, "W9", "2026-10-04 09:00:00", 456.0, level=80, yellow=44,
                  five_chars=18, five_weapons=2,
                  features=json.dumps({
                      "account_level_cnt": 80, "yellow_cnt": 44,
                      "constellation_cnt": 6, "five_star_character_chains":
                          [{"name": "长离", "value": 6}, {"name": "守岸人", "value": 0}],
                      "five_star_weapon_refinements": [{"name": "停驻之烟", "value": 1}],
                      "astrite_cnt": 0, "afterglow_coral_cnt": 7, "lunite_cnt": 1020,
                      "vehicle_frame_modules": ["云帛机骑"],
                      "character_skins": ["桃夭灼灼"],
                  }), publish="2026-10-03 18:00:00")

    # out_path 必须显式给 tmp：默认路径基于真实项目根，缺省会覆盖线上数据表
    # （2026-10-04 事故：无参调用把 data/analysis/by_game 的 204 行真实表覆盖成 2 行夹具）
    result = A.export_curated_csv(seeded, tmp_path / "wuwa.csv", game_id=10302)
    path = Path(result["path"])
    assert path == tmp_path / "wuwa.csv"
    assert result["rows"] == 2 and result["game_name"] == "鸣潮"
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"

    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == list(A.WUWA_CURATED_HEADERS)
    w9 = next(r for r in rows if r["listing_id"] == "W9")
    assert w9["游戏"] == "鸣潮" and w9["价格 ¥"] == "456.00"
    assert w9["等级"] == "80" and w9["黄数"] == "44" and w9["五星角色"] == "18"
    assert w9["共鸣链（N命）"] == "6命长离、0命守岸人"
    assert w9["武器精炼（精N）"] == "精1停驻之烟"
    assert w9["资源"] == "星声：0；月相：1020；余波珊瑚：7"
    assert w9["额外付费商品"] == "车架模组：云帛机骑；人物皮肤：桃夭灼灼"
    assert w9["商品发布时间"] == "2026-10-03 18:00:00"
    # 原神行（A1/B2）不得混入鸣潮表
    assert all(r["游戏"] == "鸣潮" for r in rows)


def test_export_curated_csv_rejects_unknown_game_and_missing_game(seeded) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="需要 --game"):
        A.export_curated_csv(seeded)
    with pytest.raises(ValueError, match="未登记看板列版式"):
        A.export_curated_csv(seeded, game_id=10026)   # 原神暂未登记
