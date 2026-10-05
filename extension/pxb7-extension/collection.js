/** MV3 采集任务：每个详情页面的消息推动下一张；状态持久化，弹窗关闭/SW 休眠不丢队列。 */
(function(root) {
  "use strict";
  const KEY = "pxb7-collection", ALARM = "pxb7-detail-watchdog";
  const active = task => task && task.status === "running";
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  // 详情采集间隔（毫秒）：0–10000、0.1 秒粒度取整（与网关 title_interval_ms 同口径）；
  // 未提供/非法回落 0（保持既有行为：不等待）；0 = 用户显式不等待。
  function clampInterval(ms) {
    if (ms === undefined || ms === null || ms === "") return 0;
    const n = Number(ms);
    if (!Number.isFinite(n)) return 0;
    return Math.max(0, Math.min(10000, Math.round(n / 100) * 100));
  }
  function create(chrome, ingest, deps = {}) {
    const getConfig = deps.getConfig || (async () => ({}));
    let chain = Promise.resolve();
    const serial = fn => {
      const result = chain.then(fn);
      chain = result.catch(() => {});
      return result;
    };
    const read = async () => (await chrome.storage.local.get(KEY))[KEY] || null;
    const save = async task => { task.updatedAt = Date.now(); await chrome.storage.local.set({[KEY]:task}); };
    function view(task) {
      if (!task) return null;
      const {items, sourceTab, workerTab, nonce, round, ...progress} = task;
      return progress;
    }
    async function finish(task, status, message, keepTab = false) {
      task.status = status; task.phase = message;
      await chrome.alarms.clear(ALARM);
      const worker = task.workerTab;
      task.workerTab = null;
      await save(task);
      if (task.mode === "detail" && task.items && status === "done") {
        try { await chrome.tabs.sendMessage(task.sourceTab, {type:"detail-completed", url:task.sourceUrl}); } catch (_) { /* 源页已关闭 */ }
      }
      if (worker && !keepTab) {
        try { await chrome.tabs.remove(worker); } catch (_) { /* 用户已关闭 */ }
      }
      if (worker && keepTab) {
        try { await chrome.tabs.update(worker, {active:true}); } catch (_) { /* 用户已关闭 */ }
      }
      return {ok:true, progress:view(task)};
    }
    async function navigate(task) {
      if (task.processed >= task.items.length) {
        return finish(task, task.failed || task.incomplete || task.items.length < task.requested ? "partial" : "done",
          `采集结束：入库 ${task.succeeded}，失败 ${task.failed}`
          + (task.incomplete ? `；${task.incomplete} 张武器信息未完整公示` : "")
          + (task.duplicates ? `；去重跳过 ${task.duplicates} 个重复商品` : "")
          + (task.items.length < task.requested ? `；仅加载出 ${task.items.length}/${task.requested} 张` : ""));
      }
      // 详情采集间隔：第 2 张起生效（首张立即开始）；独立于列表模式的全文间隔。
      if (task.processed > 0 && task.detail_interval_ms > 0) {
        task.phase = `间隔等待 ${Math.round(task.detail_interval_ms / 100) / 10} 秒…`;
        await save(task);
        await pause(task.detail_interval_ms);
      }
      task.nonce = `${task.id}:${task.processed}:${Date.now()}`;
      task.currentId = task.items[task.processed].id;
      task.phase = `加载详情 ${task.processed + 1}/${task.items.length}`;
      await save(task); // 导航前落盘，下一份内容脚本可立即识别工作标签页
      await chrome.alarms.create(ALARM, {when:Date.now() + 60000});
      try {
        await chrome.tabs.update(task.workerTab, {url:task.items[task.processed].url});
      } catch (_) {
        return finish(task, "error", "详情标签页不可用，请重新开始");
      }
      return {ok:true, progress:view(task)};
    }
    async function failed(task, error) {
      task.failed++; task.processed++; task.consecutiveFailures++;
      task.errors.push({id:task.currentId, error});
      if (error === "risk-page" || error === "verification") {
        return finish(task, "paused", "网站要求验证，已暂停；完成验证后重新采集", true);
      }
      if (task.consecutiveFailures >= 3) {
        return finish(task, "paused", "连续 3 张失败，已暂停；请检查网站或网关后重采", true);
      }
      return navigate(task);
    }
    const api = {
      progress: async () => ({ok:true, progress:view(await read())}),
      begin: (sender, options) => serial(async () => {
        const old = await read();
        if (active(old)) return {ok:false, err:"已有采集任务进行中，请等待或取消"};
        if (!sender.tab || !/^https:\/\/www\.pxb7\.com\/(buy|product)\//.test(sender.url || sender.tab.url || "")) {
          return {ok:false, err:"不支持的采集页面"};
        }
        const task = {id:`${Date.now()}-${Math.random()}`, sourceTab:sender.tab.id, sourceUrl:sender.url || sender.tab.url,
          mode:options.mode === "detail" ? "detail" : "list", requested:Math.min(200, Math.max(1, Number(options.total) || 16)),
          total:0, processed:0, succeeded:0, failed:0, incomplete:0, status:"running", phase:"准备卡片",
          startedAt:Date.now(), errors:[], consecutiveFailures:0};
        await save(task);
        // 列表进度消息会续期；源页意外卸载也不会留下永久运行状态。
        await chrome.alarms.create(ALARM, {when:Date.now() + 180000});
        return {ok:true, taskId:task.id};
      }),
      update: (sender, msg) => serial(async () => {
        const task = await read();
        if (!active(task) || task.id !== msg.taskId || task.sourceTab !== sender.tab?.id || task.workerTab) return {ok:false};
        for (const key of ["total", "processed", "succeeded", "failed", "incomplete"]) {
          if (Number.isInteger(msg[key]) && msg[key] >= 0 && msg[key] <= 200) task[key] = msg[key];
        }
        task.phase = String(msg.phase || task.phase).slice(0, 200);
        if (["done", "partial", "error", "cancelled"].includes(msg.status)) return finish(task, msg.status, task.phase);
        await save(task);
        await chrome.alarms.create(ALARM, {when:Date.now() + 180000});
        return {ok:true};
      }),
      start: (sender, msg) => serial(async () => {
        const task = await read();
        if (!active(task) || task.id !== msg.taskId || task.sourceTab !== sender.tab?.id || task.workerTab) return {ok:false, err:"任务已取消或变化"};
        if (typeof msg.round !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/.test(msg.round)) return finish(task,"error","列表轮次无效，详情采集未启动");
        const items = [], seen = new Set();
        let duplicates = 0;
        for (const item of Array.isArray(msg.items) ? msg.items.slice(0, task.requested) : []) {
          let url;
          try { url = new URL(item.url); } catch (_) { continue; }
          const match = url.pathname.match(/^\/product\/(\d{6,24})(?:\/|$)/);
          if (url.origin !== "https://www.pxb7.com" || !match || match[1] !== item.id) continue;
          if (seen.has(item.id)) { duplicates++; continue; }   // 重复账号不入队，计数可见
          seen.add(item.id);
          items.push({id:item.id, url:url.href, prefix:String(item.prefix || "").slice(0, 500)});
        }
        if (!items.length) return finish(task,"error","没有可用的商品详情链接");
        // 详情采集间隔（独立于列表模式）：消息显式携带优先，其次网关配置；都无 → 0（不等待）
        if (msg.detail_interval_ms !== undefined) {
          task.detail_interval_ms = clampInterval(msg.detail_interval_ms);
        } else {
          let config = {};
          try { config = await getConfig() || {}; } catch (_) { /* 网关不可达按 0 处理 */ }
          task.detail_interval_ms = clampInterval(config.detail_interval_ms);
        }
        task.duplicates = duplicates;
        task.items = items; task.total = items.length; task.round = msg.round;
        try {
          const tab = await chrome.tabs.create({url:"about:blank", active:false});
          task.workerTab = tab.id;
        } catch (_) { return finish(task,"error","无法创建详情采集标签页"); }
        return navigate(task);
      }),
      context: (sender) => serial(async () => {
        const task = await read();
        if (!active(task) || task.workerTab !== sender.tab?.id) return {ok:true, worker:false};
        const item = task.items[task.processed];
        let url;
        try { url = new URL(sender.url || sender.tab.url); } catch (_) { return {ok:true, worker:true}; }
        if (!item || url.origin !== "https://www.pxb7.com" || !url.pathname.startsWith(`/product/${item.id}/`)) return {ok:true, worker:true};
        return {ok:true, worker:true, job:{taskId:task.id, nonce:task.nonce, ...item, snapshot_round:task.round}};
      }),
      result: (sender, msg) => serial(async () => {
        const task = await read();
        if (!active(task) || task.workerTab !== sender.tab?.id || task.id !== msg.taskId || task.nonce !== msg.nonce) return {ok:false, err:"过期详情结果"};
        if (msg.error) return failed(task, String(msg.error).slice(0, 80));
        const item = task.items[task.processed];
        if (!msg.payload || msg.payload.listing_id !== item.id || msg.payload.url !== item.url) return failed(task,"detail-id-mismatch");
        const res = await ingest("/ingest/detail", {...msg.payload, snapshot_round:task.round});
        if (!(res.ok && res.data?.ok && res.data.snapshot_rows_updated > 0)) return failed(task, res.data?.error || res.err || "detail-not-stored");
        const weapons = res.data.attributes?.five_star_weapons;
        if (/五星角色|五星武器|光锥|音擎/.test(item.prefix)
            && (weapons == null || (weapons > 0 && !res.data.weapon_details_complete))) task.incomplete++;
        task.succeeded++; task.processed++; task.consecutiveFailures = 0;
        return navigate(task);
      }),
      cancel: () => serial(async () => {
        const task = await read();
        if (!active(task)) return {ok:true};
        try { await chrome.tabs.sendMessage(task.sourceTab, {type:"collect-cancel"}); } catch (_) { /* 源页已关闭 */ }
        return finish(task,"cancelled","已取消；已经入库的数据保留");
      }),
      timeout: () => serial(async () => {
        const task = await read();
        if (!active(task)) return;
        if (task.workerTab) return failed(task,"detail-timeout");
        return finish(task,"error","采集长时间无响应，请重新开始");
      }),
      removed: tabId => serial(async () => {
        const task = await read();
        if (active(task) && (task.workerTab === tabId || (!task.workerTab && task.sourceTab === tabId))) {
          return finish(task,"cancelled","采集标签页已关闭，任务取消");
        }
      }),
    };
    chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) api.timeout(); });
    chrome.tabs.onRemoved.addListener(tabId => api.removed(tabId));
    return api;
  }
  root.PXB7_COLLECTION = {create};
  if (typeof module !== "undefined" && module.exports) module.exports = root.PXB7_COLLECTION;
})(globalThis);
