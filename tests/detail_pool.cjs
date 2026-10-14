const assert=require('node:assert/strict');
const {create}=require('../extension/pxb7-extension/collection.js');
const {normalize}=require('../extension/pxb7-extension/detail-data.js');
const fixture=require('./fixtures/detail_response_pxb7_20261014.json');
const source={tab:{id:10,url:'https://www.pxb7.com/buy/10302/1'}};
const items=Array.from({length:4},(_,i)=>({id:String(123456+i),url:`https://www.pxb7.com/product/${123456+i}/1`,prefix:'80级'}));
function setup(n=2,budget=100) {
  let nextTab=20,used=0,response={ok:true,data:{ok:true,snapshot_rows_updated:1,missing:[]}};
  const data={},nav=[],ingested=[],closed=[];
  const chrome={storage:{local:{get:async k=>structuredClone({[k]:data[k]}),set:async v=>Object.assign(data,structuredClone(v))}},
    tabs:{create:async()=>({id:nextTab++}),update:async(id,v)=>nav.push({id,...v}),remove:async id=>closed.push(id),sendMessage:async()=>({ok:true}),onRemoved:{addListener(){}}},
    alarms:{create:async()=>{},clear:async()=>{},onAlarm:{addListener(){}}}};
  const deps={createBatch:async()=>({ok:true,data:{ok:true,protocol_version:1,batch_id:'batch',claim_epoch:'claim',
    config:{detail_transport:'response_auto',detail_concurrency:n,detail_interval_ms:0,collection_mode:'detail',purpose:'discover',target_count:4}}}),
    batchAccess:async()=>({data:++used<=budget ? {ok:true}:{ok:false,error:'access-budget-exhausted'}}),
    batchProgress:async p=>({data:{ok:true,status:p.status}}),flushBatch:async()=>({data:{ok:true}}),
    retry:async()=>({ok:true,data:{ok:true,claimed:false}})};
  const ingest=async(p,v)=>{ingested.push({p,v});return response;};
  return {q:create(chrome,ingest,deps),restart:()=>create(chrome,ingest,deps),nav,ingested,closed,data,setResponse:r=>response=r};
}
const sender=(tabId,item)=>({tab:{id:tabId,url:item.url}});
async function start(f){const b=await f.q.begin(source,{protocol_version:1,mode:'detail',total:4});
  assert.ok(b.ok);assert.ok((await f.q.start(source,{taskId:b.taskId,round:'2026-10-14T01:00:00',items})).ok);return b.taskId;}
