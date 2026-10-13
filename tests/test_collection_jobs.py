"""逻辑批次的观察/幂等/导出/缓存；隔离库、真实公开列表夹具，不请求网站。"""
import datetime as dt
import json

import pytest

from pxb7 import collection_jobs as J, db, gateway as G, parser as P
from tests.test_gateway import settings, state, _post, CARDS_PAYLOAD


@pytest.fixture
def job_state(state, monkeypatch):
    monkeypatch.setattr(state.exports, "schedule", lambda: None)
    return state


def begin(state, **overrides):
    status, ack = _post(state, "/collection/batch/create", {
        "url": CARDS_PAYLOAD["url"], "collection_mode": "auto", "target_count": 2, **overrides})
    assert status == 200, ack
    identity = {k: ack[k] for k in ("protocol_version", "batch_id", "claim_epoch")}
    return identity, ack


def cards(state, identity):
    parsed = P.parse_list_page(CARDS_PAYLOAD["html"], url=CARDS_PAYLOAD["url"])
    ids = [c.listing_id for c in parsed.cards]
    status, ack = _post(state, "/ingest/cards", {**CARDS_PAYLOAD, **identity, "selected_ids": ids})
    assert status == 200, ack
    return parsed.cards, ack


def test_logical_target_frozen_and_protocol_rejections(job_state):
    identity, ack = begin(job_state, target_count=450)
    assert ack["config"]["target_count"] == 450 and ack["config"]["block_size"] == 200
    job_state.config["cards_target"] = 10
    job_state.config["detail_interval_ms"] = 9900
    with db.connect(job_state.settings.paths.db) as conn:
        assert J.get(conn, identity)["config"]["detail_interval_ms"] == 0
        assert J.get(conn, identity)["config"]["target_count"] == 450
    status, ack = _post(job_state, "/ingest/cards", {**CARDS_PAYLOAD, **identity, "claim_epoch": "wrong", "selected_ids": []})
    assert status == 409 and ack["error"] == "invalid-batch-identity"
    status, ack = _post(job_state, "/ingest/cards", {**CARDS_PAYLOAD, **identity, "selected_ids": ["123456"] * 201})
    assert status == 409 and ack["error"] == "invalid-execution-block"
    assert G.handle_status(job_state)["db"]["snapshot_rows"] == 0


def test_same_price_observed_and_retransmission_idempotent(job_state):
    _post(job_state, "/ingest/cards", CARDS_PAYLOAD)
    identity, _ = begin(job_state, purpose="price_refresh")
    _, first = cards(job_state, identity)
    _, repeat = cards(job_state, identity)
    assert first["snapshots_inserted"] == 2 and repeat["snapshots_inserted"] == 0
    with db.connect(job_state.settings.paths.db) as conn:
        assert conn.execute("SELECT count(*) FROM collection_observation WHERE observation_kind='price'").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM fct_price_change").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM fct_listing_snapshot").fetchone()[0] == 2
        assert J.counts(conn, identity["batch_id"])["prices"] == 2


def test_coalesced_export_restarts_and_retries_locally(job_state, monkeypatch):
    from pxb7 import analysis as A
    from pxb7.export_queue import ExportQueue
    identity, _ = begin(job_state)
    _, ack = cards(job_state, identity)
    assert ack["analysis_sync"]["pending"] is True
    exports = []
    original = A.sync_game_csv
    monkeypatch.setattr(A, "sync_game_csv", lambda *args, **kwargs: exports.append(kwargs["game_id"]) or original(*args, **kwargs))
    replacement = ExportQueue(job_state.settings)
    assert replacement.pending()
    result = replacement.flush()
    assert result["ok"] and exports == [10026]
    assert not replacement.pending()
    replacement.flush()
    assert exports == [10026], "相同已导出代次不重复导出"
    with db.connect(job_state.settings.paths.db) as conn:
        from pxb7.export_queue import dirty
        dirty(conn,10026)
    monkeypatch.setattr(A, "sync_game_csv", lambda *args, **kwargs: {"ok":False,"error":"locked","game_id":10026})
    assert not replacement.flush()["ok"] and replacement.pending()
    monkeypatch.setattr(A, "sync_game_csv", original)
    assert replacement.flush()["ok"] and not replacement.pending()


