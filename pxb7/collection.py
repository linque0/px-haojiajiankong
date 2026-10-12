"""本地采集判定、一次重试台账与不可变账号修订；不请求站点。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import db, backfill, parser as P

ID_RE = re.compile(r"\d{6,24}")
META_FIELDS = {"snapshot_at", "parser_version", "collected_via"}
SNAPSHOT_SELECT = ",".join("s." + name for name in db.SNAPSHOT_COLUMNS)


def normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return normalize(float(value))
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in sorted(value.items())
                if not k.startswith("_")}
    if isinstance(value, (tuple, list)):
        return sorted((normalize(v) for v in value), key=encode)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(encode(normalize(value)).encode("utf-8")).hexdigest()


def ensure_schema(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS fct_listing_revision ("
                 "revision_id VARCHAR PRIMARY KEY, listing_id VARCHAR NOT NULL,"
                 "version INTEGER NOT NULL, captured_at TIMESTAMP NOT NULL,"
                 "source VARCHAR NOT NULL, previous_id VARCHAR, data_json VARCHAR NOT NULL,"
                 "diff_json VARCHAR NOT NULL, fingerprint VARCHAR NOT NULL,"
                 "UNIQUE(listing_id, version));"
                 "CREATE TABLE IF NOT EXISTS collection_listing_state ("
                 "listing_id VARCHAR PRIMARY KEY, list_baseline VARCHAR,"
                 "retry_used BOOLEAN NOT NULL DEFAULT FALSE,"
                 "retry_token VARCHAR, retry_status VARCHAR, retry_round TIMESTAMP);"
                 "CREATE TABLE IF NOT EXISTS collection_meta (key VARCHAR PRIMARY KEY, value VARCHAR);")
    if conn.execute("SELECT value FROM collection_meta WHERE key='history-baseline-v1'").fetchone():
        return
    conn.execute("BEGIN TRANSACTION")
    try:
        rows = conn.execute("SELECT " + SNAPSHOT_SELECT + ",l.game_id FROM fct_listing_snapshot s"
                            " LEFT JOIN dim_listing l USING(listing_id)"
                            " ORDER BY s.snapshot_at, s.listing_id").fetchall()
        for row in rows:
            values = dict(zip((*db.SNAPSHOT_COLUMNS, "game_id"), row))
            # dim_listing.title 已被覆盖，不把今天的标题伪装成历史标题。
            values["title"] = None
            append_revision(conn, values["listing_id"], values, "legacy-baseline",
                            values["snapshot_at"])
        conn.execute("INSERT INTO collection_meta VALUES ('history-baseline-v1', '1')")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def upgrade(settings) -> None:
    """旧库先 checkpoint 并备份，随后幂等升级；备份失败不改库。调用者持有 run_lock。"""
    path = Path(settings.paths.db)
    with db.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        if "collection_meta" in tables and conn.execute(
                "SELECT value FROM collection_meta WHERE key='history-baseline-v1'").fetchone():
            return
        has_data = bool(conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0])
        conn.execute("CHECKPOINT")
    if has_data:
        target = path.parent / "backups" / ("collection-upgrade-" +
                 dt.datetime.now().strftime("%Y%m%dT%H%M%S%f") + ".duckdb")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    with db.connect(path) as conn:
        ensure_schema(conn)


def current_data(conn, listing_id: str, snapshot_at=None) -> dict | None:
    row = conn.execute("SELECT " + SNAPSHOT_SELECT + ",l.game_id,l.title FROM fct_listing_snapshot s"
                       " LEFT JOIN dim_listing l USING(listing_id) WHERE s.listing_id=?"
                       + (" AND s.snapshot_at=?" if snapshot_at is not None else "")
                       + " ORDER BY s.snapshot_at DESC LIMIT 1",
                       [listing_id] + ([snapshot_at] if snapshot_at is not None else [])).fetchone()
    return dict(zip((*db.SNAPSHOT_COLUMNS, "game_id", "title"), row)) if row else None


def public_data(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k not in META_FIELDS}
    out["extracted_features"] = backfill.features(out.get("extracted_features"))
    if isinstance(out.get("featured_chars"), str):
        try:
            out["featured_chars"] = json.loads(out["featured_chars"])
        except ValueError:
            pass
    return normalize(out)


def changes(old: dict, new: dict, prefix="") -> list[dict]:
    out = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        field = prefix + key
        if isinstance(before, dict) and isinstance(after, dict):
            out.extend(changes(before, after, field + "."))
        elif before != after:
            out.append({"field": field, "before": before, "after": after,
                        "kind": "supplement" if before is None else "change"})
    return out


def append_revision(conn, listing_id: str, data: dict, source: str, captured_at=None) -> str:
    data = public_data(data)
    latest = conn.execute("SELECT revision_id,version,data_json,fingerprint"
                          " FROM fct_listing_revision WHERE listing_id=?"
                          " ORDER BY version DESC LIMIT 1", [listing_id]).fetchone()
    fingerprint = digest(data)
    if latest and latest[3] == fingerprint:
        return latest[0]
    version = latest[1] + 1 if latest else 1
    revision = f"{listing_id}:{version}"
    diff = changes(json.loads(latest[2]), data) if latest else []
    conn.execute("INSERT INTO fct_listing_revision VALUES (?,?,?,?,?,?,?,?,?)",
                 [revision, listing_id, version, captured_at or dt.datetime.now(), source,
                  latest[0] if latest else None, encode(data), encode(diff), fingerprint])
    return revision


def record(conn, listing_id: str, source: str, snapshot_at=None) -> str | None:
    data = current_data(conn, listing_id, snapshot_at)
    return append_revision(conn, listing_id, data, source) if data else None


def list_values(card) -> dict:
    # 只用公开列表来源，不与详情字段/补采全文比较。
    title = getattr(card, "list_title", card.title)
    values = {"title": title, **{k: v for k, v in card.fields.items()
              if k not in ("favorites_cnt", "viewers_masked")}}
    return normalize({k: v for k, v in values.items() if v is not None and v != ""})


def parse_candidates(html: str, url: str):
    # 清除扩展全文标记；列表基线独立于已补采全文。
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("[data-pxb7-full-title]"):
        node.attrs.pop("data-pxb7-full-title", None)
        node.attrs.pop("data-pxb7-full-title-id", None)
    parsed = P.parse_list_page(str(soup), url=url)
    titles = {}
    for node in soup.select("[productid], [data-listing-id]"):
        title_node = node.select_one("[productname]")
        if title_node:
            titles[node.get("productid") or node.get("data-listing-id")] = title_node.get("productname")
    for card in parsed.cards:
        card.list_title = titles.get(card.listing_id, card.title)
    return parsed


def decision(conn, card, *, existing: dict | None = None, game_id=None) -> dict:
    listing_id = card.listing_id
    old = existing if existing is not None else current_data(conn, listing_id)
    state = conn.execute("SELECT list_baseline,retry_used,retry_token,retry_status,retry_round"
                         " FROM collection_listing_state WHERE listing_id=?", [listing_id]).fetchone()
    latest = conn.execute("SELECT revision_id FROM fct_listing_revision WHERE listing_id=?"
                          " ORDER BY version DESC LIMIT 1", [listing_id]).fetchone()
    observed = list_values(card)
    reason, kind = "首次采集", "new"
    missing = []
    if old:
        before = json.loads(state[0]) if state and state[0] else None
        # 旧库没有列表来源基线：只比较可靠的挂牌价；不把全文差异误报为更新。
        changed = (any(observed[k] != before.get(k) for k in observed)
                   if before is not None else old.get("price_yuan") is not None
                   and observed.get("price_yuan") is not None
                   and old["price_yuan"] != observed["price_yuan"])
        missing = backfill.missing({**old, "features": backfill.features(old.get("extracted_features"))})
        if changed:
            kind, reason = "changed", "列表公开信息变化"
        elif missing:
            kind = "manual" if state and state[1] else "retry"
            reason = "自动重试已使用，待查缺补录" if kind == "manual" else "已有数据不完整，可自动重试一次"
        else:
            kind, reason = "skip", "完整账号未变化"
        if old.get("game_id") is None:
            kind, reason = "manual", "已有快照缺少游戏档案，待人工补录"
        elif game_id is not None and old["game_id"] != game_id:
            kind, reason = "skip", "商品ID已属于其他游戏，跳过不匹配页面"
    return {"id": listing_id, "kind": kind, "reason": reason, "missing": missing,
            "revision_id": latest[0] if latest else None,
            "retry_token": state[2] if state else None,
            "snapshot_round": old["snapshot_at"].isoformat() if old else None,
            "baseline": observed}


def save_baseline(conn, listing_id: str, observed: dict) -> None:
    old = conn.execute("SELECT list_baseline FROM collection_listing_state WHERE listing_id=?",
                       [listing_id]).fetchone()
    baseline = json.loads(old[0]) if old and old[0] else {}
    baseline.update({k: v for k, v in observed.items() if v is not None})
    conn.execute("INSERT INTO collection_listing_state (listing_id,list_baseline) VALUES (?,?)"
                 " ON CONFLICT(listing_id) DO UPDATE SET list_baseline=excluded.list_baseline",
                 [listing_id, encode(baseline)])


def preserve(conn, listing) -> None:
    """列表缺字段不降级详情；明确数量/名单变化则移除不适用的旧明细。"""
    old = current_data(conn, listing.listing_id)
    if not old:
        return
    feats = backfill.features(old.get("extracted_features"))
    incoming = listing.extraction.features
    for count, roster, scalar in backfill.ROSTERS:
        number = listing.card.fields.get(count)
        if number is not None and number != old.get(count) or roster in incoming or number == 0:
            feats.pop(roster, None)
            feats.pop(scalar, None)
    for key in db.SNAPSHOT_COLUMNS:
        if key not in META_FIELDS | {"listing_id", "extracted_features", "viewers_masked", "favorites_cnt"}:
            if listing.card.fields.get(key) is None and old.get(key) is not None:
                listing.card.fields[key] = old[key]
    feats = merge_known(feats, incoming)
    listing.extraction.features = feats
    if old.get("title") and not incoming.get("_list_full_title"):
        listing.title = old["title"]
    if listing.viewers_masked is None:
        listing.viewers_masked = old.get("viewers_masked")
    if listing.favorites_cnt is None:
        listing.favorites_cnt = old.get("favorites_cnt")


def merge_known(old: dict, incoming: dict) -> dict:
    """未知字段不清空已有值；确实公示的零、False 和空名单均为有效值。"""
    result = dict(old)
    for key, value in incoming.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_known(result[key], value)
        else:
            result[key] = value
    return result


def complete_retry_state(conn, listing_id: str) -> list:
    rows = backfill.latest_rows(conn, listing_id)
    missing = backfill.missing(rows[0]) if rows else [{"reason": "尚无快照"}]
    if not missing:
        conn.execute("UPDATE collection_listing_state SET retry_used=FALSE,retry_token=NULL,"
                     "retry_status=NULL,retry_round=NULL WHERE listing_id=?", [listing_id])
    return missing


def claim_retry(conn, listing_id: str, token: str, snapshot_round) -> bool:
    old = conn.execute("SELECT retry_used,retry_token,retry_status FROM collection_listing_state"
                       " WHERE listing_id=?", [listing_id]).fetchone()
    if old and old[0]:
        return old[1] == token and old[2] == "running"
    conn.execute("INSERT INTO collection_listing_state (listing_id,retry_used,retry_token,retry_status,retry_round)"
                 " VALUES (?,TRUE,?,'running',?) ON CONFLICT(listing_id) DO UPDATE SET"
                 " retry_used=TRUE,retry_token=excluded.retry_token,retry_status='running',retry_round=excluded.retry_round",
                 [listing_id, token, snapshot_round])
    return True


def finish_retry(conn, listing_id: str, token: str, status: str) -> None:
    matched = conn.execute("UPDATE collection_listing_state SET retry_status=? WHERE listing_id=? AND retry_token=?"
                           " RETURNING listing_id", [status, listing_id, token]).fetchone()
    if matched:
        complete_retry_state(conn, listing_id)


def history(conn, listing_id: str, offset=0, limit=15) -> dict:
    if not isinstance(listing_id, str) or not ID_RE.fullmatch(listing_id):
        raise ValueError("invalid-listing-id")
    offset, limit = max(0, int(offset)), max(1, min(100, int(limit)))
    total = conn.execute("SELECT count(*) FROM fct_listing_revision WHERE listing_id=?", [listing_id]).fetchone()[0]
    rows = conn.execute("SELECT revision_id,version,captured_at,source,previous_id,data_json,diff_json"
                        " FROM fct_listing_revision WHERE listing_id=? ORDER BY version DESC LIMIT ? OFFSET ?",
                        [listing_id, limit, offset]).fetchall()
    return {"ok": True, "listing_id": listing_id, "total": total, "offset": offset, "limit": limit,
            "rows": [{"revision_id": r[0], "version": r[1], "captured_at": r[2].isoformat(),
                      "source": r[3], "previous_id": r[4], "data": json.loads(r[5]),
                      "diff": json.loads(r[6])} for r in rows]}