async function job(f,tabId,item){return (await f.q.context(sender(tabId,item),{pageUrl:item.url})).job;}
const msg=j=>({taskId:j.taskId,nonce:j.nonce,payload:{listing_id:j.id,url:j.url,schema_version:1}});
(async()=>{
  const sanitized=normalize({success:true,data:{...fixture.samples[1].detail,showKey:'secret',sellerId:'private'}});
  assert.equal(sanitized.product.price_fen,'56000');assert.equal(sanitized.product.report.groups.at(-1).elements.length,51);
  assert.ok(!JSON.stringify(sanitized).includes('secret'));assert.ok(!JSON.stringify(sanitized).includes('sellerId'));
  assert.equal(normalize({success:true,data:{productId:2447969231586192717}}),null,'unsafe numerical ids never rounded');

  const f=setup();await start(f);
  assert.equal(f.nav.filter(n=>n.url).length,2);
  const a=await job(f,20,items[0]),b=await job(f,21,items[1]);
  assert.equal(a.config.detail_concurrency,2);
  f.q=f.restart(); // persisted slot association survives MV3 worker lifecycle
  await f.q.result(sender(21,items[1]),msg(b));
  assert.equal(f.ingested[0].v.listing_id,items[1].id,'out-of-order completion binds to its own slot');
  assert.equal((await f.q.result(sender(21,items[1]),msg(b))).ok,false,'duplicate receipt does not commit twice');
  await f.q.result(sender(20,items[0]),msg(a));
  const c=await job(f,21,items[2]),d=await job(f,20,items[3]);
  await f.q.result(sender(20,items[3]),msg(d));await f.q.result(sender(21,items[2]),msg(c));
  const done=(await f.q.progress()).progress;
  assert.equal(done.status,'done');assert.equal(done.complete_count,4);assert.equal(done.in_flight,0);
  assert.equal(f.ingested.length,4);assert.deepEqual(f.closed.sort(),[20,21]);assert.ok(done.completedAt && done.elapsed_ms>=0);

  const sourceRisk=setup(1),originalNow=Date.now;
  let clock=originalNow();Date.now=()=>clock;
  try {
    const begin=await sourceRisk.q.begin(source,{protocol_version:1,mode:'detail',total:4});
    await sourceRisk.q.verificationPause(source,{taskId:begin.taskId,pageUrl:source.tab.url});
    clock+=5432;
    await sourceRisk.q.verificationResume(source,{taskId:begin.taskId,pageUrl:source.tab.url});
    await sourceRisk.q.start(source,{taskId:begin.taskId,round:'2026-10-14T01:00:00',items});
    const resumed=(await sourceRisk.q.progress()).progress;
    assert.equal(resumed.verification_count,1,'source challenge survives transition into detail pool');
    assert.equal(resumed.timings.verification_ms,5432,'source wait is not reset to zero');
    assert.equal(resumed.timings.discovery_ms,5432,'end-to-end discovery includes verification wait');
    await sourceRisk.q.cancel();
  } finally {Date.now=originalNow;}

  const risk=setup();await start(risk);const ra=await job(risk,20,items[0]),rb=await job(risk,21,items[1]);
  const pause=await risk.q.verificationPause(sender(20,items[0]),{taskId:ra.taskId,nonce:ra.nonce,pageUrl:ra.url});
  assert.equal(pause.paused,true);assert.equal((await risk.q.progress()).progress.status,'waiting_verification');
  assert.equal((await risk.q.result(sender(21,items[1]),msg(rb))).held,true);
  assert.equal(risk.ingested.length,0);assert.equal(risk.nav.filter(n=>n.url).length,2,'verification prevents refilling other slots');
  const rp=await job(risk,20,items[0]);assert.notEqual(rp.nonce,ra.nonce);
  assert.ok((await risk.q.claimVerificationAttempt(sender(20,items[0]),{...msg(rp),pageUrl:rp.url})).ok);
  assert.equal((await risk.q.claimVerificationAttempt(sender(20,items[0]),{...msg(rp),pageUrl:rp.url})).error,'attempt-used');
  // Second challenged worker must be cleared independently, rather than cleared by the first worker's result.
  await risk.q.verificationPause(sender(21,items[1]),{...msg(rb),pageUrl:rb.url});
  const rpb=await job(risk,21,items[1]);
  assert.equal((await risk.q.verificationResume(sender(20,items[0]),{...msg(rp),pageUrl:rp.url})).held,true);
  await risk.q.timeout();assert.equal(risk.ingested.length,0,'waiting is not a timeout failure');
  assert.equal((await risk.q.verificationResume(sender(21,items[1]),{...msg(rpb),pageUrl:rpb.url})).held,false);
  await risk.q.result(sender(20,items[0]),msg(rp));assert.equal(risk.ingested.length,1,'same task resumes without skipping challenged account');
  await risk.q.cancel();assert.equal((await risk.q.result(sender(21,items[1]),msg(rpb))).ok,false);

  const fallback=setup(1);await start(fallback);const fj=await job(fallback,20,items[0]);
  fallback.setResponse({ok:true,data:{ok:false,fallback:true,error:'detail-map-not-ready'}});
  assert.equal((await fallback.q.result(sender(20,items[0]),msg(fj))).fallback,true);
  assert.equal(fallback.nav.filter(n=>n.url).length,1,'missing map falls back in the same page without a repeated navigation');
  fallback.setResponse({ok:true,data:{ok:true,snapshot_rows_updated:1,missing:[]}});
  const dom=msg(fj);delete dom.payload.schema_version;dom.payload.html='public DOM';
  await fallback.q.result(sender(20,items[0]),dom);assert.equal(fallback.ingested.at(-1).p,'/ingest/detail');
  assert.equal((await fallback.q.progress()).progress.dom_fallback,1);
  await fallback.q.cancel();

  const retry=setup(1);await start(retry);const rj=await job(retry,20,items[0]);
  await retry.q.result(sender(20,items[0]),{taskId:rj.taskId,nonce:rj.nonce,error:'detail-timeout'});
  assert.equal((await job(retry,20,items[1])).id,items[1].id,'ineligible retry yields its free worker to the next pending account');
  await retry.q.cancel();

  const limited=setup(8,1);await start(limited);const lj=await job(limited,20,items[0]);
  assert.equal(limited.nav.filter(n=>n.url).length,1);
  await limited.q.result(sender(20,items[0]),msg(lj));
  assert.equal((await limited.q.progress()).progress.status,'partial');
  assert.equal((await limited.q.progress()).progress.complete_count,1);
  assert.match((await limited.q.progress()).progress.phase,/access-budget-exhausted/);
  console.log('detail pool: public adapter, concurrency, identity, restart, verification, fallback and budget passed');
})().catch(e=>{console.error(e);process.exit(1);});