def test_public_text_cache_identity_age_and_reuse_time(job_state):
    identity, _ = begin(job_state)
    parsed, ack = cards(job_state, identity)
    card = parsed[0]
    acquired = J.now() - dt.timedelta(minutes=1)
    title = card.title + "；公开完整介绍尾部，详情看图"
    item = {"id":card.listing_id,"url":f"https://www.pxb7.com/product/{card.listing_id}/1","snapshot_round":ack["round"],"title":title,
            "source":"title-api","acquired_at":acquired.isoformat()}
    status, result = _post(job_state,"/collection/text",{**identity,"items":[item]})
    assert status == 200 and result["items"][0]["text_complete"], result
    status, result = _post(job_state,"/collection/text-cache/store",{**identity,"items":[{**item,"code":"CODE123"}]})
    assert status == 200, result
    query = {**identity,"items":[{"id":card.listing_id,"code":"CODE123","prefix":card.title}]}
    status, result = _post(job_state,"/collection/text-cache/query",query)
    assert result["items"][card.listing_id]["reused"]
    assert dt.datetime.fromisoformat(result["items"][card.listing_id]["acquired_at"]) == acquired
    assert _post(job_state,"/collection/text-cache/query",{**query,"items":[{**query["items"][0],"code":"WRONGCODE"}]})[1]["items"] == {}
    _post(job_state,"/collection/batch/progress",{**identity,"status":"partial"})
    next_id,_=begin(job_state,purpose="price_refresh")
    cards(job_state,next_id)
    status,result=_post(job_state,"/collection/text",{**next_id,"items":[{**item,"reused":True}]})
    assert status==200,result
    assert dt.datetime.fromisoformat(result["items"][0]["acquired_at"]) == acquired
    with db.connect(job_state.settings.paths.db) as conn:
        assert conn.execute("SELECT count(*) FROM collection_observation WHERE batch_id=? AND observation_kind='text'",[next_id["batch_id"]]).fetchone()[0] == 0


def test_batch_access_stops_during_verification_and_at_budget(job_state):
    job_state.config["daily_access_budget"]=1
    identity,_=begin(job_state)
    assert _post(job_state,"/collection/batch/access",{**identity,"action":"text","id":"123456"})[1]["ok"]
    assert _post(job_state,"/collection/batch/access",{**identity,"action":"detail","id":"123456"})[1]["error"]=="access-budget-exhausted"
    _post(job_state,"/collection/batch/progress",{**identity,"status":"waiting_verification"})
    assert _post(job_state,"/collection/batch/access",{**identity,"action":"list"})[1]["error"]=="batch-not-running"


def test_compact_evidence_replays_public_fields():
    from pxb7.evidence import compact
    before=P.parse_list_page(CARDS_PAYLOAD["html"],url=CARDS_PAYLOAD["url"])
    evidence=compact(CARDS_PAYLOAD["html"]+"<script>private-token</script><img src='binary-url'><input value='secret'>",page_type="list")
    after=P.parse_list_page(evidence,url=CARDS_PAYLOAD["url"])
    assert [(c.listing_id,c.title,c.fields) for c in after.cards]==[(c.listing_id,c.title,c.fields) for c in before.cards]
    assert all(s not in evidence for s in ("private-token","binary-url","secret"))


def test_monitor_offline_coalescing_and_exclusive_claim(job_state,monkeypatch):
    import dataclasses
    spec={"enabled":True,"interval_minutes":360,"target_count":37,"scan_budget":1000,"daily_access_budget":50,
          "purpose":"price_refresh","source_url":CARDS_PAYLOAD["url"]}
    G.ensure_collection(job_state)
    job_state.tasks=tuple(dataclasses.replace(t,monitor=spec) if t.task_id=="genshin_official" else t for t in job_state.tasks)
    stamp=J.now()
    job_state.monitor.tick(clock=stamp,online=False)
    job_state.monitor.tick(clock=stamp+dt.timedelta(hours=18),online=False)
    status=J.status(job_state.settings)
    assert {j["status"] for j in status["monitor_jobs"]}=={"deferred","missed"}
    # 不制造三份补跑队列，明确记录合并掉的两个时段。
    assert status["monitor_jobs"][0]["coalesced_slots"]==2
    monkeypatch.setattr(J,"now",lambda:stamp+dt.timedelta(hours=18))
    result=job_state.monitor.claim()
    assert result["ok"] and result["config"]["target_count"]==37
    assert result["config"]["daily_access_budget"]==50
    assert not job_state.monitor.claim()["ok"],"一个会话不能领取第二个执行任务"


