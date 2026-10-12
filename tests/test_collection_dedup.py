"""账号跨批次去重、一次补采与独立历史；离线 DOM + 临时 DuckDB。"""
import datetime as dt
import json

import pytest

from pxb7 import collection as C, db, gateway as G, backfill as B
from tests.test_gateway import state, settings, CARDS_PAYLOAD, REAL_LIST_HTML, _post

ID = "2428022628844555568"
OTHER = "2427746247312293031"


def check(state, html=REAL_LIST_HTML):
    code, result = _post(state, "/collection/check", {**CARDS_PAYLOAD, "html": html})
    assert code == 200, result
    return result


def make_complete(state):
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET five_star_chars=0,five_star_weapons=0")


def test_cross_batch_restart_and_explicit_empty_selection(state, settings):
    assert check(state)["counts"]["new"] == 2
    first = _post(state, "/ingest/cards", {**CARDS_PAYLOAD, "selected_ids": [ID, OTHER]})[1]
    assert first["accepted_ids"] == [ID, OTHER]
    make_complete(state)
    assert check(state)["counts"]["skip"] == 2
    restarted = G.GatewayState(settings, state.tasks, default_task_id="genshin_official")
    assert check(restarted)["counts"]["skip"] == 2
    before = len(list(settings.paths.raw_root.rglob("*.html")))
    second = _post(restarted, "/ingest/cards", CARDS_PAYLOAD)[1]
    assert second["snapshots_inserted"] == 0
    assert set(second["skipped_ids"]) == {ID, OTHER}
    empty = _post(restarted, "/ingest/cards", {**CARDS_PAYLOAD, "selected_ids": []})[1]
    assert empty["accepted_ids"] == [] and empty["cards_parsed"] == 0
    assert len(list(settings.paths.raw_root.rglob("*.html"))) == before


def test_changes_same_round_history_and_duplicate_replay(state):
    first = _post(state, "/ingest/cards", CARDS_PAYLOAD)[1]
    changed = REAL_LIST_HTML.replace('price="30000"', 'price="25000"').replace('￥ 300 ', '￥ 250 ')
    assert next(d for d in check(state, changed)["items"] if d["id"] == ID)["kind"] == "changed"
    result = _post(state, "/ingest/cards", {**CARDS_PAYLOAD, "html": changed})[1]
    assert result["round"] == first["round"]
    assert result["updated_ids"] == [ID]
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        h = C.history(conn, ID)
        assert h["total"] == 2
        diff = next(d for d in h["rows"][0]["diff"] if d["field"] == "price_yuan")
        assert diff == {"field": "price_yuan", "before": 300, "after": 250, "kind": "change"}
    again = _post(state, "/ingest/cards", {**CARDS_PAYLOAD, "html": changed})[1]
    assert again["snapshots_inserted"] == 0
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        assert C.history(conn, ID)["total"] == 2


def test_retry_once_persists_and_completion_resets_episode(state, settings):
    first = _post(state, "/ingest/cards", CARDS_PAYLOAD)[1]
    assert check(state)["counts"]["retry"] == 2
    payload = {"id": ID, "token": "task-1:retry", "snapshot_round": first["round"], "action": "claim"}
    assert _post(state, "/collection/retry", payload)[1]["claimed"]
    assert _post(state, "/collection/retry", payload)[1]["claimed"], "同一恢复任务幂等领取"
    assert not _post(state, "/collection/retry", {**payload, "token": "task-2"})[1]["claimed"]
    _post(state, "/collection/retry", {**payload, "action": "failed"})
    assert not _post(state, "/collection/retry", payload)[1]["claimed"]
    restarted = G.GatewayState(settings, state.tasks, default_task_id="genshin_official")
    assert next(d for d in check(restarted)["items"] if d["id"] == ID)["kind"] == "manual"
    make_complete(state)
    with db.connect(settings.paths.db) as conn:
        assert C.complete_retry_state(conn, ID) == []
        conn.execute("UPDATE fct_listing_snapshot SET level=NULL WHERE listing_id=?", [ID])
    assert next(d for d in check(restarted)["items"] if d["id"] == ID)["kind"] == "retry"


def test_selected_id_validation_and_change_classification_authority(state):
    assert _post(state, "/ingest/cards", {**CARDS_PAYLOAD, "selected_ids": ["999999"]})[0] == 400
    assert _post(state, "/ingest/cards", {**CARDS_PAYLOAD, "selected_ids": [123456]})[0] == 400
    _post(state, "/ingest/cards", {**CARDS_PAYLOAD, "selected_ids": [ID]})
    result = check(state)
    assert result["counts"]["new"] == 1 and result["counts"]["retry"] == 1
    assert _post(state, "/collection/check", {**CARDS_PAYLOAD, "url": "https://evil.example/buy/10026/1"})[0] == 400


