const assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const {create} = require('../extension/pxb7-extension/collection.js');
const code = fs.readFileSync('extension/pxb7-extension/content.js', 'utf8');
async function scenario(mode) {
  const handlers = [], timers = [], storage = {}, sent = [], navigations = [];
  const url = 'https://www.pxb7.com/buy/10302/1';
  const sender = {tab:{id:10,url},url};
  let release, calls = 0;
  const chrome = {
    storage:{local:{async get(k){return structuredClone({[k]:storage[k]});},async set(v){Object.assign(storage,structuredClone(v));}}},
    alarms:{async create(){},async clear(){},onAlarm:{addListener(){}}},
    tabs:{async create(){return {id:20};},async update(id,options){navigations.push({id,...options});},async remove(){},
      async sendMessage(id,msg){return message(msg);},onRemoved:{addListener(){}}},
    runtime:{getManifest:()=>({version:'test'}),onMessage:{addListener:fn=>handlers.push(fn)}}
  };
  const queue = create(chrome, async()=>({ok:true,data:{ok:true,snapshot_rows_updated:1}}));
  chrome.runtime.sendMessage = (msg,cb) => {
    sent.push(msg);
    if(msg.type==='config') return cb({ok:true,data:{ok:true,config:{auto_ingest:false,collection_mode:mode,cards_target:1}}});
    if(msg.type==='ingest'&&msg.path==='/collection/check') return cb({ok:true,data:{ok:true,items:[{id:'123456',kind:'new'}]}});
    if(msg.type==='ingest') return cb({ok:true,data:{ok:true,cards_parsed:1,new_listings:1,accepted_ids:['123456'],skipped_ids:[],updated_ids:[],
      items:[{id:'123456',kind:'new',missing:[],snapshot_round:'2026-10-06T19:00:00'}],round:'2026-10-06T19:00:00'}});
    const routes = {'detail-context':'context','collection-begin':'begin','collection-progress':'update','detail-start':'start'};
    if(routes[msg.type]) queue[routes[msg.type]](sender,msg).then(cb);
    else cb({ok:true});
  };
  function message(msg) { return new Promise(resolve=>handlers[0](msg,null,resolve)); }
  const card = {querySelector:()=>({getAttribute:()=>null,textContent:'80级'})};
  const sandbox = {console,Date,Promise,JSON,Number,String,chrome,
    location:{pathname:'/buy/10302/1',search:'',hash:'',href:url},
    document:{documentElement:{outerHTML:'list'},body:{appendChild(){}},createElement:()=>({style:{}})},
    sessionStorage:{getItem(){},setItem(){}},setTimeout(fn){timers.push(fn);},clearTimeout(){},setInterval(){},addEventListener(){},
    PXB7_SWEEP:{clampTarget:n=>n,countCards:()=>1,nextAction:()=> 'done-target',findMoreControl:()=>null,DEFAULTS:{},
      selectCards:()=>[{id:'123456',url:'https://www.pxb7.com/product/123456/1',card}],capture:()=> 'list'},
    PXB7_TITLES:{async collect(doc,win,options){
      calls++;
      if(calls===1) await new Promise(resolve=>release=resolve);
      return {cards:1,captured:1,titles:{},stop:options.alive()?'done':'route-change'};
    }}
  };
  sandbox.window=sandbox;
  vm.runInNewContext(code,sandbox);
  await new Promise(setImmediate);
  const first = message({type:'collect-now'});
  await new Promise(setImmediate);
  if(mode==='detail') await first;
  const oldId = (await queue.progress()).progress.id;
  await queue.cancel();
  assert.equal((await queue.progress()).progress.status,'cancelled');
  const restart = message({type:'collect-now'});
  if(mode==='list') {release();assert.equal((await first).ok,false);}
  assert.equal((await restart).ok,true,'取消后立即重采必须成功，不要求刷新源页');
  const p = (await queue.progress()).progress;
  assert.notEqual(p.id,oldId);
  assert.equal(p.status,mode==='detail'?'running':'done');
  if(mode==='detail') assert.ok(navigations.some(n=>n.id===10&&n.active===true),'取消工作页后应返回源列表');
  assert.equal(sent.filter(m=>m.path==='/ingest/cards').length,mode==='detail'?2:1,'已取消的读取不得提交');
}
(async()=>{await scenario('list');await scenario('detail');console.log('cancel then restart actual content/queue: PASS');})()
.catch(e=>{console.error(e);process.exitCode=1;});
