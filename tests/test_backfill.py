"""查缺 → 备份 → 最新轮次补录 → 原 CSV 同步，全部使用临时库及离线 DOM。"""
import datetime as dt
import json
import csv
from pathlib import Path

import pytest

from pxb7 import backfill as B, db, gateway as G
from tests.test_gateway import settings, state, _post, CARDS_PAYLOAD, DETAIL_PAYLOAD


def row(**changes):
    """鸣潮（10302）完整基线：资源 0 是值、付费商品"已核为空"空列表、聚合值顶替名单。"""
    features = {"constellation_cnt": 0, "five_star_weapon_refined": 0,
                "astrite_cnt": 0, "vehicle_frame_modules": []}
    features.update(changes.pop("features", {}))
    return {"game_id": 10302, "level": 80, "yellow_cnt": 0, "five_star_chars": 0,
            "five_star_weapons": 0, "server": "官服", "publish_time_text": "21分钟内发布",
            "favorites_cnt": 3, "features": features, **changes}


def test_missing_zero_optional_and_rosters():
    assert B.missing(row()) == []
    assert B.missing(row(level=None))[0]["key"] == "level"
    assert B.missing(row(level=float("nan")))
    assert B.missing(row(level=1.5))
    assert not B.missing(row(level=None, features={"account_level_cnt": 80}))
    assert B.missing(row(five_star_chars=1))[0]["key"] == "five_star_character_chains"
    characters = [{"name": "安可", "value": 0}]
    assert not B.missing(row(five_star_chars=1, features={"five_star_character_chains": characters}))
    assert B.missing(row(five_star_chars=2, features={"five_star_character_chains": characters * 2}))
    weapons = [{"name": "千古洑流", "value": 1}] * 2
    assert not B.missing(row(five_star_weapons=2, features={"five_star_weapon_refinements": weapons}))
    assert B.missing(row(five_star_weapons=2, features={"five_star_weapon_refinements": [{}]}))
    assert B.missing(row(five_star_weapons=1, features={"five_star_weapon_refinements": [{"name": "千古洑流...", "value": 1}]}))
    assert not B.missing(row(game_id=10032))
    assert not B.missing(row(game_id=99999))
    assert len(B.missing(row(game_id=10371))) == 5


def test_wuwa_kanban_columns_missing():
    """鸣潮必填列：资源/区服/发布时间/收藏；额外付费商品可为空。"""
    resources = ("astrite_cnt", "lunite_cnt", "afterglow_coral_cnt", "lustrous_tide_cnt", "radiant_tide_cnt")
    paid = ("vehicle_frame_modules", "motorcycle_ornaments", "character_skins")
    # 资源五件套全缺才记缺失；采到 0 是有效值
    assert [x["key"] for x in B.missing(row(features={k: None for k in resources}))] == ["wuwa_resources"]
    assert B.missing(row(features={"lunite_cnt": 0})) == []
    # 未采到付费商品是正常情况，不要求通过补录写入空列表才能消缺。
    assert B.missing(row(features={k: None for k in paid})) == []
    assert B.missing(row(features={k: None for k in paid} | {"motorcycle_ornaments": []})) == []
    # 区服 / 商品发布时间 / 收藏（快照列）
    assert {x["key"] for x in B.missing(row(server=None))} == {"server"}
    assert {x["key"] for x in B.missing(row(publish_time_text=""))} == {"publish_time_text"}
    assert {x["key"] for x in B.missing(row(favorites_cnt=None))} == {"favorites_cnt"}
    assert {x["key"] for x in B.missing(row(server=None, favorites_cnt=None,
                                             publish_time_text=None))} == \
        {"server", "publish_time_text", "favorites_cnt"}
    # 数量为 0 不要求具名名单（既有 0 值纪律），聚合值顶替即可
    assert not [x for x in B.missing(row()) if x["key"].endswith(("_chains", "_refinements"))]
    # 其他游戏不受鸣潮看板列扩展影响
    assert B.missing(row(game_id=10032)) == []


def seed(state):
    ack = _post(state, "/ingest/cards", CARDS_PAYLOAD)[1]
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET five_star_weapons=NULL, extracted_features='{}'")
    return ack


