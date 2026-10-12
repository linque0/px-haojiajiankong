// 离线 UI 夹具：所有网络、标签页与存储行为均为模拟，不访问真实扩展或商品。
(() => {
  let progress = null;
  let pending = new URLSearchParams(location.search).has('sync-failed')
    ? {10302:{game_id:10302,error:'PermissionError: 离线模拟文件占用',message:'模拟：数据已入库，请关闭占用表格后重试',path:'离线模拟 CSV'}} : {};
  const rows = Array.from({length:12}, (_,i) => ({id:String(123456+i),game_id:10302,game_name:"鸣潮",
    snapshot_round:"2026-10-06T19:30:00",collectable:true,
    missing:[{key:"five_star_weapons",label:"五星武器数",reason:"缺失或无效"}]}));
  const config = {collection_mode:"detail",auto_ingest:false,reingest_interval_min:5,cards_target:48,
    spa_settle_ms:2500,title_interval_ms:1000,detail_interval_ms:400};
  const targets = {available:[{task_id:"wuwa_official",name:"鸣潮-官服",enabled:true,selected:true,snapshots:12}],
    effective:["wuwa_official"],mode:"selected"};
  window.fetch = async () => ({status:200,text:async()=>JSON.stringify({ok:true,config,targets,
    stats:{cards_seen:0,cards_parsed:0,new_listings:0,details_stored:0,risk_pages_rejected:0},
    db:{snapshot_rows:12,keyword_hits:0,rounds:[]},recent_batches:[]})});
  window.chrome = {runtime:{getManifest:()=>({version:"0.5.7"}),lastError:null,
    sendMessage: async (message, callback) => {
      let reply = {ok:true};
      if(message.type==='collection-status') reply.progress=progress;
      if(message.type==='backfill-query') reply={ok:true,data:{ok:true,total:rows.length,can_backfill_all:true,
        rows:rows.slice(message.offset||0,(message.offset||0)+10),games:[{game_id:10302,game_name:"鸣潮",missing:12}],
        coverage:{10302:["等级","黄数","五星角色数","五星武器数","角色及武器明细"]}}};
      if(message.type==='backfill-state') reply={ok:true,results:Object.fromEntries(rows.slice(0,2).map((row,i)=>
        [row.id,{status:i ? 'incomplete' : 'failed',snapshot_round:row.snapshot_round,at:Date.now(),error:i ? '' : '离线模拟页面失效'}])),pending};
      if(message.type==='backfill-delete') {
        const ids=message.options.items.map(item=>item.id);
        for(let i=rows.length-1;i>=0;i--) if(ids.includes(rows[i].id)) rows.splice(i,1);
        reply={ok:true,data:{ok:true,deleted_ids:ids,deleted:ids.length,counts:{fct_listing_snapshot:ids.length*2},
          backup:'离线模拟备份（无真实删除）',results:[]}};
      }
      if(message.type==='backfill-sync') {pending={}; reply={ok:true,data:{ok:true,results:[{ok:true,game_id:10302}]}};}
      if(message.type==='backfill-start') {
        const total=message.options.all ? rows.length : message.options.items.length;
        progress={id:'preview',mode:'backfill',status:'running',requested:total,processed:0,
          succeeded:0,failed:0,incomplete:0,startedAt:Date.now(),updatedAt:Date.now(),
          phase:'离线模拟：不访问商品、不写库',backup:'离线模拟备份'};
        reply.progress=progress;
      }
      if(message.type==='collection-cancel') progress={...progress,status:'cancelled',updatedAt:Date.now(),phase:'离线模拟已取消'};
      if(callback) callback(reply);
      return reply;
    }},tabs:{create:async()=>({id:1})}};
})();
