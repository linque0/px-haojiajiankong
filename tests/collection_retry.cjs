const assert=require('node:assert/strict');
const {create}=require('../extension/pxb7-extension/collection.js');
const url='https://www.pxb7.com/buy/10302/1',round='2026-10-08T09:00:00';
const source={tab:{id:10,url},url};
const items=[{id:'123456',url:'https://www.pxb7.com/product/123456/1',kind:'new',snapshot_round:round},
  {id:'123457',url:'https://www.pxb7.com/product/123457/1',kind:'changed',snapshot_round:round}];
function fixture(){
  const storage={},claims=[],requests=[];let missing=true;
  const chrome={storage:{local:{async get(k){return structuredClone({[k]:storage[k]});},async set(v){Object.assign(storage,structuredClone(v));}}},
    alarms:{async create(){},async clear(){},onAlarm:{addListener(){}}},
    tabs:{async create(){return {id:20};},async update(){},async remove(){},async sendMessage(){},onRemoved:{addListener(){}}}};
  const deps={retry:async options=>{claims.push(options);return {ok:true,data:{ok:true,claimed:true}};}};
  const ingest=async(path,payload)=>{requests.push(payload);return {ok:true,data:{ok:true,snapshot_rows_updated:1,missing:missing?[{key:'level'}]:[]}};};
  return {queue:create(chrome,ingest,deps),restart:()=>create(chrome,ingest,deps),claims,requests,setMissing:v=>missing=v};
}
async function result(queue,id,error){
  const {job}=await queue.context({tab:{id:20},url:items[id].url});
  return queue.result({tab:{id:20}},{taskId:job.taskId,nonce:job.nonce,...(error?{error}:
    {payload:{listing_id:job.id,url:job.url,html:'detail'}})});
}
(async()=>{
  const f=fixture(),started=await f.queue.begin(source,{mode:'detail',total:1});
  await f.queue.start(source,{taskId:started.taskId,round,items});
  await result(f.queue,0);
  f.setMissing(false);await result(f.queue,1);
  const resumed=f.restart();
  const retryJob=(await resumed.context({tab:{id:20},url:items[0].url})).job;
  assert.equal(retryJob.kind,'retry');assert.ok(retryJob.retryToken);
  await resumed.result({tab:{id:20}},{taskId:started.taskId,nonce:retryJob.nonce,error:'verification'});
  assert.equal(f.claims.filter(c=>c.action==='claim').length,1,'验证暂停不再次领取补采次数');
  const worker={tab:{id:20,url:items[0].url},url:items[0].url};
  await resumed.verificationResume(worker,{taskId:started.taskId,pageUrl:items[0].url});
  await result(resumed,0);
  const p=(await resumed.progress()).progress;
  assert.equal(p.status,'done');assert.equal(p.new_succeeded,1);assert.equal(p.updated,1);assert.equal(p.retried,1);
  assert.equal(f.requests.length,3);assert.equal(f.requests.at(-1).retry_token,retryJob.retryToken);
  const bad=fixture(),b=await bad.queue.begin(source,{mode:'detail',total:1});
  await bad.queue.start(source,{taskId:b.taskId,round,items:[items[0]]});
  await result(bad.queue,0);await result(bad.queue,0);
  const end=(await bad.queue.progress()).progress;
  assert.equal(end.status,'partial');assert.equal(end.manual_pending,1);assert.equal(end.retried,1);
  assert.equal(bad.requests.length,2,'一轮后仍缺失不无限重试');
  console.log('one automatic retry/restart/verification/new quota: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
