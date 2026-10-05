import csv
from pathlib import Path

from pxb7 import analysis as A, db
from tests.test_gateway import settings, state, _post, CARDS_PAYLOAD, DETAIL_PAYLOAD
from tests.test_analysis import seeded


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_cards_and_detail_refresh_same_file(state):
    directory = state.settings.project_root / "data/analysis/by_game"
    directory.mkdir(parents=True)
    original = directory / "pxb7-listings-20261003-原神-10026.csv"
    original.write_text("old table", encoding="utf-8")
    _, ack = _post(state, "/ingest/cards", CARDS_PAYLOAD)
    result = ack["analysis_sync"]
    assert result["ok"] and result["rows"] == 2
    path = Path(result["path"])
    assert path.parent == state.settings.project_root / "data/analysis/by_game"
    assert path == original
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    _, ack = _post(state, "/ingest/cards", {
        **CARDS_PAYLOAD, "html": CARDS_PAYLOAD["html"].replace('price="30000"', 'price="42000"')})
    assert ack["analysis_sync"]["path"] == str(path)
    rows = read_rows(path)
    assert len(rows) == 2
    first = next(r for r in rows if r["listing_id"] == "2428022628844555568")
    assert float(first["price_yuan"]) == 420
    _, ack = _post(state, "/ingest/detail", DETAIL_PAYLOAD)
    assert ack["analysis_sync"]["ok"]
    first = next(r for r in read_rows(path) if r["listing_id"] == "2428022628844555568")
    assert first["favorites_cnt"] == "56"
    assert list(directory.glob("*.csv")) == [original]


def test_curated_game_isolation_and_atomic_failure(seeded, monkeypatch):
    out = seeded.project_root / "data/analysis/by_game"
    out.mkdir(parents=True)
    other = out / "pxb7-listings-原神-10026.csv"
    other.write_bytes(b"preserve other game")
    original = out / "pxb7-listings-20261005-鸣潮-10302-看板列.csv"
    original.write_text("old table", encoding="utf-8")
    with db.connect(seeded.paths.db) as conn:
        result = A.sync_game_csv(seeded, conn, game_id=10302)
        assert result["ok"] and result["rows"] == 1
        path = Path(result["path"])
        assert path == original
        rows = read_rows(path)
        assert tuple(rows[0]) == A.WUWA_CURATED_HEADERS
        assert other.read_bytes() == b"preserve other game"
        before = path.read_bytes()
        def locked(*args, **kwargs):
            raise PermissionError("file in use")
        monkeypatch.setattr(Path, "replace", locked)
        failed = A.sync_game_csv(seeded, conn, game_id=10302)
        assert not failed["ok"] and "PermissionError" in failed["error"]
        assert path.read_bytes() == before
        assert not list(out.glob(".sync-*"))


def test_failed_export_keeps_ingest_success(state, monkeypatch):
    def locked(*args, **kwargs):
        raise PermissionError("file in use")
    monkeypatch.setattr(Path, "replace", locked)
    status, ack = _post(state, "/ingest/cards", CARDS_PAYLOAD)
    assert status == 200 and ack["ok"] and ack["snapshots_inserted"] == 2
    assert not ack["analysis_sync"]["ok"]
    monkeypatch.undo()
    _, ack = _post(state, "/ingest/cards", CARDS_PAYLOAD)
    assert ack["analysis_sync"]["ok"] and ack["analysis_sync"]["rows"] == 2
