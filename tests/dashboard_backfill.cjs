// 实际 dashboard.html：完整看板查缺补录区块与弹窗同功能——清单/台账/全选/排队补录/删除/任务状态。
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
class Element {
  constructor(id){this.id=id;this.listeners={};this.value='';this.textContent='';this.style={};
    this.html='';this.disabled=false;this.hidden=false;this.className='';}
  addEventListener(type,fn){this.listeners[type]=fn;}
  set innerHTML(value){this.html=value;
    this.inputs=Array.from(value.matchAll(/<input type="checkbox" data-bf-id="(\d+)"([^>]*)>/g),m=>{
      const input=new Element(m[1]);input.dataset={bfId:m[1]};input.checked=m[2].includes('checked');return input;});}
  get innerHTML(){return this.html;}
  querySelector(sel){
    if(sel==='thead tr'||sel==='tbody') return this;
    if(sel.startsWith('[data-history-body=')) return historyBody;
    return null;
  }
  querySelectorAll(sel){return sel==='input[data-bf-id]' ? this.inputs||[] : [];}
  insertAdjacentHTML(){}
}
const historyBody=new Element('history-body');
const html=fs.readFileSync(require.resolve('../extension/dashboard.html'),'utf8');
for(const m of html.matchAll(/id="([^"]+)"/g)) elements.set(m[1],new Element(m[1]));
let rows=Array.from({length:12},(_,i)=>({id:String(223456+i),snapshot_round:'2026-10-06T19:30:00',
  game_id:10302,game_name:'鸣潮',collectable:i!==1,missing:[{label:'等级',reason:'缺失'}]}));
rows[0].last_backfill={snapshot_round:rows[0].snapshot_round,status:'failed',error:'delisted',at:'2026-10-06T20:00:00'};
let job=null, enqueued=null, deleted=null, allow=false;
const stats={ok:true,task:{task_id:'genshin_official',name:'原神'},
  stats:{batches:0,cards_seen:0,cards_parsed:0,new_listings:0,risk_pages_rejected:0,details_stored:0,last_batch_at:null},
  script:{},pending_details:0,recent_batches:[],targets:null,paths:null,risk:{tasks:{},global_stop:false},
  db:{snapshot_rows:0,listings:0,keyword_hits:0,rounds:[],price_histogram:[],top_keywords:[],top_keywords_by_game:{}}};
async function fetchMock(url,options) {
  const [path,query]=String(url).split('?');
  const params=new URLSearchParams(query||'');
  const body=options?.body ? JSON.parse(options.body) : {};
  const reply=data=>({status:200,json:async()=>data,text:async()=>JSON.stringify(data)});
  if(path==='/stats') return reply(stats);
  if(path==='/config') return reply({ok:true,config:{}});
  if(path==='/listings') return reply(params.get('mode')==='history'?{ok:true,total:1,games:[],columns:[],show_viewers:false,
    rows:[{listing_id:'223456',game_id:10302,game_name:'鸣潮',round:'2026-10-08T09:00',price:250,
      versions:17,changed_at:'2026-10-08T09:01:00',cells:{}}]}:{ok:true,total:0,rows:[],games:[],columns:[]});
  if(path==='/listings/history') return reply({ok:true,listing_id:'223456',offset:Number(params.get('offset')),
    limit:15,total:17,rows:[{version:17,captured_at:'2026-10-08T09:01:00',source:'detail',
      data:{title:'<script>unsafe</script>',price_yuan:250},diff:[{field:'price_yuan',before:300,after:250,kind:'change'},
        {field:'server',before:null,after:'官服',kind:'supplement'}]}]});
  if(path==='/backfill/candidates'){
    const offset=Number(params.get('offset')||0), limit=Number(params.get('limit')||10);
    return reply({ok:true,rows:rows.slice(offset,offset+limit),total:rows.length,
      games:[{game_id:10302,game_name:'鸣潮',total:12,missing:rows.length}],
      coverage:{'10302':['等级','黄数','资源','区服']}});
  }
  if(path==='/backfill/job') return reply({ok:true,job});
  if(path==='/backfill/enqueue'){
    enqueued=body; job={status:'pending',total:body.items.length,backup:'fixture.duckdb',created_at:'2026-10-06T20:30:00'};
    return reply({ok:true,total:body.items.length,backup:'fixture.duckdb'});
  }
  if(path==='/backfill/delete'){
    deleted=body;const ids=body.items.map(item=>item.id);rows=rows.filter(row=>!ids.includes(row.id));job=null;
    return reply({ok:true,deleted_ids:ids,deleted:ids.length,
      counts:{fct_listing_snapshot:ids.length*2},backup:'fixture.duckdb',results:[{ok:true,game_id:10302,rows:0}]});
  }
  if(path==='/backfill/sync') return reply({ok:true,results:[]});
  return reply({ok:false,error:'unknown-path'});
}
const context=vm.createContext({console,Date,Math,JSON,Promise,URL,URLSearchParams,Map,Set,
  setInterval(){},clearInterval(){},setTimeout(fn){setImmediate(fn);},clearTimeout(){},
  location:{origin:'http://127.0.0.1:8765'},
  document:{getElementById:id=>{assert(elements.has(id),id);return elements.get(id);}},
  window:{confirm(){return allow;}},
  navigator:{},
  fetch:fetchMock});
vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const click=id=>elements.get(id).listeners.click();
const check=id=>{const input=elements.get('bf-list').inputs.find(input=>input.dataset.bfId===id);
  input.checked=true;input.listeners.change();};
(async()=>{
  await settle();await settle();await settle();await settle();
  assert.equal(elements.get('bf-total').textContent,'12 条待补录');
  assert.match(elements.get('bf-coverage').textContent,/额外付费商品允许为空，不计缺失/);
  assert.match(elements.get('bf-list').html,/商品已下架，无法补录/,'台账最近补录（下架）进清单');
  assert.match(elements.get('bf-list').html,/223456/);
  // 全选/取消全选（与弹窗一致）
  click('bf-all');await settle();await settle();
  assert.equal(elements.get('bf-delete').textContent,'删除选中（12）','全选覆盖当前范围全部页');
  assert.equal(elements.get('bf-all').textContent,'取消全选');
  click('bf-all');await settle();await settle();
  assert.equal(elements.get('bf-delete').textContent,'删除选中（0）','再次按下即取消全选');
  // 补录选中：未启用目标跳过；经网关排队并显示任务状态
  check('223456');check('223457');
  click('bf-backfill');await settle();await settle();await settle();
  assert.equal(enqueued.items.length,1,'未启用采集目标的选择被跳过');
  assert.match(elements.get('bf-note').textContent,/已排队 1 条/);
  assert.match(elements.get('bf-job').textContent,/等待扩展领取/);
  // 删除选中：确认后整批删除、行即时移除、清单强刷
  check('223458');allow=true;
  click('bf-delete');await settle();await settle();await settle();
  assert.equal(deleted.confirm_all_rounds,true);
  assert.equal(deleted.items[0].id,'223458');
  assert.equal(elements.get('bf-total').textContent,'11 条待补录','删除后清单立即更新');
  assert.equal(elements.get('bf-delete').textContent,'删除选中（0）');
  assert.equal(elements.get('bf-job').textContent,'');
  elements.get('l-mode').value='history';elements.get('l-mode').listeners.change();
  await settle();await settle();
  assert.equal(elements.get('l-total').textContent,'共 1 个账号');
  assert.match(elements.get('listings').html,/17 个版本/);
  elements.get('listings').listeners.toggle({target:{dataset:{historyId:'223456'},open:true}});
  await settle();await settle();
  assert.match(historyBody.html,/挂牌价/);assert.match(historyBody.html,/300/);assert.match(historyBody.html,/250/);
  assert.match(historyBody.html,/补齐信息/);assert.match(historyBody.html,/&lt;script&gt;/);
  assert.ok(!historyBody.html.includes('<script>unsafe'),'历史标题必须转义');
  elements.get('listings').listeners.click({target:{closest:()=>({disabled:false,dataset:{historyPage:'223456',offset:'15'}})}});
  await settle();await settle();
  assert.match(historyBody.html,/第 2 页/);
  console.log('dashboard backfill section: OK');
})().catch(error=>{console.error(error);process.exit(1);});
