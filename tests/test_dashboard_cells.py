"""完整显示文本的30字边界，包括数组合并和Unicode字符。"""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_collapse_uses_only_complete_text_length():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    path = Path(__file__).resolve().parents[1] / 'extension/dashboard.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('const cellText ='),end=source.indexOf('function fillGameSelects',start);
const sandbox={expandedCells:new Set(),fmt:String,esc:String};
vm.runInNewContext(source.slice(start,end)+';this.render=cellText',sandbox);
for(const length of [29,30,31]){
  for(const char of ['字','😀']) {
    assert.equal(sandbox.render(char.repeat(length),'text','a').includes('<details'),length>30);
  }
}
assert.ok(!sandbox.render([1,2,3,4].map(n=>({value:1,name:'甲'})),'constellation_cnt','b').includes('<details'));
assert.ok(sandbox.render([{name:'安可',value:3},{name:'鉴心',value:0}],'constellation_cnt','z').includes('3命安可、0命鉴心'));
const paid=[{name:'车架模组',value:'云帛机骑'},{name:'摩托饰品',value:null},{name:'人物皮肤',value:'叱妖诰'}];
const html=sandbox.render(paid,'paid_items','c');
assert.ok(html.includes('车架模组：云帛机骑；摩托饰品：—；人物皮肤：叱妖诰'));
assert.ok(!html.includes('undefined命'));
sandbox.expandedCells.add('a');
assert.ok(sandbox.render('字'.repeat(31),'text','a').includes(' open'));
assert.ok(!sandbox.render('字'.repeat(30),'text','a').includes('<details'));
'''
    result = subprocess.run([node, '-e', script, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_recent_batches_pagination_bounds_and_refresh():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable')
    path = Path(__file__).resolve().parents[1] / 'extension/dashboard.html'
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const nodes={};
for(const id of ['batches','b-total','b-page','b-first','b-prev','b-next','b-last']) {
  nodes[id]={disabled:false,textContent:'',innerHTML:'',addEventListener(type,fn){this.click=fn;},querySelector(){return this;}};
}
const sandbox={$:id=>nodes[id],fmt:String,esc:String};
const start=source.indexOf('const batchState ='),end=source.indexOf('function render(s)',start);
vm.runInNewContext(source.slice(start,end)+';this.render=renderBatches',sandbox);
const rows=n=>Array.from({length:n},(_,i)=>({kind:'detail',listing_id:i,at:'time'+i,rows_updated:1}));
const count=()=> (nodes.batches.innerHTML.match(/<tr>/g)||[]).length;
sandbox.render([]);assert.equal(nodes['b-page'].textContent,'第 1 / 1 页');
for(const action of ['first','prev','next','last']) assert.ok(nodes['b-'+action].disabled);
sandbox.render(rows(10));assert.equal(count(),10);assert.ok(nodes['b-next'].disabled);
const input=rows(11);sandbox.render(input);assert.equal(count(),10);
assert.equal(input[0].listing_id,0);assert.ok(nodes.batches.innerHTML.includes('详情 10'));
nodes['b-next'].click();assert.equal(count(),1);assert.ok(nodes.batches.innerHTML.includes('详情 0'));
assert.equal(nodes['b-page'].textContent,'第 2 / 2 页');assert.ok(nodes['b-next'].disabled);
sandbox.render(rows(12));assert.equal(nodes['b-page'].textContent,'第 2 / 2 页');assert.equal(count(),2);
sandbox.render(rows(50));nodes['b-last'].click();assert.equal(count(),10);
assert.equal(nodes['b-page'].textContent,'第 5 / 5 页');
nodes['b-prev'].click();assert.equal(nodes['b-page'].textContent,'第 4 / 5 页');
sandbox.render(rows(3));assert.equal(count(),3);assert.equal(nodes['b-page'].textContent,'第 1 / 1 页');
sandbox.render(rows(21));nodes['b-last'].click();assert.equal(count(),1);
nodes['b-first'].click();assert.equal(count(),10);assert.equal(nodes['b-page'].textContent,'第 1 / 3 页');
'''
    result = subprocess.run([node, '-e', script, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
