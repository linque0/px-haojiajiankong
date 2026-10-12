"""仅临时库：删除选中缺失商品全部轮次，关联清理、备份与 CSV 重试。"""
import csv
import datetime as dt
from pathlib import Path

import pytest

from pxb7 import backfill as B, db
from tests.test_gateway import settings, state
from tests.test_backfill import seed


def selection(state):
    return {"items": B.candidates(state, {})["rows"], "confirm_all_rounds": True}


def test_delete_all_rounds_backup_related_and_other_listing_untouched(state, monkeypatch):
    seed(state)
    payload = selection(state)
    first, other = payload["items"]
    payload["items"] = [first]
    with db.connect(state.settings.paths.db) as conn:
        original_other = conn.execute("SELECT * FROM fct_listing_snapshot WHERE listing_id=?", [other["id"]]).fetchall()
        conn.execute("INSERT INTO fct_listing_snapshot SELECT * REPLACE (? AS snapshot_at) "
                     "FROM fct_listing_snapshot WHERE listing_id=?",
                     [dt.datetime.fromisoformat(first["snapshot_round"]) - dt.timedelta(hours=1), first["id"]])
        conn.execute("INSERT INTO fct_price_change (price_change_id,listing_id,ts,old_price,new_price,pct) VALUES (?,?,?,?,?,?)",
                     ["delete-test", first["id"], dt.datetime.now(), 100, 90, -0.1])
        conn.execute("INSERT INTO fct_delist_event VALUES (?,?,?,?,?)",
                     [first["id"], dt.datetime.now(), dt.datetime.now(), 0, 90])
        # 即使此前物化表没有该号，也应清理人为滞留的关联分析行。
        conn.execute("INSERT INTO keyword_feature_matrix BY NAME SELECT ? AS listing_id, ? AS snapshot_at, 10026 AS game_id",
                     [first["id"], dt.datetime.fromisoformat(first["snapshot_round"])])
    state.pending_details[first["id"]] = {"listing_id": first["id"]}
    monkeypatch.setattr(state, "match_task", lambda game_id: None)
    result = B.delete(state, payload)
    assert result["deleted"] == 1 and result["counts"]["fct_listing_snapshot"] == 2
    assert result["counts"]["fct_price_change"] == 1 and result["results"][0]["ok"]
    assert first["id"] not in state.pending_details
    with db.connect(result["backup"], read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM fct_listing_snapshot WHERE listing_id=?", [first["id"]]).fetchone()[0] == 2
    with db.connect(state.settings.paths.db, read_only=True) as conn:
        for table in result["counts"]:
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE listing_id=?", [first["id"]]).fetchone()[0] == 0
        assert original_other == conn.execute("SELECT * FROM fct_listing_snapshot WHERE listing_id=?", [other["id"]]).fetchall()
    with Path(result["results"][0]["path"]).open(encoding="utf-8-sig", newline="") as stream:
        assert {r["listing_id"] for r in csv.DictReader(stream)} == {other["id"]}


def test_delete_last_game_preserves_original_csv_and_retry(state, monkeypatch):
    seed(state)
    original = B.sync(state, {"game_ids": [10026]})["results"][0]
    path = Path(original["path"])
    dated = path.with_name("pxb7-listings-20261003-原神-10026.csv")
    path.rename(dated)
    before = dated.read_bytes()
    def locked(*args):
        raise PermissionError("CSV locked")
    monkeypatch.setattr(Path, "replace", locked)
    result = B.delete(state, selection(state))
    assert result["deleted"] == 2 and not result["results"][0]["ok"]
    assert result["results"][0]["path"] == str(dated) and dated.read_bytes() == before
    assert B.candidates(state, {})["total"] == 0
    monkeypatch.undo()
    synced = B.sync(state, {"game_ids": [10026]})["results"][0]
    assert synced["ok"] and synced["rows"] == 0 and synced["path"] == str(dated)
    assert dated.read_bytes().startswith(b'\xef\xbb\xbf')
    with dated.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        assert "listing_id" in reader.fieldnames and list(reader) == []
    assert len(list(dated.parent.glob("*.csv"))) == 1


def test_delete_rejects_stale_complete_unconfirmed_duplicate_and_empty(state):
    seed(state)
    payload = selection(state)
    with pytest.raises(ValueError, match="confirmation"):
        B.delete(state, {**payload, "confirm_all_rounds": False})
    for items in ([], payload["items"] * 101, [payload["items"][0]] * 2, [None]):
        with pytest.raises(ValueError):
            B.delete(state, {**payload, "items": items})
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET snapshot_at=snapshot_at+INTERVAL 1 HOUR WHERE listing_id=?",
                     [payload["items"][0]["id"]])
    with pytest.raises(ValueError, match="selection-changed"):
        B.delete(state, payload)
    # 已经补齐的商品，即使仍被旧弹窗勾选也不得删除。
    payload = selection(state)
    with db.connect(state.settings.paths.db) as conn:
        conn.execute("UPDATE fct_listing_snapshot SET level=80,yellow_cnt=0,five_star_chars=0,five_star_weapons=0")
    with pytest.raises(ValueError, match="selection-changed"):
        B.delete(state, payload)
    assert not list((state.settings.paths.db.parent / "backups").glob("*.duckdb"))


def test_delete_backup_failure_and_transaction_failure_keep_data(state, monkeypatch):
    seed(state)
    payload = selection(state)
    def fail(*args):
        raise PermissionError("backup locked")
    monkeypatch.setattr(B.shutil, "copy2", fail)
    with pytest.raises(PermissionError):
        B.delete(state, payload)
    assert B.candidates(state, {})["total"] == 2
    monkeypatch.undo()
    original_connect = db.connect
    class FailingConnection:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, key):
            return getattr(self.conn, key)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.conn.close()
        def execute(self, sql, *args):
            if sql.startswith("DELETE FROM dim_listing"):
                raise RuntimeError("failed final delete")
            return self.conn.execute(sql, *args)
    monkeypatch.setattr(db, "connect", lambda *a, **kw: FailingConnection(original_connect(*a, **kw)))
    with pytest.raises(RuntimeError, match="final delete"):
        B.delete(state, payload)
    assert B.candidates(state, {})["total"] == 2
    assert len(list((state.settings.paths.db.parent / "backups").glob("*.duckdb"))) == 1
