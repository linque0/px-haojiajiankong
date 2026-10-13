"""Normal public response fixture -> structured parser and real gateway transactions."""
import copy
import datetime as dt
import html
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pxb7 import detail_data as D, db, gateway as G
from tests.test_gateway import settings, state, _post


@pytest.fixture(scope="module")
def responses():
    node=shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    js="const fs=require('fs'),a=require('./extension/pxb7-extension/detail-data.js');const f=JSON.parse(fs.readFileSync('tests/fixtures/detail_response_pxb7_20261014.json'));process.stdout.write(JSON.stringify(f.samples.map(s=>({...a.normalize({success:true,data:s.detail}),url:s.url,listing_id:s.detail.productId}))));"
    return json.loads(subprocess.check_output([node,"-e",js],encoding="utf-8"))


def fresh(value):
    value=copy.deepcopy(value)
    value["observed_at"]=dt.datetime.now(dt.timezone.utc).isoformat()
    return value


def test_real_response_full_rosters_maps_resources_zero_and_units(responses):
    facts=D.parse(fresh(responses[1]))
    assert facts["price_yuan"]==560
    assert facts["attributes"]["five_star_chars"]==14
    assert facts["attributes"]["five_star_weapons"]==8
    assert facts["attributes"]["tap_status"]=="未绑定TAP"
    assert facts["attributes"]["trade_code_status"]=="有换绑CD"
    assert facts["features"]["lustrous_tide_cnt"]==0
    assert len(facts["features"]["wuwa_map_exploration"])==51
    assert len(facts["features"]["five_star_character_chains"])==14
    assert len(facts["features"]["five_star_weapon_refinements"])==8
    assert facts["features"]["_detail_response_evidence"]["asset_report_time"] is None
    with pytest.raises(ValueError,match="delisted"):
        D.parse(fresh(responses[0]))
    # Counter comparison retains distinct inventory copies (sold fixture is only an offline structural example).
    sold=fresh(responses[0]);sold["product"]["status"]=1
    f=D.parse(sold)["features"]
    assert len(f["five_star_weapon_refinements"])==18
    assert sum(x["name"]=="千古洑流" for x in f["five_star_weapon_refinements"])==2


@pytest.mark.parametrize("change,error",[
    (lambda v:v.update(listing_id="123456"),"detail-id-mismatch"),
    (lambda v:v["product"].update(title="截断介绍…"),"incomplete"),
    (lambda v:v["product"].update(price_fen="nan"),"invalid-detail-price"),
    (lambda v:v["product"].update(report=None),"report-not-ready"),
    (lambda v:v["product"]["report"]["groups"][-1]["elements"].pop(),"map-not-ready"),
    (lambda v:v["product"]["report"]["groups"][0]["elements"][0]["role"].update(mingZuo="1"),"field-conflict"),
    (lambda v:v["product"].update(sellerId="private"),"invalid-public-detail-shape"),
    (lambda v:v.update(observed_at="2000-01-01T00:00:00+00:00"),"observation-time"),
    (lambda v:v.update(observed_at=False),"observation-time"),
])
def test_fast_path_rejects_incomplete_conflicting_private_or_stale(responses,change,error):
    value=fresh(responses[1]);change(value)
    with pytest.raises(ValueError,match=error):D.parse(value)


def test_structured_gateway_same_round_commits_and_rejects_wrong_price(state,responses,monkeypatch):
    state.config.update(targets=["wuwa_official"],detail_transport="response_auto",detail_concurrency=4)
    monkeypatch.setattr(state.exports,"schedule",lambda:None)
    status,batch=_post(state,"/collection/batch/create",{"url":"https://www.pxb7.com/buy/10302/1","target_count":1,"collection_mode":"detail"})
    assert status==200,batch
    identity={k:batch[k] for k in ("protocol_version","batch_id","claim_epoch")}
    assert batch["config"]["detail_concurrency"]==4
    value=fresh(responses[1]);d=value["product"]
    card=f'<div data-listing-id="{d["id"]}" class="middleCard" productid="{d["id"]}" createtime="2026-10-14 00:31:09"><div class="smallCardTitle" price="56000" productname="{html.escape(d["title"][:160])}">{html.escape(d["title"][:160])}</div><span>等级80 黄数45 五星角色数量14 官服</span></div>'
    status,ack=_post(state,"/ingest/cards",{**identity,"url":"https://www.pxb7.com/buy/10302/1","html":card,"selected_ids":[d["id"]]})
    assert status==200,ack
    status,permit=_post(state,"/collection/batch/access",{**identity,"action":"detail","id":d["id"]})
    assert status==200,permit
    payload={**fresh(value),**identity,"snapshot_round":ack["round"],"attempt_id":"trial:1","access_permit":permit["permit_id"]}
    bad=copy.deepcopy(payload);bad["product"]["price_fen"]="560"
    status,rejected=_post(state,"/ingest/detail-data",bad)
    assert status==422 and rejected["fallback"] and rejected["error"]=="detail-price-conflict"
    status,result=_post(state,"/ingest/detail-data",payload)
    assert status==200 and result["snapshot_rows_updated"]==1 and result["complete"],result
    status,repeat=_post(state,"/ingest/detail-data",payload)
    assert status==200,repeat
    with db.connect(state.settings.paths.db) as c:
        assert c.execute("select count(*) from fct_listing_snapshot").fetchone()[0]==1
        assert c.execute("select count(*) from collection_observation where observation_kind='text'").fetchone()[0]==1
    evidence=list(state.settings.paths.raw_root.rglob(f'detail_{d["id"]}.json'))
    assert evidence and all(json.loads(p.read_text(encoding="utf-8"))["page_type"]=="detail_data" for p in evidence)
    assert not list(state.settings.paths.raw_root.rglob(f'detail_{d["id"]}.html'))


def test_shared_thread_lock_waits_for_export(settings):
    import threading
    from pxb7.pipeline import run_lock
    entered=threading.Event();release=threading.Event();order=[]
    def worker():
        with run_lock(settings):entered.set();release.wait(2);order.append("export")
    t=threading.Thread(target=worker);t.start();assert entered.wait(2)
    threading.Timer(.05,release.set).start()
    with run_lock(settings):order.append("ingest")
    t.join(2)
    assert order==["export","ingest"]
