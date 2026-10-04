"""执行真实内容脚本，验证 SPA 列表→详情→列表使用当前路由的采集端点。"""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_spa_navigation_uses_current_page_type():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    script = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const sent = [], timers = [], intervals = [], storage = new Map();
const location = {pathname:'/buy/10302/1', search:'', hash:'', href:'https://www.pxb7.com/buy/10302/1'};
const sandbox = {
  location, console, Date, Promise, JSON, Number, String,
  document:{documentElement:{outerHTML:'<div>current page</div>'},
    querySelector(){return {textContent:'80级，17个五星武器：精1千古洑流'};},
    body:{appendChild(){}}, createElement(){return {style:{}};}},
  sessionStorage:{getItem(k){return storage.get(k);},setItem(k,v){storage.set(k,v);}},
  setTimeout(fn){timers.push(fn);return timers.length;}, clearTimeout(){},
  setInterval(fn,ms){intervals.push([fn,ms]);},
  chrome:{runtime:{getManifest(){return {version:'0.4.1'};},
    onMessage:{addListener(){}}, sendMessage(msg,cb){
      if(msg.type==='config') return cb({ok:true,data:{ok:true,config:{spa_settle_ms:1}}});
      if(msg.type!=="ingest") return cb({ok:true,worker:false,taskId:"test-task"});
      sent.push(msg);
      cb({ok:true,data:{ok:true,cards_seen:16,cards_parsed:16,parse_success_rate:1,
        new_listings:0,viewers_masked:null,favorites_cnt:0}});
    }}},
  addEventListener(){}
};
sandbox.window = sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
async function drain(){
  await new Promise(setImmediate);
  while(timers.length){timers.shift()();await new Promise(setImmediate);}
}
(async()=>{
  await drain();
  const watch = intervals.find(([fn,ms])=>ms===500)[0];
  location.pathname='/product/123456/1';location.href='https://www.pxb7.com'+location.pathname;
  watch();await drain();
  location.pathname='/buy/10026/1';location.href='https://www.pxb7.com'+location.pathname;
  watch();await drain();
  assert.deepEqual(sent.map(m=>m.path), ['/ingest/cards','/ingest/detail','/ingest/cards']);
  assert.equal(sent[1].payload.listing_id, '123456');
  assert.equal(sent[2].payload.url, 'https://www.pxb7.com/buy/10026/1');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    path = Path(__file__).resolve().parents[1] / 'extension' / 'pxb7-extension' / 'content.js'
    result = subprocess.run([node, '-e', script, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('marker,ready,expected', [(True, True, 1), (False, True, 0), (True, False, 0)])
def test_detail_completion_waits_for_dom_with_auto_disabled(marker, ready, expected):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    script = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const marker = process.argv[2] === 'True', ready = process.argv[3] === 'True';
const hash = marker ? '#pxb7-collect-detail' : '';
const timers = [], sent = []; let reads = 0;
const sandbox = {
  console, Date, Promise, JSON, Number, String,
  location:{pathname:'/product/123456/1',search:'',hash,
    href:'https://www.pxb7.com/product/123456/1'+hash},
  document:{documentElement:{outerHTML:'<div>detail</div>'},body:{appendChild(){}},
    createElement(){return {style:{}};},querySelector(){
      reads++; return ready && reads > 2 ? {textContent:'17个五星武器：精1千古洑流'} : null;
    }},
  sessionStorage:{getItem(){return null;},setItem(){}},
  setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},setInterval(){},
  chrome:{runtime:{getManifest(){return {version:'0.4.2'};},onMessage:{addListener(){}},
    sendMessage(msg,cb){
      if(msg.type==='config') return cb({ok:true,data:{ok:true,config:{auto_ingest:false,spa_settle_ms:1}}});
      if(msg.type!=="ingest") return cb({ok:true,worker:false,taskId:"test-task"});
      if(msg.type!=="ingest")return cb({ok:true,worker:false,taskId:"test-task"});
  sent.push(msg);cb({ok:true,data:{attributes:{five_star_weapons:17},weapon_details_complete:true}});
    }}},addEventListener(){}
};sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
(async()=>{
  await new Promise(setImmediate);
  while(timers.length){timers.shift()();await new Promise(setImmediate);}
  assert.equal(sent.length,Number(process.argv[4]));
  if(sent.length){assert.equal(sent[0].path,'/ingest/detail');assert.ok(reads>=4);}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    path = Path(__file__).resolve().parents[1] / 'extension' / 'pxb7-extension' / 'content.js'
    result = subprocess.run([node, '-e', script, str(path), str(marker), str(ready), str(expected)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