def test_latest_pagination_selection_and_backup(state):
    seed(state)
    original = B.candidates(state, {"limit": "1"})
    assert original["total"] == 2 and len(original["rows"]) == 1
    assert B.candidates(state, {"game_id": "10302"})["total"] == 0
    first = original["rows"][0]
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("INSERT INTO fct_listing_snapshot SELECT * REPLACE (? AS snapshot_at) "
                     "FROM fct_listing_snapshot WHERE listing_id=?",
                     [dt.datetime.fromisoformat(first["snapshot_round"]) + dt.timedelta(minutes=30), first["id"]])
    assert B.candidates(state, {})["total"] == 2
    with pytest.raises(ValueError, match="selection-changed"):
        B.prepare(state, {"items": [first]})
    prepared = B.prepare(state, {"all": True})
    assert prepared["total"] == 2 and Path(prepared["backup"]).is_file()
    with db.connect(prepared["backup"], read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 3
    assert all(item["url"] == f'https://www.pxb7.com/product/{item["id"]}/1' for item in prepared["items"])


def test_prepare_target_gate_and_backup_failure(state, monkeypatch):
    seed(state)
    monkeypatch.setattr(state, "match_task", lambda game: None)
    assert not B.candidates(state, {})["rows"][0]["collectable"]
    with pytest.raises(ValueError, match="target-not-selected"):
        B.prepare(state, {"all": True})
    monkeypatch.undo()
    def locked(*args):
        raise PermissionError("backup locked")
    monkeypatch.setattr(B.shutil, "copy2", locked)
    with pytest.raises(PermissionError):
        B.prepare(state, {"all": True})


def test_backfill_refresh_clears_old_assets_preserves_history_and_syncs(state):
    seed(state)
    listing = "2428022628844555568"
    with db.connect(state.settings.paths.db) as conn:
        snapshot = conn.execute("SELECT snapshot_at FROM fct_listing_snapshot WHERE listing_id=?", [listing]).fetchone()[0]
        old = snapshot - dt.timedelta(minutes=30)
        conn.execute("INSERT INTO fct_listing_snapshot SELECT * REPLACE (? AS snapshot_at) "
                     "FROM fct_listing_snapshot WHERE listing_id=?", [old, listing])
        features = {"five_star_character_chains": [{"name": "旧角色", "value": 6}],
                    "constellation_cnt": 6, "five_star_weapon_refinements": [{"name": "旧武器", "value": 5}],
                    "five_star_weapon_refined": 5}
        conn.execute("UPDATE fct_listing_snapshot SET five_star_chars=1, five_star_weapons=1, extracted_features=? "
                     "WHERE listing_id=? AND snapshot_at=?", [json.dumps(features), listing, snapshot])
        before = conn.execute("SELECT listing_id,snapshot_at,price_yuan FROM fct_listing_snapshot ORDER BY 1,2").fetchall()
    html = '<div class="product-detail"><h1>等级80 黄数4 五星角色：2 五星武器：2</h1></div>'
    payload = {**DETAIL_PAYLOAD, "backfill": True, "snapshot_round": snapshot.isoformat(), "html": html}
    status, ack = _post(state, "/ingest/detail", payload)
    assert status == 200 and ack["snapshot_rows_updated"] == 1
    assert not ack["backfill"]["complete"] and ack["analysis_sync"]["ok"]
    with Path(ack["analysis_sync"]["path"]).open(encoding="utf-8-sig", newline="") as stream:
        exported = next(r for r in csv.DictReader(stream) if r["listing_id"] == listing)
        assert exported["five_star_chars"] == "2" and exported["five_star_weapons"] == "2"
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        assert before == conn.execute("SELECT listing_id,snapshot_at,price_yuan FROM fct_listing_snapshot ORDER BY 1,2").fetchall()
        current = conn.execute("SELECT five_star_chars,five_star_weapons,extracted_features FROM fct_listing_snapshot "
                               "WHERE listing_id=? AND snapshot_at=?", [listing, snapshot]).fetchone()
        f = json.loads(current[2])
        assert current[:2] == (2, 2) and "_backfilled_at" in f
        assert "five_star_character_chains" not in f and "five_star_weapon_refinements" not in f
        assert conn.execute("SELECT extracted_features FROM fct_listing_snapshot WHERE listing_id=? AND snapshot_at=?",
                            [listing, old]).fetchone()[0] == "{}"
    payload["snapshot_round"] = old.isoformat()
    assert _post(state, "/ingest/detail", payload) == (409, {"ok": False, "error": "latest-snapshot-changed"})
    assert not state.pending_details


def test_sync_failure_is_separate_and_retry_needs_no_web(state, monkeypatch):
    seed(state)
    selected = B.prepare(state, {"all": True})["items"][0]
    def locked(*args):
        raise PermissionError("CSV locked")
    monkeypatch.setattr(Path, "replace", locked)
    status, ack = _post(state, "/ingest/detail", {**DETAIL_PAYLOAD, **{"url": selected["url"],
                        "listing_id": selected["id"], "snapshot_round": selected["snapshot_round"], "backfill": True}})
    assert status == 200 and ack["snapshot_rows_updated"] == 1 and not ack["analysis_sync"]["ok"]
    raw_count = len(list(state.settings.paths.raw_root.rglob("*.html")))
    monkeypatch.undo()
    assert B.sync(state, {"game_ids": [10026]})["results"][0]["ok"]
    assert len(list(state.settings.paths.raw_root.rglob("*.html"))) == raw_count


def test_invalid_backfill_rejected(state):
    assert _post(state, "/ingest/detail", {**DETAIL_PAYLOAD, "backfill": True})[0] == 400


def test_orphan_snapshot_visible_and_deletable(state):
    """快照在而档案缺失的孤儿行必须进清单（否则既不可补录也不可达删除）。"""
    seed(state)
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("INSERT INTO fct_listing_snapshot SELECT * REPLACE ('999999999' AS listing_id) "
                     "FROM fct_listing_snapshot LIMIT 1")
        conn.execute("UPDATE fct_listing_snapshot SET level=NULL WHERE listing_id='999999999'")
    rows = B.candidates(state, {"game_id": "all"})["rows"]
    orphan = next(row for row in rows if row["id"] == "999999999")
    assert orphan["game_id"] is None and orphan["game_name"] == "未知档案" and not orphan["collectable"]
    assert B.candidates(state, {"game_id": "10026"})["total"] == 2   # 孤儿不混入具体游戏筛选
    assert all(game["game_id"] is not None for game in B.candidates(state, {})["games"])
    result = B.delete(state, {"items": [{"id": "999999999", "snapshot_round": orphan["snapshot_round"]}],
                              "confirm_all_rounds": True})
    assert result["deleted"] == 1 and result["results"] == []        # 无游戏档案则无 CSV 同步
    assert B.candidates(state, {"game_id": "all"})["total"] == 2


def test_results_ledger_annotates_candidates_and_delete_purges(state):
    seed(state)
    item = B.candidates(state, {})["rows"][0]
    B.record_result(state, item["id"], snapshot_round=item["snapshot_round"],
                    status="failed", error="delisted")
    annotated = next(r for r in B.candidates(state, {})["rows"] if r["id"] == item["id"])
    assert annotated["last_backfill"]["status"] == "failed"
    with pytest.raises(ValueError):
        B.report_result(state, {"id": item["id"], "snapshot_round": item["snapshot_round"],
                                "status": "bogus"})
    result = B.delete(state, {"items": [{"id": item["id"], "snapshot_round": item["snapshot_round"]}],
                              "confirm_all_rounds": True})
    assert result["deleted"] == 1 and item["id"] not in B.load_results(state)


def test_backfill_job_broker_lifecycle(state):
    """完整看板发起 → 网关排队 → 扩展领取 → 启动回报；内存态、不可重复领取。"""
    seed(state)
    assert B.job_status(state)["job"] is None
    assert B.claim_job(state) == {"ok": False, "error": "no-pending-job"}
    assert B.finish_job(state, {"ok": True})["ok"] is False
    enqueued = B.enqueue(state, {"all": True})
    assert enqueued["ok"] and enqueued["total"] == 2 and Path(enqueued["backup"]).is_file()
    assert B.job_status(state)["job"]["status"] == "pending"
    claimed = B.claim_job(state)
    assert claimed["ok"] and len(claimed["items"]) == 2
    assert all(item["url"] == f'https://www.pxb7.com/product/{item["id"]}/1' for item in claimed["items"])
    assert B.job_status(state)["job"]["status"] == "claimed"
    assert B.claim_job(state)["ok"] is False
    assert B.finish_job(state, {"ok": True})["ok"]
    assert B.job_status(state)["job"]["status"] == "accepted"
    B.enqueue(state, {"all": True})
    B.finish_job(state, {"ok": False, "error": "补录任务启动失败"})
    job = B.job_status(state)["job"]
    assert job["status"] == "failed" and "启动失败" in job["error"]


def test_http_backfill_routes_validate_local_origin(state):
    import threading
    import urllib.request
    import urllib.error
    seed(state)
    handler = type("TestHandler", (G.GatewayHandler,), {"state": state})
    server = G.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def request(path, body=None, origin="chrome-extension://test-extension"):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
              headers={"Content-Type": "application/json", "Origin": origin})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as response:
            return response.code, json.loads(response.read())
    try:
        assert request("/backfill/candidates?limit=1")[1]["total"] == 2
        assert request("/backfill/candidates?offset=bad")[0] == 400
        assert request("/backfill/prepare", {"all": True}, "https://www.pxb7.com")[0] == 403
        assert request("/backfill/delete", {"items": []}, "https://www.pxb7.com")[0] == 403
        assert request("/backfill/enqueue", {"all": True}, "https://www.pxb7.com")[0] == 403
        assert request("/backfill/result", {}, "https://www.pxb7.com")[0] == 403
        assert request("/backfill/job/claim", {}, "https://www.pxb7.com")[0] == 403
        assert not list((state.settings.paths.db.parent / "backups").glob("*.duckdb"))
        assert request("/backfill/prepare", [])[0] == 400
        assert request("/backfill/prepare", {"all": True})[1]["total"] == 2
        assert request("/backfill/sync", {"game_ids": [10026]})[1]["results"][0]["ok"]
        assert request("/backfill/delete", {"items": []})[0] == 400
        current = request("/backfill/candidates")[1]["rows"]
        deleted = request("/backfill/delete", {"items": current, "confirm_all_rounds": True})
        assert deleted[0] == 200 and deleted[1]["deleted"] == 2
        assert deleted[1]["results"][0]["rows"] == 0
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_backfill_transaction_rolls_back_and_zero_clears_rosters(state, monkeypatch):
    seed(state)
    item = B.candidates(state, {})["rows"][0]
    entry = {"listing_id": item["id"], "snapshot_at": dt.datetime.fromisoformat(item["snapshot_round"]),
             "attributes": {"five_star_weapons": 0, "five_star_chars": 0}, "features": {}}
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET extracted_features=? WHERE listing_id=?",
                     [json.dumps({"five_star_weapon_refinements": [{"name": "千古洑流", "value": 1}]}), item["id"]])
        before = conn.execute("SELECT * FROM fct_listing_snapshot ORDER BY listing_id").fetchall()
        def fail(*args, **kwargs):
            raise RuntimeError("failed write")
        monkeypatch.setattr(db, "update_detail_fields", fail)
        with pytest.raises(RuntimeError):
            B.apply(conn, entry)
        assert before == conn.execute("SELECT * FROM fct_listing_snapshot ORDER BY listing_id").fetchall()
        monkeypatch.undo()
        assert B.apply(conn, entry) == 1
        raw = conn.execute("SELECT extracted_features FROM fct_listing_snapshot WHERE listing_id=?", [item["id"]]).fetchone()[0]
        assert "five_star_weapon_refinements" not in json.loads(raw)
        previous = {"five_star_character_chains": [{"name": "安可", "value": 0}],
                    "_detail_fields": ["five_star_chars"], "_detail_rosters": ["five_star_character_chains"]}
        conn.execute("UPDATE fct_listing_snapshot SET five_star_chars=1,extracted_features=? WHERE listing_id=?",
                     [json.dumps(previous), item["id"]])
        entry["attributes"] = {"level": 80}
        entry["features"] = {"_detail_fields": ["level"], "_detail_rosters": []}
        assert B.apply(conn, entry) == 1
        raw = conn.execute("SELECT extracted_features FROM fct_listing_snapshot WHERE listing_id=?", [item["id"]]).fetchone()[0]
        saved = json.loads(raw)
        assert "five_star_chars" in saved["_detail_fields"]
        assert saved["_detail_rosters"] == ["five_star_character_chains"]
