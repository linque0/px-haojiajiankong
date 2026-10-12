const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {create} = require('../extension/pxb7-extension/collection.js');
const popup = {id:'test-extension',url:'chrome-extension://test-extension/popup.html'};
function fixture(count=1) {
  const storage = {}, navigations=[], closed=[], ingested=[], alarms={};
  let listener, response={ok:true,data:{ok:true,snapshot_rows_updated:1,backfill:{complete:true,missing:[]}}};
  const items=Array.from({length:count},(_,i)=>({id:String(123456+i),url:`https://www.pxb7.com/product/${123456+i}/1`,
    snapshot_round:`2026-10-06T${i%2 ? '10' : '09'}:00:00`,game_id:10302}));
  const chrome={
    storage:{local:{async get(k){return structuredClone(Object.fromEntries((Array.isArray(k)?k:[k]).map(x=>[x,storage[x]])));},
      async set(v){Object.assign(storage,structuredClone(v));}}},
    runtime:{id:'test-extension',getURL:p=>`chrome-extension://test-extension/${p}`,getManifest:()=>({version:'0.5.7'}),
      onMessage:{addListener(fn){listener=fn;}},onInstalled:{addListener(){}},onStartup:{addListener(){}}},
    action:{setBadgeBackgroundColor(){},setBadgeText(){}},
    alarms:{async create(k,v){alarms[k]=v;},async clear(k){delete alarms[k];},onAlarm:{addListener(){}}},
    tabs:{async query(){return [{id:99,url:'https://example.com/'}];},async create(){return {id:20};},
      async update(id,opts){navigations.push({id,...opts});},async remove(id){closed.push(id);},
      async sendMessage(){throw Error('no source page');},onRemoved:{addListener(){}}},
  };
  const deps={getConfig:async()=>({detail_interval_ms:0}),prepareBackfill:async()=>({ok:true,data:{ok:true,items,backup:'fixture.duckdb'}})};
  const ingest=async(path,payload)=>{ingested.push(payload);return response;};
  return {chrome,items,storage,ingested,navigations,closed,queue:create(chrome,ingest,deps),restart:()=>create(chrome,ingest,deps),
    deps,listener:()=>listener,setResponse:r=>response=r};
}
async function submit(f,queue=f.queue) {
  const progress=(await queue.progress()).progress;
  const item=f.items[progress.processed];
  const worker={tab:{id:20,url:item.url},url:item.url};
  const job=(await queue.context(worker)).job;
  return queue.result(worker,{taskId:job.taskId,nonce:job.nonce,payload:{listing_id:item.id,url:item.url,html:'public DOM'}});
}
(async()=>{
  const f=fixture(201);
  assert.equal((await f.queue.startBackfill({all:true})).ok,true,'从非站点标签页启动');
  assert.equal((await f.queue.begin({tab:{id:2},url:'https://www.pxb7.com/buy/10302/1'},{mode:'list'})).ok,false);
  for(let i=0;i<201;i++) {
    if(i===200) assert.match((await f.queue.progress()).progress.phase,/第 2 批/);
    await submit(f,i>=100 ? f.restart() : f.queue);
    assert.equal(f.ingested[i].snapshot_round,f.items[i].snapshot_round);
    assert.equal(f.ingested[i].backfill,true);
  }
  assert.equal((await f.queue.progress()).progress.status,'done');
  assert.equal(Object.keys(f.storage['pxb7-backfill-results']).length,201);
  assert.deepEqual(f.closed,[20]);
  const invalid=fixture(2); invalid.items[1]=invalid.items[0];
  assert.equal((await invalid.queue.startBackfill({all:true})).ok,false,'重复账号不得进入补录队列');
  assert.equal(invalid.navigations.length,0);
  const noConfig=fixture(); noConfig.deps.getConfig=async()=>({});
  assert.equal((await noConfig.restart().startBackfill({all:true})).ok,false,'读取不到间隔配置时不得默认取消限速');
  assert.equal(noConfig.navigations.length,0);

  const cancel=fixture(); await cancel.queue.startBackfill({all:true});
  const worker={tab:{id:20,url:cancel.items[0].url},url:cancel.items[0].url};
  const old=(await cancel.queue.context(worker)).job;
  await cancel.queue.cancel();
  assert.equal(cancel.navigations.at(-1).id,99,'取消后返回原非站点标签页');
  assert.equal((await cancel.queue.startBackfill({all:true})).ok,true);
  assert.equal((await cancel.queue.result(worker,{taskId:old.taskId,nonce:old.nonce,payload:{}})).ok,false);
  assert.equal(cancel.ingested.length,0);

  const risk=fixture(); await risk.queue.startBackfill({all:true});
  const rworker={tab:{id:20,url:risk.items[0].url},url:risk.items[0].url};
  const job=(await risk.queue.context(rworker)).job;
  await risk.queue.verificationPause(rworker,{taskId:job.taskId,nonce:job.nonce,pageUrl:job.url});
  await risk.queue.timeout(); assert.equal((await risk.queue.progress()).progress.processed,0);
  assert.equal((await risk.restart().context(rworker)).job.waiting_verification,true);
  await risk.queue.verificationResume(rworker,{taskId:job.taskId,pageUrl:job.url});
  await submit(risk); assert.equal((await risk.queue.progress()).progress.status,'done');

  const partial=fixture(); await partial.queue.startBackfill({all:true});
  partial.setResponse({ok:true,data:{ok:true,snapshot_rows_updated:1,backfill:{complete:false,missing:[{key:'level'}]}}});
  await submit(partial);
  assert.equal((await partial.queue.progress()).progress.incomplete,1);
  assert.equal((await partial.queue.progress()).progress.status,'partial');
  assert.equal(partial.storage['pxb7-backfill-results']['123456'].status,'incomplete');
  const failures=fixture(4); await failures.queue.startBackfill({all:true});
  failures.setResponse({ok:true,data:{ok:false,error:'latest-snapshot-changed'}});
  for(let i=0;i<3;i++) await submit(failures);
  assert.equal((await failures.queue.progress()).progress.status,'paused');
  assert.equal(failures.ingested.length,3);

  // 下架商品（2026-10-06 用户指令）：跳过并标记补录失败，不计连续失败、不停队。
  const delisted=fixture(5); await delisted.queue.startBackfill({all:true});
  delisted.setResponse({ok:true,data:{ok:false,error:'delisted'}});
  for(let i=0;i<5;i++) await submit(delisted);
  const dp=(await delisted.queue.progress()).progress;
  assert.equal(dp.status,'partial','连续下架不得暂停整队');
  assert.equal(dp.delisted,5); assert.equal(dp.consecutiveFailures,0);
  assert.equal(delisted.storage['pxb7-backfill-results']['123459'].status,'failed');
  assert.equal(delisted.storage['pxb7-backfill-results']['123459'].error,'delisted');

  // 完整看板发起的中转任务：空闲领取即建队列；忙碌拒领；启动失败回报网关。
  const bare=fixture();
  assert.equal((await bare.queue.claimBackfillJob()).err,'unsupported','未注入中转依赖时静默不可领');
  const claimed=fixture(2); let finishReport=null;
  claimed.deps.claimBackfillJob=async()=>({ok:true,data:{ok:true,items:claimed.items,backup:'fixture.duckdb'}});
  claimed.deps.finishBackfillJob=async r=>{finishReport=r;return {ok:true};};
  await claimed.queue.startBackfill({all:true});
  assert.equal((await claimed.queue.claimBackfillJob()).ok,false,'采集进行中不得领取看板任务');
  await claimed.queue.cancel();
  assert.equal((await claimed.queue.claimBackfillJob()).ok,true,'空闲时领取看板任务并开始执行');
  assert.equal((await claimed.queue.progress()).progress.mode,'backfill');
  assert.equal(finishReport?.ok,true,'领取成功回报网关');
  const broken=fixture(1); finishReport=null;
  broken.deps.claimBackfillJob=async()=>({ok:true,data:{ok:true,items:[{id:'bad'}],backup:'x.duckdb'}});
  broken.deps.finishBackfillJob=async r=>{finishReport=r;return {ok:true};};
  assert.equal((await broken.queue.claimBackfillJob()).ok,false,'无效清单拒领并回报失败');
  assert.equal(finishReport?.ok,false);

  const deletion=fixture(); let deletes=0;
  deletion.deps.deleteBackfill=async()=>{deletes++;return {ok:true,data:{ok:true,deleted_ids:['123456']}};};
  deletion.storage['pxb7-backfill-results']={'123456':{status:'failed'},'123457':{status:'failed'}};
  await deletion.queue.startBackfill({all:true});
  assert.equal((await deletion.queue.deleteBackfill({})).ok,false,'采集中不得删除');
  assert.equal(deletes,0);
  await deletion.queue.cancel();
  assert.equal((await deletion.queue.deleteBackfill({})).data.ok,true);
  assert.equal(deletion.storage['pxb7-backfill-results']['123456'],undefined);
  assert.equal(deletion.storage['pxb7-backfill-results']['123457'].status,'failed');
  let release;
  deletion.deps.deleteBackfill=async()=>{await new Promise(resolve=>release=resolve);return {ok:true,data:{ok:true,deleted_ids:[]}};};
  const deleting=deletion.queue.deleteBackfill({});
  await new Promise(resolve=>setImmediate(resolve));
  const queuedStart=deletion.queue.startBackfill({all:true});
  assert.equal((await deletion.queue.progress()).progress.status,'cancelled','删除与新采集串行');
  release(); await deleting; assert.equal((await queuedStart).ok,true);

  // 执行真实 background.js 消息路由，站点伪造启动不得触发网关请求。
  const bg=fixture(); let fetches=0, data={ok:true,results:[{ok:false,game_id:10302,error:'locked'}]};
  const context=vm.createContext({chrome:bg.chrome,URLSearchParams,AbortSignal,setTimeout,
    importScripts(){context.PXB7_COLLECTION={create};},
    async fetch(){fetches++;return {status:200,text:async()=>JSON.stringify(data)};}});
  vm.runInContext(fs.readFileSync(require.resolve('../extension/pxb7-extension/background.js'),'utf8'),context);
  const message=(msg,sender)=>new Promise(resolve=>bg.listener()(msg,sender,resolve));
  assert.equal((await message({type:'backfill-start',options:{all:true}},{id:popup.id,url:'https://www.pxb7.com/product/123456/1'})).ok,false);
  assert.equal(fetches,0);
  assert.equal((await message({type:'backfill-delete',options:{}},{id:popup.id,url:'https://www.pxb7.com/product/123456/1'})).ok,false);
  assert.equal(fetches,0,'内容脚本不能删除');
  bg.storage['pxb7-backfill-results']={'123456':{status:'failed'}};
  data={ok:true,deleted_ids:['123456'],results:[{ok:false,game_id:10302,error:'locked'}]};
  assert.equal((await message({type:'backfill-delete',options:{items:[{id:'123456'}],confirm_all_rounds:true}},popup)).data.ok,true);
  assert.equal(Object.keys((await message({type:'backfill-state'},popup)).results).length,0);
  await message({type:'backfill-sync',game_ids:[10302]},popup);
  assert.equal((await message({type:'backfill-state'},popup)).pending[10302].error,'locked');
  data={ok:true,analysis_sync:{ok:true,game_id:10302}};
  await message({type:'ingest',path:'/ingest/detail',payload:{}},{url:'https://www.pxb7.com/product/123456/1'});
  assert.equal(Object.keys((await message({type:'backfill-state'},popup)).pending).length,0);
  console.log('backfill queue and background: OK');
})().catch(e=>{console.error(e);process.exit(1);});
