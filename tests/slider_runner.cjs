'use strict';
const assert=require('node:assert/strict');
const vm=require('node:vm');
const R=require('../tools/pxb7_slider.cjs');
const url='https://www.pxb7.com/buy/10302/1';
function driver(states) {
  const calls=[]; let idx=0, inputSent=false, tick=0;
  return {calls,now:()=>tick,sleep:async ms=>{tick+=ms;},send:async(method,params)=>{
    calls.push({method,params});
    if (method==='Input.dispatchMouseEvent') {if(params.type==='mouseReleased')inputSent=true;return {};}
    if (params.expression.includes('function publicGeometry')) return {result:{value:{url,x:882,y:518,endX:1162,width:320,height:40,viewportWidth:2048,viewportHeight:972}}};
    const state=inputSent?states[Math.min(idx++,states.length-1)]:{url,blocked:true,businessReady:false};
    return {result:{value:state}};
  }};
}
(async()=>{
  let d=driver([{url,blocked:false,businessReady:true}]);
  assert.deepEqual(await R.runOnce(d.send,url,d),{ok:true,inputSent:true,verificationCleared:true,businessReady:true});
  assert.equal(d.calls.filter(c=>c.params.type==='mousePressed').length,1);
  // 文案“通过”但遮罩仍在、只有验证消失而业务没恢复，都不能成功。
  for (const state of [{url,blocked:true,businessReady:true},{url,blocked:false,businessReady:false}]) {
    d=driver([state]);
    assert.equal((await R.runOnce(d.send,url,{...d,timeoutMs:1500})).error,'verification-or-business-timeout');
    assert.equal(d.calls.filter(c=>c.params.type==='mousePressed').length,1);
  }
  // 曾短暂出现正常卡片，然后再次出现挑战，稳定计时必须重置。
  d=driver([{url,blocked:false,businessReady:true},{url,blocked:true,businessReady:false},{url,blocked:false,businessReady:true}]);
  assert.equal((await R.runOnce(d.send,url,d)).ok,true);assert.ok(d.now()>=2040);
  d=driver([{url:'https://www.pxb7.com/product/2447747437281833822/1',blocked:false,businessReady:true}]);
  assert.equal((await R.runOnce(d.send,url,d)).error,'page-changed');
  let calls=0;
  assert.equal((await R.runOnce(async()=>{calls++;return{result:{value:{url,blocked:false,businessReady:true}}}},url)).error,'no-visible-challenge');
  assert.equal(calls,1,'无可见挑战不发输入');
  for (const value of ['http://evil.com:9222','http://127.0.0.1.evil.com:9222','http://u@localhost:9222','https://127.0.0.1:9222','http://localhost:9222/?key=secret']) assert.throws(()=>R.endpoint(value));
  assert.equal(R.parseArgs(['--cdp','http://127.0.0.1:9222','--target','ABC','--watch-seconds','60']).watchSeconds,60);
  assert.throws(()=>R.parseArgs(['--cdp','http://127.0.0.1:9222','--target','ABC','--watch-seconds','Infinity']));
  // 执行实际公开DOM探针，隐藏挑战不能阻止正常列表；可见遮罩不能放行旧卡片。
  const track={parentElement:null,innerText:'访问验证',getBoundingClientRect:()=>({width:320,height:40})};
  const card={parentElement:null,innerText:'80级 ￥444',getBoundingClientRect:()=>({width:300,height:100})};
  let hidden=false;
  const env={location:{href:url,pathname:'/buy/10302/1'},getComputedStyle:e=>({display:e===track&&hidden?'none':'block',visibility:'visible'}),
    document:{title:'鸣潮账号',body:{innerText:''},querySelectorAll:s=>s.includes('WAF_NC_WRAPPER')?[track]:[card]}};
  let state=vm.runInNewContext('('+R.publicState.toString()+')()',env);assert.equal(state.blocked,true);assert.equal(state.businessReady,false);
  hidden=true;state=vm.runInNewContext('('+R.publicState.toString()+')()',env);assert.equal(state.businessReady,true);
  env.document.title='访问验证';state=vm.runInNewContext('('+R.publicState.toString()+')()',env);assert.equal(state.blocked,true);
  env.document.title='鸣潮账号';
  env.location={href:'https://www.pxb7.com/product/2447747437281833822/1',pathname:'/product/2447747437281833822/1'};
  env.document.body.innerText='推荐商品 ￥123 商品描述';assert.equal(vm.runInNewContext('('+R.publicState.toString()+')()',env).businessReady,false);
  env.document.title='鸣潮账号交易_【MGVTQ4812】80级';env.document.body.innerText='账号详情 ￥444 【MGVTQ4812】80级';
  assert.equal(vm.runInNewContext('('+R.publicState.toString()+')()',env).businessReady,true);
  console.log('slider runner: real shared input, stable business recovery, wrong page, loopback guard: PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
