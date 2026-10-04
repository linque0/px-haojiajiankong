"""全文部分失败仍入库，但不标记去重成功、不加载更多，弹窗收到完整率。"""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('stop', ['rate-limit', 'done'])
def test_partial_title_collection_can_be_retried_and_reports_missing(stop):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    script = r'''
const vm=require('vm'),fs=require('fs'),assert=require('assert');
const timers=[],sent=[],storage=new Map(),badges=[],handlers=[];let sweeps=0;
const full='80级，10个五星角色：3命角色甲；2个五星武器：精1武器甲，精2武器乙';
const doc={documentElement:{outerHTML:'<a>short title</a>',cloneNode(){
 const attrs={productid:'123456'};
 return {querySelectorAll(){return [{getAttribute(n){return attrs[n];},setAttribute(n,v){attrs[n]=v;}}];},
   get outerHTML(){return '<a data-pxb7-full-title="'+attrs['data-pxb7-full-title']+'">123456</a>';}};
}},createElement(){const badge={style:{}};badges.push(badge);return badge;},body:{appendChild(){}}};
const sandbox={document:doc,console,Date,Promise,JSON,Number,String,process,
 location:{pathname:'/buy/10302/1',href:'https://www.pxb7.com/buy/10302/1',search:'',hash:''},
 sessionStorage:{getItem(k){return storage.get(k);},setItem(k,v){storage.set(k,v);}},
 setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},setInterval(){},addEventListener(){},
 PXB7_TITLES:{async collect(){return {cards:2,captured:1,failed:1,requested:1,stop:process.argv[2],titles:{'123456':full}};}},
 PXB7_SWEEP:{clampTarget(){return 32;},countCards(){return 2;},findMoreControl(){return null;},nextAction(){sweeps++;return 'done';},DEFAULTS:{}},
 chrome:{runtime:{getManifest(){return {version:'0.4.4'};},onMessage:{addListener(fn){handlers.push(fn);}},sendMessage(msg,cb){
  if(msg.type==='config')return cb({ok:true,data:{ok:true,config:{auto_ingest:true,spa_settle_ms:1,cards_target:32}}});
  if(msg.type!=="ingest")return cb({ok:true,worker:false,taskId:"test-task"});
  sent.push(msg);cb({ok:true,data:{ok:true,cards_seen:2,cards_parsed:2,new_listings:0,parse_success_rate:1}});
 }}}
};sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
async function drain(){await new Promise(setImmediate);while(timers.length){timers.shift()();await new Promise(setImmediate);}}
(async()=>{
 await drain();assert.equal(sent.length,1);assert.equal(sweeps,1);assert.equal(storage.size,0);
 assert.ok(sent[0].payload.html.includes(full));
 assert.equal(sent[0].payload.title_collection.captured,1);assert.ok(!('titles' in sent[0].payload.title_collection));
 assert.ok(badges[0].textContent.includes('全文 1/2'));assert.ok(badges[0].textContent.includes('1 张待补齐'));
 let reply;
 handlers[0]({type:'collect-now'},null,r=>reply=r);await drain();
 assert.equal(sent.length,2);assert.equal(reply.title_collection.captured,1);assert.equal(reply.title_collection.cards,2);
 assert.ok(!('titles' in reply.title_collection));assert.equal(storage.size,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, '-e', script, str(root / 'extension/pxb7-extension/content.js'), stop],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