def test_unknown_values_supplements_and_order_are_semantic():
    assert C.digest({"a": [{"name": " b ", "value": 1}, {"name": "a", "value": 0}], "_at": "a"}) == \
        C.digest({"a": [{"value": 0, "name": "a"}, {"value": 1, "name": "b"}], "_at": "b"})
    assert C.changes({"x": None}, {"x": 0})[0]["kind"] == "supplement"


def test_legacy_upgrade_backup_and_history_grouping(state, settings):
    # 模拟尚未升级的历史库，当前档案标题不应出现在历史旧版本。
    with db.connect(settings.paths.db) as conn:
        db.upsert_dim_listings(conn, [{"listing_id": ID, "game_id": 10026, "title": "最新标题"}])
        for date, price in [(dt.datetime(2026, 10, 1, 9), 400), (dt.datetime(2026, 10, 2, 9), 300)]:
            db.write_snapshot_round(conn, date, [{"listing_id": ID, "price_yuan": price, "level": 80}])
    result = G.handle_listings(state, {"mode": "history"})
    assert result["total"] == 1 and result["rows"][0]["versions"] == 2
    assert G.handle_listings(state, {})["total"] == 2
    assert list((settings.paths.db.parent / "backups").glob("collection-upgrade-*.duckdb"))
    with db.connect(settings.paths.db, read_only=True) as conn:
        h = C.history(conn, ID, 1, 1)
        assert h["total"] == 2 and len(h["rows"]) == 1
        assert h["rows"][0]["data"]["title"] is None
    C.upgrade(settings)
    assert len(list((settings.paths.db.parent / "backups").glob("collection-upgrade-*.duckdb"))) == 1


def test_detail_revision_and_missing_does_not_erase_known(state):
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    url = f"https://www.pxb7.com/product/{ID}/1"
    detail = {"listing_id": ID, "url": url, "html": '<h1>80级，0个五星角色，0个五星武器</h1><div>25人已收藏</div>'}
    ack = _post(state, "/ingest/detail", detail)[1]
    assert ack["complete"]
    repeat = _post(state, "/ingest/detail", detail)[1]
    assert repeat["revision_id"] == ack["revision_id"]
    _post(state, "/ingest/detail", {**detail, "html": '<h1>80级，0个五星角色，0个五星武器</h1>'})
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        data = C.current_data(conn, ID)
        assert data["favorites_cnt"] == 25
        assert C.history(conn, ID)["rows"][0]["revision_id"] == ack["revision_id"]


def test_delete_includes_history_and_retry_state(state):
    first = _post(state, "/ingest/cards", CARDS_PAYLOAD)[1]
    _post(state, "/collection/retry", {"id": ID, "token": "delete-test", "action": "claim", "snapshot_round": first["round"]})
    result = B.delete(state, {"items": [{"id": ID, "snapshot_round": first["round"]}], "confirm_all_rounds": True})
    assert result["counts"]["fct_listing_revision"] == 1
    assert result["counts"]["collection_listing_state"] == 1
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        assert C.history(conn, ID)["total"] == 0
        assert C.history(conn, OTHER)["total"] == 1
    assert next(d for d in check(state)["items"] if d["id"] == ID)["kind"] == "new"


def test_upgrade_backup_failure_leaves_schema_unchanged(state, monkeypatch):
    with db.connect(state.settings.paths.db) as conn:
        db.upsert_dim_listings(conn, [{"listing_id": ID, "game_id": 10026}])
        db.write_snapshot_round(conn, dt.datetime(2026, 10, 1), [{"listing_id": ID, "price_yuan": 300}])
    def fail(*args):
        raise OSError("backup-denied")
    monkeypatch.setattr(C.shutil, "copy2", fail)
    with pytest.raises(OSError, match="backup-denied"):
        C.upgrade(state.settings)
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        assert "fct_listing_revision" not in {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 1


def test_http_collection_and_history_routes_origins_and_pagination(state):
    import threading
    import urllib.request
    import urllib.error
    handler = type("Handler", (G.GatewayHandler,), {"state": state})
    server = G.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def request(path, payload=None, origin="https://www.pxb7.com"):
        req = urllib.request.Request(base + path,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Origin": origin, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
    try:
        assert request("/collection/check", CARDS_PAYLOAD)[1]["counts"]["new"] == 2
        assert request("/collection/check", CARDS_PAYLOAD, "https://evil.example")[0] == 403
        assert request("/ingest/cards", {**CARDS_PAYLOAD, "selected_ids": [ID]})[1]["accepted_ids"] == [ID]
        assert request(f"/listings/history?id={ID}&limit=1")[1]["total"] == 1
        assert request(f"/listings/history?id={ID}", origin="https://evil.example")[0] == 403
        assert request("/listings/history?id=invalid")[0] == 400
        assert request("/collection/check", {**CARDS_PAYLOAD, "html": "访问验证 请按住滑块，拖动到最右边"})[0] == 422
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
