const assert = require('node:assert/strict');
const {create} = require('../extension/pxb7-extension/collection.js');
const {clampTarget} = require('../extension/pxb7-extension/sweep.js');
const source = {tab:{id:10, url:'https://www.pxb7.com/buy/10302/1'}, url:'https://www.pxb7.com/buy/10302/1'};
const items = Array.from({length:17}, (_,i) => ({id:String(123456+i),url:`https://www.pxb7.com/product/${123456+i}/1`,prefix:'80级'}));
function fixture(deps) {
  const storage = {}, navigations=[], closed=[], ingested=[], messages=[], alarms={};
  const chrome = {
    storage:{local:{async get(k){return structuredClone({[k]:storage[k]});},async set(v){Object.assign(storage,structuredClone(v));}}},
    alarms:{async create(k,v){alarms[k]=v;},async clear(k){delete alarms[k];},onAlarm:{addListener(){}}},
    tabs:{async create(){return {id:20};},async update(id,opt){navigations.push({id,...opt});},
      async remove(id){closed.push(id);},async sendMessage(id,msg){messages.push({id,...msg});},onRemoved:{addListener(){}}},
  };
  let response = {ok:true,data:{ok:true,snapshot_rows_updated:1}};
  const ingest = async (path, payload) => {ingested.push({path,payload});return response;};
  return {queue:create(chrome,ingest,deps), restart:()=>create(chrome,ingest,deps), navigations, closed, ingested, messages, alarms,
    setResponse:v=>response=v};
}
async function start(f, n=17, extra) {
  const begin = await f.queue.begin(source,{mode:'detail',total:n});
  assert.equal(begin.ok,true);
  const reply = await f.queue.start(source,{taskId:begin.taskId,items:items.slice(0,n),round:'2026-10-04T09:00:00',...extra});
  assert.equal(reply.ok,true);
  return begin.taskId;
}
async function ready(queue, url) {
  return (await queue.context({tab:{id:20,url},url})).job;
}
(async()=>{
  for (const n of [1,8,17,31,200]) assert.equal(clampTarget(n),n);
  assert.equal(clampTarget(undefined),16); assert.equal(clampTarget(999),200);
  const f = fixture(), id = await start(f);
  assert.equal((await f.queue.begin(source,{mode:'list',total:1})).ok,false,'同时只能一个任务');
  const first = await ready(f.queue,items[0].url);
  assert.equal(first.id,items[0].id); assert.equal(first.snapshot_round,'2026-10-04T09:00:00');
  assert.equal((await f.queue.context({tab:{id:999},url:items[0].url})).worker,false);
  await f.queue.result({tab:{id:20}}, {taskId:id,nonce:first.nonce,payload:{listing_id:first.id,url:first.url,html:'<div>完整标题</div>'}});
  assert.equal(f.ingested[0].payload.snapshot_round,'2026-10-04T09:00:00');
  assert.equal(f.navigations.length,2);
  const stale = await f.queue.result({tab:{id:20}}, {taskId:id,nonce:first.nonce,payload:{}});
  assert.equal(stale.ok,false); assert.equal(f.ingested.length,1,'过期/重复结果不得入库');
  const recovered = f.restart(); // 新 SW 使用相同持久化状态，无需原任务的 Promise/弹窗
  for(let i=1;i<17;i++) {
    const job = await ready(recovered, items[i].url);
    await recovered.result({tab:{id:20}}, {taskId:id,nonce:job.nonce,payload:{listing_id:job.id,url:job.url,html:'完整标题'}});
  }
  const done = (await recovered.progress()).progress;
  assert.equal(done.status,'done'); assert.equal(done.processed,17); assert.equal(done.succeeded,17);
  assert.equal(f.ingested.length,17); assert.deepEqual(f.closed,[20]);
  assert.equal(f.messages[0].type,'detail-completed'); assert.ok(!done.items);

  const cancel = fixture(); await start(cancel,1); await cancel.queue.cancel();
  assert.equal((await cancel.queue.progress()).progress.status,'cancelled');
  assert.equal(cancel.ingested.length,0); assert.deepEqual(cancel.closed,[20]);
  const closed = fixture(); await start(closed,1); await closed.queue.removed(20);
  assert.equal((await closed.queue.progress()).progress.status,'cancelled');

  const risk = fixture(), riskId = await start(risk,1), riskJob = await ready(risk.queue,items[0].url);
  await risk.queue.result({tab:{id:20}}, {taskId:riskId,nonce:riskJob.nonce,error:'verification'});
  assert.equal((await risk.queue.progress()).progress.status,'paused'); assert.equal(risk.closed.length,0);
  assert.equal(risk.navigations.at(-1).active,true); assert.equal(risk.ingested.length,0);

  const timeout = fixture(); await start(timeout);
  for(let i=0;i<3;i++) await timeout.queue.timeout();
  assert.equal((await timeout.queue.progress()).progress.failed,3);
  assert.equal((await timeout.queue.progress()).progress.status,'paused');

  const bad = fixture(), badId = await start(bad,1), job = await ready(bad.queue,items[0].url);
  await bad.queue.result({tab:{id:20}}, {taskId:badId,nonce:job.nonce,payload:{listing_id:'999999',url:job.url,html:'x'}});
  assert.equal(bad.ingested.length,0); assert.equal((await bad.queue.progress()).progress.status,'partial');

  const list = fixture(), begin = await list.queue.begin(source,{mode:'list',total:8});
  await list.queue.update(source,{taskId:begin.taskId,total:8,processed:4,phase:'读取全文'});
  assert.equal((await list.queue.progress()).progress.processed,4);
  await list.queue.update({tab:{id:999}},{taskId:begin.taskId,processed:8,status:'done'});
  assert.equal((await list.queue.progress()).progress.processed,4,'其他标签页不能覆盖进度');
  await list.queue.update(source,{taskId:begin.taskId,processed:8,succeeded:7,failed:1,status:'partial',phase:'结束'});
  assert.equal((await list.queue.progress()).progress.status,'partial');
  const quality = fixture(); items[0].prefix='80级，10个五星角色';
  const qualityId = await start(quality,1), qualityJob = await ready(quality.queue,items[0].url);
  quality.setResponse({ok:true,data:{ok:true,snapshot_rows_updated:1,attributes:{five_star_weapons:15},weapon_details_complete:false}});
  await quality.queue.result({tab:{id:20}}, {taskId:qualityId,nonce:qualityJob.nonce,
    payload:{listing_id:qualityJob.id,url:qualityJob.url,html:'只有3条精炼'}});
  assert.equal((await quality.queue.progress()).progress.status,'partial');
  assert.equal((await quality.queue.progress()).progress.incomplete,1);
  assert.equal(quality.messages.length,0,'武器不完整不能设源页去重成功');

  // 详情采集间隔（2026-10-05 用户指令）：与列表模式互不共用；第 2 张起生效，0.1 秒粒度
  const paced = fixture({getConfig: async () => ({detail_interval_ms: 200})});
  const pacedId = await start(paced,2);                       // 配置 200ms
  const pacedJob = await ready(paced.queue,items[0].url);
  let t0 = Date.now();
  await paced.queue.result({tab:{id:20}}, {taskId:pacedId,nonce:pacedJob.nonce,
    payload:{listing_id:pacedJob.id,url:pacedJob.url,html:'完整标题'}});
  const elapsed = Date.now() - t0;
  assert.ok(elapsed >= 190, `第 2 张前应等待详情间隔（实际 ${elapsed}ms）`);
  assert.equal(paced.navigations.length,2,'间隔后仍推进到下一张');
  assert.equal((await paced.queue.progress()).progress.succeeded,1);
  // 消息显式携带优先于配置：0 覆盖配置 → 不等待
  const unpaced = fixture({getConfig: async () => ({detail_interval_ms: 5000})});
  const unpacedId = await start(unpaced,2,{detail_interval_ms:0});
  const unpacedJob = await ready(unpaced.queue,items[0].url);
  t0 = Date.now();
  await unpaced.queue.result({tab:{id:20}}, {taskId:unpacedId,nonce:unpacedJob.nonce,
    payload:{listing_id:unpacedJob.id,url:unpacedJob.url,html:'完整标题'}});
  assert.ok(Date.now() - t0 < 150, '显式 0 = 不等待');
  assert.equal(unpaced.navigations.length,2);
  // 非法/缺省回落 0（无 getConfig、配置为垃圾值）
  const bare = fixture(); const bareId = await start(bare,1,{detail_interval_ms:'fast'});
  assert.equal((await bare.queue.progress()).progress.detail_interval_ms,0);
  const fromConfig = fixture({getConfig: async () => ({detail_interval_ms: 3500.4})});
  await start(fromConfig,1);
  assert.equal((await fromConfig.queue.progress()).progress.detail_interval_ms,3500,'0.1 秒粒度取整');

  // 重复账号检查：队列内按商品编号去重，计数可见且不入库
  const dup = fixture();
  const dupList = [items[0], items[1], {...items[1]}, {...items[1]}, items[2]];
  const dupBegin = await dup.queue.begin(source,{mode:'detail',total:5});
  await dup.queue.start(source,{taskId:dupBegin.taskId,items:dupList,round:'2026-10-04T09:30:00'});
  const dupProgress = (await dup.queue.progress()).progress;
  assert.equal(dupProgress.duplicates,2,'重复商品计数 = 2');
  assert.equal(dupProgress.total,3,'去重后仅 3 个唯一商品');
  console.log('detail interval pacing and duplicate guard: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
