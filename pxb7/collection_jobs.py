"""扩展逻辑批次与真实观察。固定本地协议，不请求网站、不存认证数据。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import collection as C, db, pipeline as PL

VERSION = 1
BLOCK_SIZE = 200
MAX_TARGET = 10000
PURPOSES = ("discover", "price_refresh", "detail_enrich")


def now():
    return dt.datetime.now(dt.timezone.utc)


def ensure(settings):
    """调用者持有run_lock。已有数据先checkpoint/备份；旧事实及时间口径不变。"""
    with db.connect(settings.paths.db) as conn:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        if "collection_batch" in tables:
            return
        has_data=bool(conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0])
        if has_data:
            conn.execute("CHECKPOINT")
    # Windows复制数据库文件前必须关闭连接；run_lock仍由调用者持有。
    if has_data:
        backup = Path(settings.paths.db).parent / "backups" / (
            "collection-batch-v1-" + now().strftime("%Y%m%dT%H%M%S%f") + ".duckdb")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings.paths.db, backup)
    with db.connect(settings.paths.db) as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("""
                CREATE TABLE collection_batch (
                  batch_id VARCHAR PRIMARY KEY, task_id VARCHAR NOT NULL, game_id INTEGER NOT NULL,
                  source_url VARCHAR NOT NULL, claim_epoch VARCHAR NOT NULL, config_json VARCHAR NOT NULL,
                  status VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
                  stop_reason VARCHAR, accesses INTEGER DEFAULT 0, checkpoint_json VARCHAR, handoff_json VARCHAR,
                  heartbeat_at TIMESTAMPTZ);
                CREATE TABLE monitor_job (
                  job_id VARCHAR PRIMARY KEY, task_id VARCHAR, slot BIGINT, due_at TIMESTAMPTZ,
                  status VARCHAR, batch_id VARCHAR, reason VARCHAR, spec_json VARCHAR, coalesced_slots INTEGER,
                  UNIQUE(task_id,slot));
                CREATE TABLE collection_access (
                  permit_id VARCHAR PRIMARY KEY, batch_id VARCHAR, action VARCHAR, listing_id VARCHAR,
                  issued_at TIMESTAMPTZ NOT NULL);
                CREATE TABLE collection_scanned (
                  batch_id VARCHAR,listing_id VARCHAR,kind VARCHAR,PRIMARY KEY(batch_id,listing_id));
                CREATE TABLE collection_batch_item (
                  batch_id VARCHAR, listing_id VARCHAR, kind VARCHAR, snapshot_round TIMESTAMP,
                  price_confirmed BOOLEAN DEFAULT FALSE, text_complete BOOLEAN DEFAULT FALSE,
                  complete BOOLEAN DEFAULT FALSE, missing_json VARCHAR, PRIMARY KEY(batch_id,listing_id));
                CREATE TABLE collection_observation (
                  batch_id VARCHAR, listing_id VARCHAR, observation_kind VARCHAR, observed_at TIMESTAMPTZ,
                  data_json VARCHAR, PRIMARY KEY(batch_id,listing_id,observation_kind));
                CREATE TABLE listing_text_cache (
                  game_id INTEGER, listing_id VARCHAR, product_code VARCHAR, title VARCHAR,
                  source VARCHAR, acquired_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ,
                  content_hash VARCHAR, quality_version INTEGER, PRIMARY KEY(game_id,listing_id));
                CREATE TABLE collection_export_state (
                  game_id INTEGER PRIMARY KEY, dirty_generation BIGINT DEFAULT 0,
                  exported_generation BIGINT DEFAULT 0, last_export_at TIMESTAMPTZ, error VARCHAR);
                CREATE TABLE collection_site_hold (
                  site VARCHAR PRIMARY KEY, reason VARCHAR, retry_after_until TIMESTAMPTZ,
                  created_at TIMESTAMPTZ, batch_id VARCHAR);
            """)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def positive(value, name, maximum, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("invalid-" + name)
    return value


def create(state, payload, *, override=None):
    url = payload.get("url")
    parts = urlsplit(url if isinstance(url, str) else "")
    match = re.fullmatch(r"/buy/(\d+)(?:/(\d+))?/?", parts.path)
    if parts.scheme != "https" or parts.netloc != "www.pxb7.com" or not match:
        raise ValueError("invalid-source-url")
    if any(word in parts.query.lower() for word in ("token", "cookie", "authorization", "captcha", "password", "timestamp__")):
        raise ValueError("source-url-has-private-parameters")
    task = state.match_task(int(match[1]), int(match[2] or 1))
    if task is None:
        raise ValueError("target-not-selected")
    config = {**state.config, **(override or {})}
    purpose = payload.get("purpose", config.get("purpose", "discover"))
    if purpose not in PURPOSES:
        raise ValueError("invalid-purpose")
    target_value = payload.get("target_count", config.get("target_count", 0))
    if type(target_value) is int and target_value == 0:
        target_value = config["cards_target"]
    target = positive(target_value,
                      "target-count", MAX_TARGET)
    mode = payload.get("collection_mode", config["collection_mode"])
    if mode not in ("list", "detail", "auto"):
        raise ValueError("invalid-collection-mode")
    ids = payload.get("ids",config.get("selected_listing_ids",[]))
    if not isinstance(ids, list) or len(ids) > MAX_TARGET or any(
            not isinstance(i, str) or not C.ID_RE.fullmatch(i) for i in ids):
        raise ValueError("invalid-selected-ids")
    frozen = {**config, "purpose": purpose, "target_count": target, "collection_mode": mode,
              "ids": list(dict.fromkeys(ids)), "block_size": BLOCK_SIZE,
              "scan_budget": positive(payload.get("scan_budget", config.get("scan_budget", 3200)),
                                      "scan-budget", 100000),
              "max_scan_rounds": positive(payload.get("max_scan_rounds", config.get("max_scan_rounds", 12)),
                                          "max-scan-rounds", 1000),
              "daily_access_budget": positive(config.get("daily_access_budget", 1000), "access-budget", 100000)}
    batch_id, epoch = uuid.uuid4().hex, uuid.uuid4().hex
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
        if hold(conn):
            raise ValueError("site-access-held")
        reconcile_expired(conn)
        active = conn.execute("SELECT batch_id FROM collection_batch WHERE status IN ('running','waiting_verification','needs_reconcile')").fetchone()
        if active:
            raise ValueError("batch-busy")
        conn.execute("INSERT INTO collection_batch (batch_id,task_id,game_id,source_url,claim_epoch,config_json,status,created_at)"
                     " VALUES (?,?,?,?,?,?,'running',?)",
                     [batch_id, task.task_id, task.game_id, url, epoch, C.encode(frozen), now()])
        conn.execute("UPDATE collection_batch SET heartbeat_at=? WHERE batch_id=?",[now(),batch_id])
    return {"ok": True, "protocol_version": VERSION, "batch_id": batch_id, "claim_epoch": epoch,
            "config": frozen, "task_id": task.task_id, "game_id": task.game_id}


def get(conn, payload, *, terminal=False):
    if type(payload.get("protocol_version")) is not int or payload.get("protocol_version") != VERSION:
        raise ValueError("unsupported-collection-protocol")
    if not isinstance(payload.get("batch_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", payload["batch_id"]):
        raise ValueError("invalid-batch-identity")
    row = conn.execute("SELECT task_id,game_id,source_url,claim_epoch,config_json,status FROM collection_batch WHERE batch_id=?",
                       [payload.get("batch_id")]).fetchone()
    if not row or payload.get("claim_epoch") != row[3]:
        raise ValueError("invalid-batch-identity")
    if not terminal and row[5] != "running":
        raise ValueError("batch-not-running")
    return dict(task_id=row[0], game_id=row[1], url=row[2], config=json.loads(row[4]), status=row[5])


def validate_ingest(state, payload, *, detail=False, scan=False):
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
        batch = get(conn, payload)
        if state.task_by_id(batch["task_id"]) is None:
            raise ValueError("target-not-selected")
        if detail:
            if not isinstance(payload.get("listing_id"), str) or not C.ID_RE.fullmatch(payload["listing_id"]):
                raise ValueError("invalid-item-id")
            row = conn.execute("SELECT snapshot_round FROM collection_batch_item WHERE batch_id=? AND listing_id=?",
                               [payload["batch_id"], payload.get("listing_id")]).fetchone()
            if not row or row[0] is None or payload.get("snapshot_round") != row[0].isoformat(timespec="seconds"):
                raise ValueError("batch-item-round-mismatch")
            if not re.fullmatch(r"https://www\.pxb7\.com/product/" + re.escape(payload["listing_id"]) + r"/\d+/?", payload.get("url", "")):
                raise ValueError("batch-item-url-mismatch")
            old=C.current_data(conn,payload["listing_id"])
            if not old or old.get("game_id") != batch["game_id"]:
                raise ValueError("batch-item-game-mismatch")
            if old.get("snapshot_at")!=row[0]:
                raise ValueError("latest-snapshot-changed")
        elif payload.get("url") != batch["url"]:
            raise ValueError("batch-source-mismatch")
        ids = payload.get("selected_ids")
        if not detail and (not isinstance(ids, list) or len(ids) > BLOCK_SIZE or any(
                not isinstance(i, str) or not C.ID_RE.fullmatch(i) for i in ids)):
            raise ValueError("invalid-execution-block")
        if not scan and ids is not None and batch["config"]["ids"] and not set(ids)<=set(batch["config"]["ids"]):
            raise ValueError("batch-selected-scope-mismatch")
    return batch


def register(conn, payload, decisions):
    if not payload.get("batch_id"):
        return
    for item in decisions:
        conn.execute("INSERT INTO collection_batch_item (batch_id,listing_id,kind,snapshot_round,missing_json)"
                     " VALUES (?,?,?,?,?) ON CONFLICT(batch_id,listing_id) DO UPDATE SET"
                     " snapshot_round=excluded.snapshot_round,missing_json=excluded.missing_json",
                     [payload["batch_id"], item["id"], item["kind"], item.get("snapshot_round"), C.encode(item.get("missing", []))])


def quality(conn, listing_id, config, missing):
    saved=C.current_data(conn,listing_id)
    feats=json.loads(saved.get("extracted_features") or "{}") if saved else {}
    proof=feats.get("_public_text",{})
    states=proof.get("field_states",{})
    public_missing=[m for m in missing if states.get(m.get("key"))!="unreported"]
    if saved and saved.get("game_id")==10302:
        try:
            fresh=now()-dt.datetime.fromisoformat(proof["acquired_at"])<dt.timedelta(minutes=config.get("text_cache_ttl_minutes",10))
        except (KeyError,ValueError,TypeError):
            fresh=False
        if not fresh:
            public_missing.append({"key":"public_detail_text","reason":"详情独有文字未核验或已过期"})
    return public_missing,not missing and bool(saved) and all(saved.get(k) is not None for k in ("level","yellow_cnt","five_star_chars","five_star_weapons"))


def observe(conn, payload, listing_id, kind, data, *, acquired_at=None):
    if not payload.get("batch_id"):
        return
    conn.execute("INSERT INTO collection_observation VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                 [payload["batch_id"], listing_id, kind, acquired_at or now(), C.encode(data)])
    if kind == "price":
        conn.execute("UPDATE collection_batch_item SET price_confirmed=TRUE WHERE batch_id=? AND listing_id=?",
                     [payload["batch_id"], listing_id])


def counts(conn, batch_id):
    rows = conn.execute("SELECT kind,price_confirmed,text_complete,complete FROM collection_batch_item WHERE batch_id=?", [batch_id]).fetchall()
    scanned=conn.execute("SELECT count(*) FROM collection_scanned WHERE batch_id=?",[batch_id]).fetchone()[0]
    return {"processed": len(rows), "scanned":scanned,"new": sum(r[0] == "new" and r[1] for r in rows),
            "prices": sum(r[1] for r in rows), "text_complete": sum(r[2] for r in rows),
            "complete": sum(r[3] for r in rows), "pending": sum(not r[3] for r in rows)}


def access(state, payload):
    action = payload.get("action")
    if action not in ("list", "text", "detail"):
        raise ValueError("invalid-access-action")
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
        batch = get(conn, payload)
        if hold(conn):
            return {"ok":False,"error":"site-access-held"}
        # 操作预算以站点会话合计；失败也不归还。页面自发网络请求不在此计数内。
        start = now().astimezone(dt.timezone(dt.timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0)
        used = conn.execute("SELECT count(*) FROM collection_access WHERE issued_at>=?", [start]).fetchone()[0]
        if used >= batch["config"]["daily_access_budget"]:
            return {"ok": False, "error": "access-budget-exhausted"}
        conn.execute("UPDATE collection_batch SET accesses=accesses+1 WHERE batch_id=?", [payload["batch_id"]])
        conn.execute("UPDATE collection_batch SET heartbeat_at=? WHERE batch_id=?",[now(),payload["batch_id"]])
        permit = uuid.uuid4().hex
        conn.execute("INSERT INTO collection_access VALUES (?,?,?,?,?)", [permit, payload["batch_id"], action, payload.get("id"), now()])
        return {"ok": True, "permit_id": permit, "action": action}


def hold(conn):
    row=conn.execute("SELECT reason,CAST(retry_after_until AS VARCHAR) FROM collection_site_hold WHERE site='pxb7'").fetchone()
    if row and row[0]=="rate-limit" and row[1] and dt.datetime.fromisoformat(row[1])<=now():
        conn.execute("DELETE FROM collection_site_hold WHERE site='pxb7'")
        return None
    return {"reason":row[0],"retry_after_until":row[1]} if row else None


def clear_hold(state):
    """明确的本地操作；已知Retry-After未到期时不可提前恢复。"""
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
        current=hold(conn)
        if current and current["reason"]=="rate-limit" and current["retry_after_until"]:
            raise ValueError("retry-after-not-elapsed")
        conn.execute("DELETE FROM collection_site_hold WHERE site='pxb7'")
    return {"ok":True}


def reconcile_expired(conn):
    # 验证等待不受普通执行租约影响；未知页面位置不自动重采。
    conn.execute("UPDATE collection_batch SET status='needs_reconcile',stop_reason='heartbeat-lost'"
        " WHERE status='running' AND heartbeat_at<?",[now()-dt.timedelta(minutes=10)])


def cancel_local(state,payload):
    batch_id=payload.get("batch_id")
    if not isinstance(batch_id,str) or not re.fullmatch(r"[0-9a-f]{32}",batch_id):
        raise ValueError("invalid-batch-id")
    with PL.run_lock(state.settings),db.connect(state.settings.paths.db) as conn:
        reconcile_expired(conn)
        conn.execute("UPDATE collection_batch SET status='cancelled',ended_at=?,stop_reason='reconciled-and-cancelled'"
            " WHERE batch_id=? AND status='needs_reconcile'",[now(),batch_id])
        row=conn.execute("SELECT status FROM collection_batch WHERE batch_id=?",[batch_id]).fetchone()
        if not row or row[0]!="cancelled":
            raise ValueError("batch-not-reconcilable")
        conn.execute("UPDATE monitor_job SET status='cancelled',reason='reconciled-and-cancelled' WHERE batch_id=?",[batch_id])
    return {"ok":True}


DIAGNOSTIC_ERRORS=frozenset(("timeout","fetch-rejected","invalid-json","unexpected-content",
    "business-error","empty-title","auth-required","access-denied","rate-limit","verification",
    "route-change","cancelled","http-error","network-error","title-mismatch","incomplete-title",
    "session-not-ready","bridge-timeout","list-change","access-budget-exhausted","access-permit-timeout"))


def clean_diagnostics(raw):
    if not isinstance(raw,list) or len(raw)>200:
        raise ValueError("invalid-diagnostics")
    result=[]
    for value in raw:
        if not isinstance(value,dict) or not isinstance(value.get("id"),str) or not C.ID_RE.fullmatch(value["id"]):
            raise ValueError("invalid-diagnostic-identity")
        item={"id":value["id"]}
        if value.get("error") in DIAGNOSTIC_ERRORS:
            item["error"]=value["error"]
        for key in ("attempt","duration_ms","status","retry_after_until"):
            number=value.get(key)
            if isinstance(number,(int,float)) and not isinstance(number,bool) and 0<=number<1e15:
                item[key]=number
        for key,choices in {"stage":("fetch","body","validate"),"content_type":("json","html","other","unknown")}.items():
            if value.get(key) in choices:
                item[key]=value[key]
        result.append(item)
    return result


def text_cache(state, payload, *, store=False):
    entries = payload.get("items")
    if not isinstance(entries, list) or len(entries) > BLOCK_SIZE:
        raise ValueError("invalid-cache-items")
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
        batch = get(conn, payload)
        found = {}
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not C.ID_RE.fullmatch(item["id"]):
                raise ValueError("invalid-cache-identity")
            code = item.get("code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", code):
                continue
            row = conn.execute("SELECT product_code,title,source,CAST(acquired_at AS VARCHAR),content_hash,quality_version FROM listing_text_cache WHERE game_id=? AND listing_id=?",
                               [batch["game_id"], item["id"]]).fetchone()
            if row:
                row = (*row[:3],dt.datetime.fromisoformat(row[3]),*row[4:])
            if store:
                title = item.get("title")
                source = item.get("source")
                if not isinstance(title, str) or not 6 <= len(title) <= 50000 or source not in ("list-response", "title-api", "detail"):
                    raise ValueError("invalid-cache-content")
                try:
                    acquired = dt.datetime.fromisoformat(item["acquired_at"])
                    if acquired.tzinfo is None or acquired > now() + dt.timedelta(minutes=5):
                        raise ValueError()
                except (KeyError, ValueError, TypeError):
                    raise ValueError("invalid-cache-time") from None
                saved = C.current_data(conn,item["id"])
                introduction = json.loads(saved.get("extracted_features") or "{}").get("_introduction", {}) if saved else {}
                registered = conn.execute("SELECT text_complete FROM collection_batch_item WHERE batch_id=? AND listing_id=?", [payload["batch_id"],item["id"]]).fetchone()
                if not saved or saved.get("game_id") != batch["game_id"] or saved.get("title") != title \
                        or not registered or not registered[0] or introduction.get("source") != source:
                    raise ValueError("unverified-cache-content")
                if row and acquired <= row[3]:
                    continue
                conn.execute("INSERT INTO listing_text_cache VALUES (?,?,?,?,?,?,?,?,?)"
                             " ON CONFLICT(game_id,listing_id) DO UPDATE SET product_code=excluded.product_code,"
                             "title=excluded.title,source=excluded.source,acquired_at=excluded.acquired_at,"
                             "last_used_at=excluded.last_used_at,content_hash=excluded.content_hash,quality_version=excluded.quality_version",
                             [batch["game_id"], item["id"], code, title, source, acquired, now(), hashlib.sha256(title.encode()).hexdigest(), VERSION])
            elif row and not batch["config"].get("force_text_refresh"):
                ttl = batch["config"].get("text_cache_ttl_minutes", 10)
                prefix = re.sub(r"[^\w]", "", item.get("prefix", "") or "")
                if row[0] == code and row[5] == VERSION and now() - row[3] < dt.timedelta(minutes=ttl) \
                        and len(prefix) >= 24 and re.sub(r"[^\w]", "", row[1]).startswith(prefix):
                    found[item["id"]] = {"title": row[1], "source": row[2], "acquired_at": row[3].isoformat(), "reused": True}
                    conn.execute("UPDATE listing_text_cache SET last_used_at=? WHERE game_id=? AND listing_id=?", [now(), batch["game_id"], item["id"]])
        # 有界缓存：最旧使用项先淘汰，不影响已经入库的数据。
        conn.execute("DELETE FROM listing_text_cache WHERE (game_id,listing_id) IN ("
                     " SELECT game_id,listing_id FROM listing_text_cache ORDER BY last_used_at DESC OFFSET 5000)")
        total = conn.execute("SELECT coalesce(sum(length(title)),0) FROM listing_text_cache").fetchone()[0]
        while total > 20000000:
            oldest = conn.execute("SELECT game_id,listing_id,length(title) FROM listing_text_cache ORDER BY last_used_at LIMIT 1").fetchone()
            conn.execute("DELETE FROM listing_text_cache WHERE game_id=? AND listing_id=?", list(oldest[:2]))
            total -= oldest[2]
        return {"ok": True, "items": found}


def progress(state, payload):
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
        batch = get(conn, payload, terminal=True)
        status = payload.get("status", batch["status"])
        if status not in ("running", "waiting_verification", "done", "partial", "cancelled", "error", "paused", "needs_reconcile"):
            raise ValueError("invalid-batch-status")
        checkpoint = payload.get("checkpoint", {})
        if not isinstance(checkpoint, dict) or len(C.encode(checkpoint)) > 1024000:
            raise ValueError("invalid-checkpoint")
        checkpoint = {k:v for k,v in checkpoint.items() if k in (
            "phase", "processed", "current_id", "scanned", "pending_ids", "events", "diagnostics")}
        checkpoint["diagnostics"]=clean_diagnostics(checkpoint.get("diagnostics",[]))
        events=checkpoint.get("events",[])
        if not isinstance(events,list) or len(events)>200:
            raise ValueError("invalid-events")
        checkpoint["events"]=[{k:v for k,v in e.items() if k in ("type","at","id","stage","waiting_ms","intervention")}
            for e in events if isinstance(e,dict) and e.get("type") in (
                "challenge_detected","input_attempted","input_sent","unsupported","input_failed",
                "challenge_cleared","business_ready","item_committed")]
        # 迟到的终态回执不能重新施加已经显式解除的访问等待。
        if batch["status"] in ("running","waiting_verification"):
            for diagnostic in checkpoint["diagnostics"]:
                reason=diagnostic.get("error")
                if reason in ("rate-limit","auth-required","access-denied","unexpected-content"):
                    until=diagnostic.get("retry_after_until")
                    conn.execute("INSERT INTO collection_site_hold VALUES ('pxb7',?,?,?,?)"
                        " ON CONFLICT(site) DO UPDATE SET reason=excluded.reason,retry_after_until=excluded.retry_after_until,"
                        "created_at=excluded.created_at,batch_id=excluded.batch_id",
                        [reason,dt.datetime.fromtimestamp(until/1000,dt.timezone.utc) if until else None,now(),payload["batch_id"]])
        if batch["status"] == "needs_reconcile" and status == "cancelled":
            pass  # 显式取消允许释放待对账队列，已提交事实保持。
        elif batch["status"] not in ("running", "waiting_verification"):
            # 已确认终态的重传不改结束时间/截止代次，也不覆盖原检查点。
            return {"ok":True,"status":batch["status"],"counts":counts(conn,payload["batch_id"]),"game_id":batch["game_id"]}
        if status=="done":
            completed=counts(conn,payload["batch_id"])
            criterion={"discover":"new","price_refresh":"prices","detail_enrich":"processed"}[batch["config"]["purpose"]]
            if completed[criterion]<batch["config"]["target_count"] or completed["pending"]:
                status="partial"
        terminal = status not in ("running", "waiting_verification")
        if terminal:
            exported=conn.execute("SELECT dirty_generation FROM collection_export_state WHERE game_id=?",[batch["game_id"]]).fetchone()
            checkpoint["cutoff_generation"]=exported[0] if exported else 0
        conn.execute("UPDATE collection_batch SET status=?,ended_at=?,stop_reason=?,checkpoint_json=?,heartbeat_at=? WHERE batch_id=?",
                     [status, now() if terminal else None, str(payload.get("reason", ""))[:200], C.encode(checkpoint), now(),payload["batch_id"]])
        if terminal:
            conn.execute("UPDATE monitor_job SET status=?,reason=? WHERE batch_id=?",[status,str(payload.get("reason", ""))[:200],payload["batch_id"]])
        return {"ok": True, "status": status, "counts": counts(conn, payload["batch_id"]), "game_id": batch["game_id"]}


def status(settings):
    """只读状态供统一看板；不返回领取代次或公开文字正文。"""
    try:
        with PL.run_lock(settings),db.connect(settings.paths.db,read_only=True) as conn:
            tables={r[0] for r in conn.execute("SHOW TABLES").fetchall()}
            if "collection_batch" not in tables:
                return {"protocol_version":VERSION,"batches":[],"monitor_jobs":[],"exports":[]}
            batches=[]
            for r in conn.execute("SELECT batch_id,task_id,game_id,status,CAST(created_at AS VARCHAR),CAST(ended_at AS VARCHAR),stop_reason,accesses,config_json,handoff_json,checkpoint_json FROM collection_batch ORDER BY created_at DESC LIMIT 20").fetchall():
                batches.append(dict(batch_id=r[0],task_id=r[1],game_id=r[2],status=r[3],started_at=r[4],ended_at=r[5],
                    stop_reason=r[6],active_operations=r[7],config=json.loads(r[8]),handoff=json.loads(r[9]) if r[9] else None,
                    checkpoint=json.loads(r[10]) if r[10] else {},counts=counts(conn,r[0])))
            monitor_jobs=[dict(job_id=r[0],task_id=r[1],due_at=r[2],status=r[3],reason=r[4],coalesced_slots=r[5])
                for r in conn.execute("SELECT job_id,task_id,CAST(due_at AS VARCHAR),status,reason,coalesced_slots FROM monitor_job ORDER BY due_at DESC LIMIT 20").fetchall()]
            exports=[dict(game_id=r[0],dirty_generation=r[1],exported_generation=r[2],last_export_at=r[3],error=r[4])
                for r in conn.execute("SELECT game_id,dirty_generation,exported_generation,CAST(last_export_at AS VARCHAR),error FROM collection_export_state").fetchall()]
            held=conn.execute("SELECT reason,CAST(retry_after_until AS VARCHAR) FROM collection_site_hold WHERE site='pxb7'").fetchone()
            return {"protocol_version":VERSION,"batches":batches,"monitor_jobs":monitor_jobs,"exports":exports,
                    "site_hold":{"reason":held[0],"retry_after_until":held[1]} if held else None}
    except Exception as exc:
        return {"protocol_version":VERSION,"error":type(exc).__name__}
