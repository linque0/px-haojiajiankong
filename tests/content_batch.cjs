const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {create}=require('../extension/pxb7-extension/collection.js');
async function scenario(count, fallback=false, stop='done') {
  const url='https://www.pxb7.com/buy/10302/1',sender={tab:{id:10,url},url};
  const storage={},handlers=[],sent=[],closed=[],timers=[],stamps=[];
  const refs=Array.from({length:count},(_,i)=>({id:String(123456+i),url:`https://www.pxb7.com/product/${123456+i}/1`,
    card:{querySelector:()=>({getAttribute:n=>n==='productuniqueno'?'CODE'+i:null,textContent:'80级，0个五星角色；0个五星武器；0星声；详情看图'})}}));
  const frozen={collection_mode:'auto',purpose:'discover',target_count:count,scan_budget:10000,max_scan_rounds:12,
    detail_interval_ms:0,title_interval_ms:3000,verification_mode:'manual'};
  const chrome={storage:{local:{async get(k){return structuredClone({[k]:storage[k]});},async set(v){Object.assign(storage,structuredClone(v));}}},
    alarms:{async create(){},async clear(){},onAlarm:{addListener(){}}},
    tabs:{async create(){return {id:20};},async update(){},async remove(id){closed.push(id);},async sendMessage(){},onRemoved:{addListener(){}}},
    runtime:{getManifest:()=>({version:'test'}),onMessage:{addListener:fn=>handlers.push(fn)}}};
  const queue=create(chrome,async(path,payload)=>({ok:true,data:{ok:true,snapshot_rows_updated:1,missing:[]}}),{
    createBatch:async()=>({ok:true,data:{ok:true,batch_id:'batch',claim_epoch:'epoch',config:{...frozen}}}),
    batchProgress:async payload=>{stamps.push(payload);return {ok:true,data:{ok:true}};},
    batchAccess:async()=>({ok:true,data:{ok:true}}),flushBatch:async()=>({ok:true,data:{ok:true,results:[{ok:true}]}})});
  chrome.runtime.sendMessage=(msg,cb)=> {
    sent.push(msg);
    if(msg.type==='config')return cb({ok:true,data:{ok:true,config:{auto_ingest:false,collection_mode:'auto',target_count:count,cards_target:10}}});
    if(msg.type==='ingest') {
      const ids=msg.payload.selected_ids||[];
      if(msg.path==='/collection/check')return cb({ok:true,data:{ok:true,items:ids.map(id=>({id,kind:'new'}))}});
      if(msg.path==='/collection/text-cache/query')return cb({ok:true,data:{ok:true,items:{}}});
      if(msg.path==='/collection/text-cache/store')return cb({ok:true,data:{ok:true}});
      if(msg.path==='/collection/text')return cb({ok:true,data:{ok:true,items:msg.payload.items.map(i=>({id:i.id,complete:!fallback||i.id!==refs.at(-1).id,missing:fallback&&i.id===refs.at(-1).id?[{key:'server'}]:[]}))}});
      assert.equal(msg.path,'/ingest/cards');
      return cb({ok:true,data:{ok:true,round:'2026-10-13T09:00:00',new_listings:ids.length,accepted_ids:ids,updated_ids:[],
        items:ids.map(id=>({id,kind:'new',snapshot_round:'2026-10-13T09:00:00',missing:[{key:'introduction'}]}))}});
    }
    const methods={'detail-context':'context','collection-begin':'begin','collection-progress':'update','detail-start':'start','collection-access':'authorize'};
    if(methods[msg.type])return queue[methods[msg.type]](sender,msg).then(cb);
    cb({ok:true});
  };
  const sandbox={console,Date,Promise,JSON,Number,String,chrome,location:{pathname:'/buy/10302/1',search:'',hash:'',href:url},
    document:{body:{appendChild(){}},createElement:()=>({style:{}})},
    sessionStorage:{getItem(){},setItem(){}},setTimeout(fn){timers.push(fn);},clearTimeout(){},setInterval(){},addEventListener(){},
    PXB7_SWEEP:{clampTarget:n=>n,selectCards:()=>refs,capture:(doc,selected)=>JSON.stringify(selected.map(i=>i.id)),
      DEFAULTS:{maxStalled:2,intervalMs:2000},nextAction:()=> 'done-target',findMoreControl:()=>null},
    PXB7_TITLES:{async collect(doc,win,options){
      const last=sent.filter(m=>m.path==='/ingest/cards').at(-1);
      assert.ok(last,'全文请求之前必须保存价格');assert.equal(last.payload.selected_ids.length,options.ids.length);
      return {stop,attempts:options.ids.length,cards:options.ids.length,captured:options.ids.length,
        titles:Object.fromEntries(options.ids.map(id=>[id,'80级，0个五星角色；0个五星武器；0星声；完整介绍尾部，详情看图'])),provenance:{}};
    }}
  };sandbox.window=sandbox;
  vm.runInNewContext(fs.readFileSync('extension/pxb7-extension/content.js','utf8'),sandbox);
  await new Promise(setImmediate);
  const reply=await new Promise(resolve=>handlers[0]({type:'collect-now'},null,resolve));
  assert.ok(reply.ok,JSON.stringify(reply));await new Promise(setImmediate);
  const p=(await queue.progress()).progress;
  assert.equal(p.requested,count,'逻辑目标不能静默截成200或试点10');
  const ingests=sent.filter(m=>m.path==='/ingest/cards');
  if(stop==='done')assert.deepEqual(ingests.map(m=>m.payload.selected_ids.length), count===450?[200,200,50]:[count]);
  assert.equal(new Set(ingests.flatMap(m=>m.payload.selected_ids)).size,ingests.flatMap(m=>m.payload.selected_ids).length);
  if(stop!=='done') {assert.equal(p.status,'partial');assert.equal(sent.filter(m=>m.type==='detail-start').length,0);}
  else if(fallback) {
    assert.equal(sent.filter(m=>m.type==='detail-start').length,1);
    const workerUrl=refs.at(-1).url;
    const job=(await queue.context({tab:{id:20,url:workerUrl},url:workerUrl})).job;
    assert.equal(job.id,refs.at(-1).id);
    await queue.result({tab:{id:20}},{taskId:p.id,nonce:job.nonce,payload:{listing_id:job.id,url:workerUrl,html:'公开详情'}});
    assert.equal((await queue.progress()).progress.new_succeeded,count,'详情回退不能再次增加新增数');
    assert.deepEqual(closed,[20]);
  } else {assert.equal(p.status,'done');assert.equal(sent.filter(m=>m.type==='detail-start').length,0);}
  assert.ok(stamps.length>0,'冻结批次与终态应进入本地台账');
}
async function reconcileAfterLocalCancel() {
  const saved={status:'needs_reconcile',batch:{batch_id:'batch',claim_epoch:'epoch'},items:[],processed:0};
  let navigations=0;
  const chrome={storage:{local:{async get(k){return {[k]:structuredClone(saved)};},async set(v){Object.assign(saved,Object.values(v)[0]);}}},
    alarms:{onAlarm:{addListener(){}}},tabs:{onRemoved:{addListener(){}},async update(){navigations++;}}};
  const queue=create(chrome,async()=>{throw Error('不应重采');},{batchProgress:async()=>({data:{ok:true,status:'cancelled'}}),
    flushBatch:async()=>({data:{ok:true}})});
  assert.equal((await queue.progress()).progress.status,'cancelled','网关对账取消必须释放扩展旧忙碌状态');
  assert.equal(navigations,0,'对账补报只处理本地状态');
}
async function completionRequiresGatewayQuality() {
  const source={tab:{id:10,url:'https://www.pxb7.com/buy/10302/1'},url:'https://www.pxb7.com/buy/10302/1'};
  const saved={id:'task',sourceTab:10,sourceUrl:source.url,status:'running',batch:{batch_id:'batch',claim_epoch:'epoch'}};
  let notified=0;
  const chrome={storage:{local:{async get(k){return {[k]:structuredClone(saved)};},async set(v){Object.assign(saved,Object.values(v)[0]);}}},
    alarms:{async clear(){},onAlarm:{addListener(){}}},tabs:{onRemoved:{addListener(){}},async sendMessage(){notified++;}}};
  const queue=create(chrome,async()=>({}),{batchProgress:async()=>({data:{ok:true,status:'partial'}}),flushBatch:async()=>({data:{ok:true}})});
  await queue.update(source,{taskId:'task',status:'done',phase:'任务完成'});
  assert.equal((await queue.progress()).progress.status,'partial');
  assert.equal(notified,0,'未完成不能发详情完成消息或标记源页去重');
}
(async()=>{await scenario(450);await scenario(3,true);await scenario(3,false,'rate-limit');await reconcileAfterLocalCancel();await completionRequiresGatewayQuality();console.log('actual content/queue: frozen logical blocks, price first, selective fallback, rate-limit stop, local reconcile and quality: PASS');})()
  .catch(error=>{console.error(error);process.exitCode=1;});
