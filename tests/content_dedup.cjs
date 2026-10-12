// 执行实际内容脚本：超过200个旧账号、加载补足新增额度、无新增与本机查询失败。
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const sweep=require('../extension/pxb7-extension/sweep.js');
const content=fs.readFileSync('extension/pxb7-extension/content.js','utf8');
async function scenario(name) {
  const messages=[],timers=[];let loaded=0,titleIds=[],captured=[];
  const old=Array.from({length:205},(_,i)=>({id:String(100000+i),kind:'skip'}));
  const extra=[{id:'200001',kind:'changed'},{id:'200002',kind:'retry'},{id:'200003',kind:'manual'}];
  const fresh=Array.from({length:16},(_,i)=>({id:String(300000+i),kind:'new'}));
  function current(){return [...old,...(name==='all-old'?[]:extra),...(loaded&&name==='mixed'?fresh:[]),
    ...(name==='bounds'?Array.from({length:loaded},(_,i)=>({id:String(400000+i),kind:'skip'})):[])];}
  function refs(){return current().map(d=>({...d,url:`https://www.pxb7.com/product/${d.id}/1`,
    card:{querySelector:()=>({getAttribute:()=>null,textContent:'80级'})}}));}
  const sandbox={console,Date,Promise,JSON,Number,String,Map,Set,Infinity,
    location:{pathname:'/buy/10302/1',search:'',hash:'',href:'https://www.pxb7.com/buy/10302/1'},
    document:{documentElement:{outerHTML:'dom'},body:{appendChild(){}},createElement:()=>({style:{}})},
    sessionStorage:{getItem(){},setItem(){}},setTimeout(fn){timers.push(fn);},clearTimeout(){},setInterval(){},addEventListener(){},
    PXB7_SWEEP:{...sweep,DEFAULTS:{...sweep.DEFAULTS,intervalMs:0},selectCards:(doc,n)=>{assert.equal(n,Infinity);return refs();},
      findMoreControl:()=>({click(){loaded++;}}),capture:(doc,items)=>{captured=items.map(i=>i.id);return JSON.stringify(captured);}},
    PXB7_TITLES:{async collect(doc,win,options){titleIds=options.ids;return {cards:titleIds.length,captured:titleIds.length,titles:{},stop:'done'};}},
    chrome:{runtime:{getManifest:()=>({version:'0.5.12'}),onMessage:{addListener(){}},sendMessage(msg,cb){
      messages.push(msg);
      if(msg.type==='detail-context')return cb({ok:true,worker:false});
      if(msg.type==='config')return cb({ok:true,data:{ok:true,config:{auto_ingest:true,collection_mode:'list',cards_target:3,spa_settle_ms:1}}});
      if(msg.type==='collection-begin')return cb({ok:true,taskId:'task'});
      if(msg.path==='/collection/check')return cb(name==='error'?{ok:false,err:'offline'}:
        {ok:true,data:{ok:true,items:current().map(d=>({...d,snapshot_round:'2026-10-08T09:00:00'}))}});
      if(msg.path==='/ingest/cards'){
        const items=current().filter(d=>msg.payload.selected_ids.includes(d.id));
        return cb({ok:true,data:{ok:true,round:'2026-10-08T09:00:00',new_listings:items.filter(d=>d.kind==='new').length,
          accepted_ids:items.filter(d=>['new','changed'].includes(d.kind)).map(d=>d.id),skipped_ids:[],updated_ids:items.filter(d=>d.kind==='changed').map(d=>d.id),
          items:items.map(d=>({...d,snapshot_round:'2026-10-08T09:00:00',missing:[]}))}});
      }
      cb({ok:true});
    }}}
  };sandbox.window=sandbox;vm.runInNewContext(content,sandbox);
  await new Promise(setImmediate);
  for(let i=0;timers.length&&i<100;i++){timers.shift()();await new Promise(setImmediate);}
  if(name==='error'){
    assert.equal(messages.filter(m=>m.path==='/ingest/cards').length,0);
    assert.equal(messages.filter(m=>m.type==='detail-start').length,0);
    assert.equal(messages.filter(m=>m.type==='collection-progress').at(-1).status,'error');
  }else if(name==='mixed'){
    const payload=messages.find(m=>m.path==='/ingest/cards').payload;
    assert.deepEqual(Array.from(payload.selected_ids),['200001','200002','300000','300001','300002']);
    assert.deepEqual(Array.from(titleIds),['200001','300000','300001','300002'],'不为旧账号或缺项重试请求全文');
    assert.equal(loaded,1,'按新账号数量继续加载，而非在205个旧账号处停止');
    const job=messages.find(m=>m.type==='detail-start');
    assert.deepEqual(Array.from(job.items,d=>d.id),['200002','200001'],'缺项先重采，变更再采详情');
    assert.equal(job.new_succeeded,3);assert.equal(job.duplicates,205);assert.equal(job.manual_pending,1);
  }else if(name==='all-old'){
    assert.equal(loaded,2);assert.equal(titleIds.length,0);
    assert.equal(messages.filter(m=>m.type==='detail-start').length,0);
    assert.deepEqual(Array.from(messages.find(m=>m.path==='/ingest/cards').payload.selected_ids),[]);
    assert.equal(messages.filter(m=>m.type==='collection-progress').at(-1).status,'partial');
  }else if(name==='bounds'){
    assert.equal(loaded,12,'仍有更多旧账号也不能超过12轮加载上限');
    assert.equal(messages.find(m=>m.path==='/ingest/cards').payload.sweep.stop,'done-rounds');
  }else{
    assert.equal(loaded,2,'连续两轮无新商品停止');
    assert.ok(messages.some(m=>m.type==='detail-start'),'无新账号仍处理已发现更新/重试');
  }
}
(async()=>{for(const name of ['mixed','empty','error','all-old','bounds']) await scenario(name);console.log('candidate dedup/new quota/query failure: PASS');})()
.catch(e=>{console.error(e);process.exitCode=1;});