def test_monitor_definition_requires_explicit_budget():
    from pxb7.monitor import validate
    assert validate(None,game_id=10302,biz_prod=1) is None
    assert validate({"enabled":False},game_id=10302,biz_prod=1)=={"enabled":False}
    with pytest.raises(ValueError):
        validate({"enabled":True,"interval_minutes":1},game_id=10302,biz_prod=1)


def test_finish_handoff_is_once_and_independent_of_export(job_state, monkeypatch):
    from pxb7 import analysis as A
    identity,_=begin(job_state)
    cards(job_state,identity)
    _post(job_state,"/collection/batch/progress",{**identity,"status":"partial"})
    calls=[]
    original=A.prepare_analysis
    monkeypatch.setattr(A,"prepare_analysis",lambda *args,**kwargs:calls.append(1) or original(*args,**kwargs))
    status,result=_post(job_state,"/collection/batch/flush",identity)
    assert status==200 and result["ok"] and result["handoff"]["ok"]
    assert result["handoff"]["valuation_publication"]=="pending-existing-publisher"
    assert _post(job_state,"/collection/batch/flush",identity)[1]["handoff"]==result["handoff"]
    assert calls==[1]


def test_text_retransmission_and_old_tail_cannot_overwrite(job_state):
    identity,_=begin(job_state)
    parsed,ack=cards(job_state,identity)
    item={"id":parsed[0].listing_id,"url":f"https://www.pxb7.com/product/{parsed[0].listing_id}/1",
        "snapshot_round":ack["round"],"title":parsed[0].title+"；完整介绍尾部，详情看图",
        "source":"title-api","acquired_at":J.now().isoformat()}
    assert _post(job_state,"/collection/text",{**identity,"items":[item]})[0]==200
    with db.connect(job_state.settings.paths.db) as conn:
        before=conn.execute("SELECT dirty_generation FROM collection_export_state").fetchone()[0]
    result=_post(job_state,"/collection/text",{**identity,"items":[item]})[1]
    assert result["items"][0]["duplicate"]
    older={**item,"title":parsed[0].title+"；旧尾部，详情看图","acquired_at":(J.now()-dt.timedelta(minutes=2)).isoformat()}
    assert _post(job_state,"/collection/text",{**identity,"items":[older]})[1]["error"]=="stale-text-response"
    with db.connect(job_state.settings.paths.db) as conn:
        assert conn.execute("SELECT dirty_generation FROM collection_export_state").fetchone()[0]==before


def test_detail_exclusive_public_fields_commit_without_changing_price(job_state):
    identity,_=begin(job_state)
    parsed,ack=cards(job_state,identity)
    card=parsed[0]
    html=f"<div class='product-detail'><h1>{card.title}；详情看图</h1><div class='server'>B服</div>邮箱出售 已绑定TAP 官方验号 找回包赔</div>"
    payload={**identity,"listing_id":card.listing_id,"snapshot_round":ack["round"],
        "url":f"https://www.pxb7.com/product/{card.listing_id}/1","html":html}
    status,result=_post(job_state,"/ingest/detail",payload)
    assert status==200,result
    with db.connect(job_state.settings.paths.db) as conn:
        stored=conn.execute("SELECT server,mail_status,tap_status,price_yuan,has_compensation FROM fct_listing_snapshot WHERE listing_id=?",[card.listing_id]).fetchone()
        assert stored[:3]==("B服","邮箱出售","已绑定TAP") and float(stored[3])==300 and stored[4] is True


