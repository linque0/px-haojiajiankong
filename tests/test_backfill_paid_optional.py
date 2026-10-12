"""额外付费商品可选：不伪造值、不改变其他必填检测，使用临时库。"""
import json
from pathlib import Path

import pytest

from pxb7 import backfill as B, db
from tests.test_gateway import settings, state, _post, DETAIL_PAYLOAD
from tests.test_backfill import row, seed


PAID_KEYS = ("vehicle_frame_modules", "motorcycle_ornaments", "character_skins")


@pytest.mark.parametrize("paid", [{}, {k: None for k in PAID_KEYS},
                                  {k: [] for k in PAID_KEYS},
                                  {"character_skins": [{"name": "皮肤"}]}])
def test_paid_optional_but_core_missing_still_reported(paid):
    account = row()
    account["features"] = {k: v for k, v in account["features"].items() if k not in PAID_KEYS} | paid
    assert B.missing(account) == []
    assert {r["key"] for r in B.missing(account | {"five_star_weapons": None})} == {"five_star_weapons"}


def seed_wuwa(state):
    seed(state)
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE dim_listing SET game_id=10302")
        conn.execute("UPDATE fct_listing_snapshot SET level=80,yellow_cnt=0,five_star_chars=0,"
                     "five_star_weapons=0,server='官服',publish_time_text='21分钟内发布',"
                     "favorites_cnt=0,extracted_features=?", [json.dumps({"astrite_cnt": 0})])
        items = B.latest_rows(conn)
    state.config["targets"] = ["wuwa_official"]
    return [{"id": r["id"], "snapshot_round": r["snapshot_round"]} for r in items]


def test_paid_only_candidate_disappears_and_old_selection_rejected(state):
    items = seed_wuwa(state)
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        before = conn.execute("SELECT * FROM fct_listing_snapshot ORDER BY listing_id").fetchall()
    result = B.candidates(state, {})
    assert result["total"] == 0
    assert "额外付费商品" not in result["coverage"]["10302"]
    for action, payload in ((B.prepare, {"items": items}),
                            (B.enqueue, {"items": items}),
                            (B.delete, {"items": items, "confirm_all_rounds": True})):
        with pytest.raises(ValueError, match="selection-changed-refresh-list"):
            action(state, payload)
    assert not list((state.settings.paths.db.parent / "backups").glob("*.duckdb"))
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        assert before == conn.execute("SELECT * FROM fct_listing_snapshot ORDER BY listing_id").fetchall()


def test_backfill_receipt_complete_without_paid_fields(state):
    items = seed_wuwa(state)
    item = next(i for i in items if i["id"] == "2428022628844555568")
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET level=NULL WHERE listing_id=?", [item["id"]])
    assert B.candidates(state, {})["total"] == 1
    status, ack = _post(state, "/ingest/detail", {
        **DETAIL_PAYLOAD, "backfill": True, "snapshot_round": item["snapshot_round"],
        "html": '<a href="/buy/10302/1">鸣潮</a><div class="product-detail"><h1>等级80</h1></div>'})
    assert status == 200 and ack["backfill"] == {"complete": True, "missing": []}
    assert B.candidates(state, {})["total"] == 0
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        features = json.loads(conn.execute("SELECT extracted_features FROM fct_listing_snapshot WHERE listing_id=?",
                                         [item["id"]]).fetchone()[0])
    assert not set(PAID_KEYS) & features.keys(), "不得为了消缺伪造付费商品空列表"
    assert Path(ack["analysis_sync"]["path"]).is_file()
