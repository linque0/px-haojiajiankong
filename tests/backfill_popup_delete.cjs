// 实际 popup.js：跨页选择、失败快捷勾选、确认取消/提交、旧轮次选择与未启用游戏。
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
class Element {
  constructor(id){this.id=id;this.listeners={};this.style={};this.value='';this.textContent='';this.html='';}
  addEventListener(type,fn){this.listeners[type]=fn;}
  set innerHTML(value){this.html=value;this.inputs=Array.from(value.matchAll(/<input type="checkbox" data-bf-id="(\d+)"([^>]*)>/g),m=>{
    const input=new Element(m[1]);input.dataset={bfId:m[1]};input.checked=m[2].includes('checked');return input;
  });}
  get innerHTML(){return this.html;}
  querySelectorAll(selector){return selector==='input[data-bf-id]' ? this.inputs||[] : [];}
  checkValidity(){return true;}
}
const html=fs.readFileSync(require.resolve('../extension/pxb7-extension/popup.html'),'utf8');
for(const match of html.matchAll(/id="([^"]+)"/g)) elements.set(match[1],new Element(match[1]));
elements.get('bf-game').value='selected';
let rows=Array.from({length:12},(_,i)=>({id:String(123456+i),snapshot_round:'2026-10-06T19:30:00',
  game_name:'鸣潮',game_id:10302,collectable:i!==1&&i!==3,missing:[{label:'等级',reason:'缺失'}]}));
const results={'123456':{status:'failed',snapshot_round:rows[0].snapshot_round,at:Date.now()},
  '123457':{status:'incomplete',snapshot_round:rows[1].snapshot_round,at:Date.now()},
  '123458':{status:'failed',snapshot_round:'旧轮次',at:Date.now()}};
let allow=false, confirms=[], requests=[], starts=[], startReply={ok:true,progress:{backup:'fixture.duckdb',mode:'backfill'}};
let queryGate=null;   // 置为未决 Promise 可模拟"在飞查询"，验证强刷不被 bfLoading 吞掉
const context=vm.createContext({console,Date,URL,Map,Promise,setInterval(){},setTimeout(){},
  document:{getElementById:id=>{assert(elements.has(id),id);return elements.get(id);}},
  window:{confirm(text){confirms.push(text);return allow;}},
  fetch:async()=>({status:200,text:async()=>JSON.stringify({ok:true,stats:{},db:{rounds:[]},recent_batches:[],config:{}})}),
  chrome:{runtime:{getManifest:()=>({version:'0.5.9'}),lastError:null,sendMessage:async(message,callback)=>{
    requests.push(message);let reply={ok:true};
    if(message.type==='backfill-query'){
      const snapshot=rows, limit=message.limit||10, offset=message.offset||0;
      if(queryGate) await queryGate;   // 请求已发出、行集已定型，模拟删除前的旧清单在飞
      reply={ok:true,data:{ok:true,rows:snapshot.slice(offset,offset+limit),
        total:snapshot.length,games:[{game_id:10302,game_name:'鸣潮',missing:snapshot.length}],coverage:{},can_backfill_all:false}};
    }
    if(message.type==='backfill-state') reply={ok:true,results,pending:{}};
    if(message.type==='backfill-start'){
      assert.equal(Array.isArray(message.options.items),true);
      starts.push(message.options.items.map(item=>item.id));
      reply=startReply;
    }
    if(message.type==='backfill-delete') {
      assert.equal(message.options.confirm_all_rounds,true);
      const ids=message.options.items.map(item=>item.id);rows=rows.filter(row=>!ids.includes(row.id));
      reply={ok:true,data:{ok:true,deleted_ids:ids,deleted:ids.length,counts:{fct_listing_snapshot:ids.length*2},backup:'fixture.duckdb'}};
    }
    if(callback) callback(reply);return reply;
  }}}});
vm.runInContext(fs.readFileSync(require.resolve('../extension/pxb7-extension/popup.js'),'utf8'),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const click=id=>elements.get(id).listeners.click();
const select=async id=>{const input=elements.get('bf-list').inputs.find(input=>input.dataset.bfId===id);input.checked=true;input.listeners.change();await settle();};
(async()=>{
  await settle();
  assert.match(elements.get('bf-coverage').textContent,/额外付费商品允许为空，不计缺失/);
  click('bf-check-failed');
  assert.equal(elements.get('bf-delete').textContent,'删除选中（2）');
  assert.equal(elements.get('bf-selected').disabled,false,'至少一条可补录即启用；未启用记录在提交时跳过');
  await click('bf-delete');
  assert.equal(requests.filter(msg=>msg.type==='backfill-delete').length,0,'取消确认不得删除');
  assert.match(confirms[0],/全部入库轮次/);
  click('bf-next');await settle();await select('123466');
  assert.equal(elements.get('bf-delete').textContent,'删除选中（3）','跨页选择保留');
  allow=true;await click('bf-delete');
  assert.equal(JSON.stringify(requests.find(msg=>msg.type==='backfill-delete').options.items.map(item=>item.id)),JSON.stringify(['123456','123457','123466']));
  assert.equal(elements.get('bf-total').textContent,'9 条待补录');
  assert.match(elements.get('bf-note').textContent,/已删除 3 个商品、6 条快照/);
  await select('123458');rows.find(row=>row.id==='123458').snapshot_round='2026-10-07T00:00:00';
  click('bf-refresh');await settle();
  assert.equal(elements.get('bf-delete').disabled,true,'轮次变化清除选择，不自动改为新轮次');

  // 全选/取消全选（2026-10-06 用户指令，替代补录全部）：全选并入当前范围全部页，再按清空。
  click('bf-all');await settle();
  assert.equal(elements.get('bf-delete').textContent,'删除选中（9）','全选覆盖当前范围全部页');
  assert.equal(elements.get('bf-all').textContent,'取消全选');
  click('bf-all');await settle();
  assert.equal(elements.get('bf-delete').textContent,'删除选中（0）','再次按下即取消全选');
  assert.equal(elements.get('bf-all').textContent,'全选');

  // 补录选中：未启用采集目标的勾选被跳过并提示。
  await select('123458');await select('123459');
  click('bf-selected');await settle();
  assert.equal(JSON.stringify(starts.at(-1)),JSON.stringify(['123458']),'仅发送可补录记录');
  assert.match(elements.get('bf-note').textContent,/跳过 1 条/);
  // 清单变化拒绝时自动强刷并清空旧选择。
  startReply={ok:false,err:'selection-changed-refresh-list'};
  await select('123458');
  click('bf-selected');await settle();await settle();
  assert.equal(elements.get('bf-delete').textContent,'删除选中（0）','拒绝后清空旧选择');
  assert.match(elements.get('bf-note').textContent,/清单已变化/);
  startReply={ok:true,progress:{backup:'fixture.duckdb'}};

  // 强制刷新不被 bfLoading 吞掉：在飞查询期间完成删除，查询结束后自动补跑刷新。
  let gate; queryGate=new Promise(resolve=>gate=resolve);
  click('bf-refresh');await settle();
  await select('123459');
  const deleting=click('bf-delete');await settle();await settle();
  assert.match(elements.get('bf-note').textContent,/已删除 1 个商品/,'删除成功即时可见');
  assert.equal(elements.get('bf-delete').textContent,'删除选中（0）');
  gate();await deleting;await settle();await settle();
  assert.equal(elements.get('bf-total').textContent,'8 条待补录','在飞查询结束后补跑强刷，旧清单不残留');
  console.log('backfill popup deletion: OK');
})().catch(error=>{console.error(error);process.exit(1);});
