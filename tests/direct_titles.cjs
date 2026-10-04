const vm=require('vm'), fs=require('fs'), path=require('path'), assert=require('assert');
const root=path.resolve(process.argv[2]), T=require(path.join(root,'extension/pxb7-extension/titles.js'));
const short='80级，20黄，19个五星角色：3命角色甲，角色乙，角色丙';
const full=short+'；2个五星武器：精1武器A，精2武器B';
const flush=()=>new Promise(setImmediate);
function harness(results=[], listData={list:[]}, count=2){
  let time=10000;const listeners={},calls=[],emitted=[];
  const cards=Array.from({length:count},(_,i)=>i===0?'123456':i===1?'234567':String(300000+i)).map((id,index)=>{
    const title={textContent:short, getAttribute(name){return name==='productuniqueno'?'CODE'+index:null;}};
    return {isConnected:true,querySelector(){return title;},getAttribute(){return id;}};
  });
  const response=(status,data)=>({status,ok:status>=200&&status<300,headers:new Headers({'content-type':'application/json'}),
    async json(){return data;},clone(){return response(status,data);}});
  const win={location:{href:'https://www.pxb7.com/buy/10302/1'},document:{querySelectorAll(s){return s.includes('middleCard')?cards:[];}},
    setTimeout(fn,ms){if(ms!==15000&&ms!==120000){time+=ms;queueMicrotask(fn);}return 1;},clearTimeout(){},
    addEventListener(n,fn){(listeners[n]??=[]).push(fn);},removeEventListener(n,fn){listeners[n]=(listeners[n]||[]).filter(f=>f!==fn);},
    dispatchEvent(e){emitted.push(e);for(const fn of [...listeners[e.type]||[]])fn(e);},
    CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}},
    async fetch(url,init){calls.push({url,init,time});
      if(!url.endsWith('selectTitleByCode'))return response(200,{success:true,data:listData});
      const next=results.shift()??[200,{success:true,data:{showTitle:full}}];
      if(next instanceof Error)throw next;
      return response(...next);
    }
  };
  vm.runInNewContext(fs.readFileSync(path.join(root,'extension/pxb7-extension/title-source.js'),'utf8'),
    {window:win,URL,Headers,AbortController,Date:{now:()=>time},Map,WeakMap,JSON,Array,Object,String,Number,Promise});
  async function ready(){
    // 通过实际 fetch 包装器观察正常公开列表响应。
    await win.fetch('https://api-pc.pxb7.com/api/search/product/selectPageList',
      {headers:{'client_type':'0','px-authorization-user':'session-secret','x-unrelated':'ignored'}});
    await flush();
  }
  return {win,calls,emitted,cards,ready,response};
}
(async()=>{
  let h=harness();
  let result=await T.collect(h.win.document,h.win);
  assert.equal(result.stop,'session-not-ready');assert.equal(h.calls.length,0);
  await h.ready();
  result=await T.collect(h.win.document,h.win);
  assert.equal(result.captured,2);assert.equal(result.requested,2);
  let calls=h.calls.filter(c=>c.url.endsWith('selectTitleByCode'));
  assert.equal(calls.length,2);assert.ok(calls[1].time-calls[0].time>=3000);
  assert.deepEqual(calls.map(c=>JSON.parse(c.init.body)),[{productUniqueNo:'CODE0'},{productUniqueNo:'CODE1'}]);
  assert.equal(calls[0].init.headers.get('px-authorization-user'),'session-secret');
  assert.equal(calls[0].init.headers.get('x-unrelated'),null);
  assert.ok(!h.emitted.some(e=>/mouse|click/.test(e.type)));
  assert.ok(!h.emitted.some(e=>e.detail.includes('session-secret')));
  result=await T.collect(h.win.document,h.win);
  assert.equal(result.captured,2);assert.equal(result.requested,0);
  assert.equal(h.calls.filter(c=>c.url.endsWith('selectTitleByCode')).length,2);

  h=harness([],{records:['123456','234567'].map((id,index)=>({productBasicInfo:
    {productId:id,productUniqueNo:'CODE'+index,productName:full,smallImgShowTitle:short}}))});
  await h.ready();result=await T.collect(h.win.document,h.win);
  assert.equal(result.captured,2);assert.equal(result.requested,0);assert.equal(h.calls.length,1);
  // 相同前缀的不同商品以 ID 关联；缓存不匹配当前文案时回到标题接口。
  h=harness([],{records:[{productId:'123456',productUniqueNo:'CODE0',productName:'其他商品全文'}]});
  await h.ready();result=await T.collect(h.win.document,h.win);
  assert.equal(result.captured,2);assert.equal(result.requested,2);

  for(const status of [429,403]){
    h=harness([[status,{success:false}]]);await h.ready();
    result=await T.collect(h.win.document,h.win);
    assert.equal(result.stop,status===429?'rate-limit':'verification');assert.equal(result.captured,0);
    assert.equal(h.calls.filter(c=>c.url.endsWith('selectTitleByCode')).length,1);
  }
  h=harness([[503,{}],[200,{success:true,data:{showTitle:full}}]]);await h.ready();
  result=await T.collect(h.win.document,h.win);
  calls=h.calls.filter(c=>c.url.endsWith('selectTitleByCode'));
  assert.equal(result.captured,2);assert.equal(calls.length,3);assert.ok(calls[1].time-calls[0].time>=6000);
  h=harness([new Error('network'),new Error('network')]);await h.ready();
  result=await T.collect(h.win.document,h.win);
  assert.equal(result.failed,0);assert.equal(result.captured,2);assert.equal(result.retried,1);
  assert.equal(h.calls.filter(c=>c.url.endsWith('selectTitleByCode')).length,4);
  h=harness([[200,{success:true,data:{showTitle:''}}]]);await h.ready();
  result=await T.collect(h.win.document,h.win);
  assert.equal(result.failed,0);assert.equal(result.captured,2);assert.equal(result.retried,1);
  // 重现原批次：8张成功后3张空响应，不能让剩余5张永远不被尝试。
  h=harness([...Array(8).fill([200,{success:true,data:{showTitle:full}}]),
    ...Array(3).fill([200,{success:true,data:{showTitle:''}}]),
    ...Array(8).fill([200,{success:true,data:{showTitle:full}}])],{list:[]},16);
  await h.ready();result=await T.collect(h.win.document,h.win);
  assert.equal(result.captured,16);assert.equal(result.failed,0);assert.equal(result.retried,3);
  calls=h.calls.filter(c=>c.url.endsWith('selectTitleByCode'));
  assert.equal(calls.length,19);assert.ok(calls[8].time-calls[7].time>=8000);
  // 用户配置的6秒间隔实际传递到MAIN，不与去重间隔混用。
  h=harness();await h.ready();result=await T.collect(h.win.document,h.win,{intervalMs:6000});
  calls=h.calls.filter(c=>c.url.endsWith('selectTitleByCode'));
  assert.ok(calls[1].time-calls[0].time>=6000);
  // 持续网络失败有界停止并保留逐项原因。
  h=harness(Array(6).fill(new Error('network')),{list:[]},16);
  await h.ready();result=await T.collect(h.win.document,h.win);
  assert.equal(result.stop,'unavailable');assert.equal(result.failed,3);assert.equal(result.errors.length,3);
  assert.equal(h.calls.filter(c=>c.url.endsWith('selectTitleByCode')).length,6);
  h=harness();await h.ready();let alive=true;
  result=await T.collect(h.win.document,h.win,{alive:()=>alive,request:async()=>{alive=false;return {title:full};}});
  assert.equal(result.stop,'route-change');assert.equal(result.captured,0);
  h=harness();await h.ready();
  result=await T.collect(h.win.document,h.win,{request:async()=>{h.cards[0].isConnected=false;return {title:full};}});
  assert.equal(result.stop,'list-change');assert.equal(result.captured,0);
  h=harness();await h.ready();
  result=await T.collect(h.win.document,h.win,{request:async()=>({title:'错误商品：99个五星武器'})});
  assert.equal(result.captured,0);assert.equal(result.failed,2);
})().catch(e=>{console.error(e);process.exitCode=1;});
