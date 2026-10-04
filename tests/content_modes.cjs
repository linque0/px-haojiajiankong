const assert=require('node:assert/strict'), vm=require('node:vm'), fs=require('node:fs');
const code=fs.readFileSync('extension/pxb7-extension/content.js','utf8');
async function scenario(mode) {
  const sent=[], timers=[], handlers=[], storage=new Map(); let fullCalls=0, ids;
  const selected=Array.from({length:3},(_,i)=>({id:String(123456+i),url:`https://www.pxb7.com/product/${123456+i}/1`,
    card:{querySelector(){return {getAttribute(){return null;},textContent:'80级，10个五星角色：3命安可；1个五星武器：精1千古洑流'};}}}));
  const sandbox={console,Date,Promise,JSON,Number,String,
    location:{pathname:'/buy/10302/1',search:'',hash:'',href:'https://www.pxb7.com/buy/10302/1'},
    document:{documentElement:{outerHTML:'raw document'},body:{appendChild(){}},createElement(){return {style:{}};}},
    sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
    setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},setInterval(){},addEventListener(){},
    PXB7_SWEEP:{clampTarget:n=>n,countCards:()=>16,findMoreControl:()=>null,nextAction:()=> 'done-target',DEFAULTS:{},
      selectCards:(doc,n)=>{assert.equal(n,3);return selected;},capture:(doc,refs,titles)=>{assert.equal(refs.length,3);return 'only-3-cards';}},
    PXB7_TITLES:{async collect(doc,win,options){fullCalls++;ids=options.ids;
      return {cards:3,captured:3,titles:{},errors:[],stop:'done'};}},
    chrome:{runtime:{getManifest:()=>({version:'0.5.0'}),onMessage:{addListener:fn=>handlers.push(fn)},sendMessage(msg,cb){
      sent.push(msg);
      if(msg.type==='detail-context') return cb({ok:true,worker:false});
      if(msg.type==='config')return cb({ok:true,data:{ok:true,config:{auto_ingest:true,collection_mode:mode,cards_target:3,spa_settle_ms:1}}});
      if(msg.type==='collection-begin')return cb({ok:true,taskId:'task-3'});
      if(msg.type==='ingest')return cb({ok:true,data:{ok:true,round:'2026-10-04T09:00:00',cards_seen:3,cards_parsed:3}});
      return cb({ok:true});
    }}}
  };sandbox.window=sandbox;
  vm.runInNewContext(code,sandbox);
  await new Promise(setImmediate);
  while(timers.length){timers.shift()();await new Promise(setImmediate);}
  assert.equal(sent.filter(m=>m.type==='ingest').length,1);
  assert.equal(sent.find(m=>m.type==='ingest').payload.html,'only-3-cards');
  if(mode==='detail'){
    assert.equal(fullCalls,0,'详情模式不得调用标题补采接口');
    const job=sent.find(m=>m.type==='detail-start');assert.equal(job.items.length,3);
    assert.equal(job.round,'2026-10-04T09:00:00');assert.equal(job.items[0].id,'123456');
    assert.equal(storage.size,0,'队列完成前不能设置去重成功');
    handlers[0]({type:'detail-completed',url:sandbox.location.href},null,()=>{});
    assert.equal(storage.size,1);
  }else{
    assert.equal(fullCalls,1);assert.deepEqual(Array.from(ids),['123456','123457','123458']);
    assert.equal(sent.filter(m=>m.type==='detail-start').length,0);
    assert.equal(sent.filter(m=>m.type==='collection-progress').at(-1).status,'done');
    assert.equal(storage.size,1);
  }
}
(async()=>{await scenario('list');await scenario('detail');console.log('actual content.js list/detail flow: PASS');})()
 .catch(error=>{console.error(error);process.exitCode=1;});