def test_wuwa_full_intro_requires_public_detail_proof_and_zero_is_valid(job_state):
    from pxb7 import backfill as B, collection as C
    _post(job_state,"/ingest/cards",CARDS_PAYLOAD)
    card=P.parse_list_page(CARDS_PAYLOAD["html"],url=CARDS_PAYLOAD["url"]).cards[0]
    with db.connect(job_state.settings.paths.db) as conn:
        conn.execute("UPDATE dim_listing SET game_id=10302 WHERE listing_id=?",[card.listing_id])
        conn.execute("UPDATE fct_listing_snapshot SET level=80,yellow_cnt=0,five_star_chars=0,five_star_weapons=NULL,server=NULL,extracted_features='{}' WHERE listing_id=?",[card.listing_id])
        def missing():
            current=C.current_data(conn,card.listing_id)
            return B.missing({**current,"features":json.loads(current["extracted_features"] or "{}")})
        issues,_=J.quality(conn,card.listing_id,{},missing())
        assert "public_detail_text" in {i["key"] for i in issues}
        proof={"_public_text":{"acquired_at":J.now().isoformat(),"field_states":{"server":"unreported","wuwa_resources":"unreported"}},
            "_weapon_information":{"state":"unreported"}}
        conn.execute("UPDATE fct_listing_snapshot SET five_star_weapons=0,extracted_features=? WHERE listing_id=?",[json.dumps(proof),card.listing_id])
        issues,ready=J.quality(conn,card.listing_id,{},missing())
        assert "server" not in {i["key"] for i in issues} and "wuwa_resources" not in {i["key"] for i in issues}
        assert not ready,"公开文字未公示的核心估值信息不等于估值可用"
        assert conn.execute("SELECT server,yellow_cnt FROM fct_listing_snapshot WHERE listing_id=?",[card.listing_id]).fetchone()==(None,0)


def test_retry_after_hold_survives_restart_and_rejects_early_clear(job_state,monkeypatch):
    identity,_=begin(job_state)
    at=J.now()
    diagnostic={"id":"123456","error":"rate-limit","status":429,"stage":"fetch",
        "retry_after_until":(at+dt.timedelta(minutes=5)).timestamp()*1000,"headers":"private-secret"}
    assert _post(job_state,"/collection/batch/progress",{**identity,"status":"partial","checkpoint":{"diagnostics":[diagnostic]}})[0]==200
    assert G.handle_ingest(job_state,"/collection/batch/create",json.dumps({"url":CARDS_PAYLOAD["url"]}).encode())[1]["error"]=="site-access-held"
    assert _post(job_state,"/collection/batch/clear-hold",{})[1]["error"]=="retry-after-not-elapsed"
    with db.connect(job_state.settings.paths.db) as conn:
        assert "private-secret" not in conn.execute("SELECT checkpoint_json FROM collection_batch").fetchone()[0]
    monkeypatch.setattr(J,"now",lambda:at+dt.timedelta(minutes=6))
    next_identity,_=begin(job_state)
    assert next_identity["batch_id"]!=identity["batch_id"]


def test_lost_heartbeat_reconciles_but_verification_wait_does_not_expire(job_state,monkeypatch):
    identity,_=begin(job_state)
    old=J.now()-dt.timedelta(hours=1)
    with db.connect(job_state.settings.paths.db) as conn:
        conn.execute("UPDATE collection_batch SET heartbeat_at=?",[old])
        J.reconcile_expired(conn)
        assert J.get(conn,identity,terminal=True)["status"]=="needs_reconcile"
    assert _post(job_state,"/collection/batch/cancel-local",{"batch_id":identity["batch_id"]})[1]["ok"]
    identity,_=begin(job_state)
    _post(job_state,"/collection/batch/progress",{**identity,"status":"waiting_verification"})
    with db.connect(job_state.settings.paths.db) as conn:
        conn.execute("UPDATE collection_batch SET heartbeat_at=?",[old])
        J.reconcile_expired(conn)
        assert J.get(conn,identity,terminal=True)["status"]=="waiting_verification"


def test_invalid_target_and_id_types_do_not_silently_fall_back(job_state):
    for target in (False,-1,"10",None):
        result=_post(job_state,"/collection/batch/create",{"url":CARDS_PAYLOAD["url"],"target_count":target})
        assert result[0]==409 and result[1]["error"]=="invalid-target-count"
    identity,_=begin(job_state)
    for ids in ([123456],[{}],["bad"]):
        assert _post(job_state,"/ingest/cards",{**CARDS_PAYLOAD,**identity,"selected_ids":ids})[1]["error"]=="invalid-execution-block"
