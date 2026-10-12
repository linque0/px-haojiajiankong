// 实际 content.js + collection.js：下架详情页 → delisted 错误 → 跳过并标记补录失败，
// 不计入连续失败（多个下架不再触发"连续 3 张失败"整队暂停）；无标记页面仍走超时路径。
const assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const {create} = require('../extension/pxb7-extension/collection.js');
const code = fs.readFileSync('extension/pxb7-extension/content.js', 'utf8');

async function drain(timers, times) {
  for (let i = 0; i < times; i++) {
    await new Promise(setImmediate);
    timers.splice(0).forEach(fn => fn());
  }
  await new Promise(setImmediate);
}

async function scenario({title, zoneText, count}) {
  const navigations = [], reported = [], storage = {};
  const items = Array.from({length: count}, (_, i) =>
    ({id: String(223456 + i), url: `https://www.pxb7.com/product/${223456 + i}/1`,
      snapshot_round: '2026-10-06T19:30:00', game_id: 10302}));
  const chrome = {
    storage:{local:{async get(k){return structuredClone({[k]:storage[k]});},
      async set(v){Object.assign(storage,structuredClone(v));}}},
    alarms:{async create(){},async clear(){},onAlarm:{addListener(){}}},
    tabs:{async create(){return {id:20};},async update(id,o){navigations.push({id,...o});},async remove(){},
      async sendMessage(){throw Error('no source page');},onRemoved:{addListener(){}}},
    runtime:{id:'test-extension',getURL:p=>`chrome-extension://test-extension/${p}`,
      getManifest:()=>({version:'test'}),onMessage:{addListener(){}},
      onInstalled:{addListener(){}},onStartup:{addListener(){}}},
  };
  const deps = {getConfig:async()=>({detail_interval_ms:0}),
    prepareBackfill:async()=>({ok:true,data:{ok:true,items,backup:'fixture.duckdb'}}),
    reportResult:async payload => reported.push(payload)};
  const queue = create(chrome, async()=>({ok:true,data:{ok:true,snapshot_rows_updated:1}}), deps);
  await queue.startBackfill({all:true});

  // 每次导航 = 工作标签页加载新详情页 = 一份新内容脚本
  for (const item of items) {
    const sender = {tab:{id:20,url:item.url},url:item.url};
    const handlers = [], timers = [];
    const sandboxChrome = {...chrome, runtime:{...chrome.runtime, lastError:null,
      sendMessage:(msg,cb)=>{handlers.length ? handlers[0](msg,sender,cb)
        : cb(queueMessage(msg));}}};
    function queueMessage(msg) {
      // 内容脚本 → 队列的固定路由（与 background.js 一致）
      const routes = {'detail-context':'context','detail-result':'result'};
      if (routes[msg.type]) return queue[routes[msg.type]](sender,msg);
      return {ok:true};
    }
    const sandbox = {console,Date,Promise,JSON,Number,String,Array,Object,Math,
      chrome:sandboxChrome,
      location:{pathname:`/product/${item.id}/`,search:'',hash:'',href:item.url},
      document:{title,
        querySelector:sel => sel === '.product-detail' ? {textContent:zoneText} : null,
        querySelectorAll:()=>[],
        documentElement:{outerHTML:'<html></html>'},
        body:{appendChild(){}, textContent:zoneText},
        createElement:()=>({style:{}})},
      sessionStorage:{getItem(){},setItem(){}},
      setTimeout(fn){timers.push(fn);},clearTimeout(){},setInterval(){},clearInterval(){},
      addEventListener(){},removeEventListener(){}};
    sandbox.window = sandbox;
    vm.runInNewContext(code, sandbox);
    await new Promise(setImmediate);
    // 标题迟迟不就绪：下架标记约 3.2s（32 次×100ms）稳定判定；无标记页耗满 20s 超时
    await drain(timers, title.includes('下架') ? 60 : 210);
  }
  return {queue, reported, storage, navigations, items};
}

(async()=>{
  // 5 个连续下架：逐个跳过并标记失败，不暂停队列
  const delisted = await scenario({title:'商品已下架 - pxb7', zoneText:'商品已下架', count:5});
  const p = (await delisted.queue.progress()).progress;
  assert.equal(p.status, 'partial', '下架商品不得触发连续失败暂停');
  assert.equal(p.delisted, 5);
  assert.equal(p.failed, 5);
  assert.equal(p.consecutiveFailures, 0, '下架不计入连续失败');
  assert.match(p.phase, /下架跳过 5/);
  for (const item of delisted.items) {
    const saved = delisted.storage['pxb7-backfill-results'][item.id];
    assert.equal(saved.status, 'failed');
    assert.equal(saved.error, 'delisted');
    assert.equal(saved.snapshot_round, item.snapshot_round);
  }
  assert.equal(delisted.reported.length, 5);
  assert.equal(delisted.reported[0].error, 'delisted');
  assert.equal(delisted.reported[0].status, 'failed');
  assert.equal(delisted.navigations.length, 5, '每个下架商品都推进到下一项');

  // 无下架标记页面：保持原超时路径，按普通失败计入连续失败
  const normal = await scenario({title:'测试商品 - pxb7', zoneText:'等级80 黄数4', count:1});
  const q = (await normal.queue.progress()).progress;
  assert.equal(q.consecutiveFailures, 1, '无标记超时按普通失败计');
  assert.equal(normal.storage['pxb7-backfill-results']['223456'].error, 'detail-not-ready');
  assert.equal(q.status, 'partial', '单商品队列失败后正常结束（未暂停）');
  console.log('content delist skip and mark failed: PASS');
})().catch(e=>{console.error(e);process.exit(1);});
