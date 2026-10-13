/** MV3 采集任务：每个详情页面的消息推动下一张；状态持久化，弹窗关闭/SW 休眠不丢队列。 */
(function(root) {
  "use strict";
  const KEY = "pxb7-collection", ALARM = "pxb7-detail-watchdog";
  const active = task => task && ["running", "waiting_verification"].includes(task.status);
  const running = task => task && task.status === "running";
  // SPA的MessageSender.url可能是内容脚本加载时的地址；内容脚本显式报告当前href。
  // 报告地址必须仍属于发送标签页的同源、同路径、同查询串，hash用于区分原任务。
  function samePage(sender, expected, reported) {
    const transport = sender.tab?.url || sender.url;
    try {
      const base = new URL(transport), wanted = new URL(expected);
      return (reported || transport) === expected && base.origin === wanted.origin
        && base.pathname === wanted.pathname && base.search === wanted.search;
    } catch (_) { return false; }
  }
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
    async function retryAction(item, action, task) {
      if (!deps.retry) return {ok:false, err:"自动补采组件不可用，请更新扩展"};
      const token = item.retryToken || `${task.id}:${item.id}`;
      item.retryToken = token;
      return deps.retry({id:item.id, token, action, snapshot_round:item.snapshot_round || task.round});
    }
    function view(task) {
      if (!task) return null;
      const {items, sourceTab, returnTab, workerTab, nonce, round, newIds, ...progress} = task;
      return progress;
    }
    async function finish(task, status, message, keepTab = false) {
      const current = task.items?.[task.processed];
      if (["cancelled", "error", "paused"].includes(status) && current?.kind === "retry" && current.retryClaimed) {
        try {await retryAction(current, "cancelled", task);} catch (_) { /* 网关不可达时保留已使用的资格 */ }
      }
      task.status = status; task.phase = message;
      await chrome.alarms.clear(ALARM);
      const worker = task.workerTab;
      task.workerTab = null;
      await save(task);
      if (task.sourceTab && task.mode === "detail" && task.items && status === "done") {
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
        const short = task.mode !== "backfill" && (task.new_succeeded ?? task.succeeded) < task.requested;
        return finish(task, task.failed || task.incomplete || short ? "partial" : "done",
          (task.mode === "backfill" ? `补录结束：入库 ${task.succeeded}，失败 ${task.failed}`
            : `采集结束：新增 ${task.new_succeeded ?? task.succeeded}/${task.requested}，更新 ${task.updated || 0}，自动重试 ${task.retried || 0}，失败 ${task.failed}`)
          + (task.incomplete ? `；${task.incomplete} 个账号信息仍有缺项` : "")
          + (task.delisted ? `；下架跳过 ${task.delisted} 个（已标记补录失败）` : "")
          + (task.duplicates ? `；去重跳过 ${task.duplicates} 个重复商品` : "")
          + (task.manual_pending ? `；待查缺补录 ${task.manual_pending}` : "")
          + (short ? "；新账号不足目标" : ""));
      }
      // 详情采集间隔：第 2 张起生效（首张立即开始）；独立于列表模式的全文间隔。
      if (task.processed > 0 && task.detail_interval_ms > 0) {
        task.phase = `间隔等待 ${Math.round(task.detail_interval_ms / 100) / 10} 秒…`;
        await save(task);
        await pause(task.detail_interval_ms);
      }
      task.nonce = `${task.id}:${task.processed}:${Date.now()}`;
      task.currentId = task.items[task.processed].id;
      const current = task.items[task.processed];
      if (current.kind === "retry" && !current.retryClaimed) {
        const response = await retryAction(current, "claim", task);
        if (!(response?.ok && response.data?.ok)) {
          return finish(task, "error", response?.data?.error || response?.err || "自动重试资格查询失败，任务已停止");
        }
        if (!response.data.claimed) {
          task.processed++;
          if (response.data.reason === "already-complete") task.duplicates = (task.duplicates || 0) + 1;
          else task.manual_pending = (task.manual_pending || 0) + 1;
          return navigate(task);
        }
        current.retryClaimed = true;
        task.retried = (task.retried || 0) + 1;
      }
      task.phase = task.mode === "backfill"
        ? `补录第 ${Math.floor(task.processed / 200) + 1} 批 · 详情 ${task.processed + 1}/${task.items.length}`
        : `加载详情 ${task.processed + 1}/${task.items.length}`;
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
      if (error === "risk-page" || error === "verification") {
        return verificationPause(task, task.workerTab, "detail");
      }
      const item = task.items[task.processed];
      task.failed++;
      if (task.mode !== "backfill" && ["new", "changed"].includes(item.kind) && !item.retryQueued
          && !["delisted", "detail-id-mismatch"].includes(error)) {
        item.retryQueued = true;
        task.items.push({...item, kind:"retry", retryQueued:true, initialFailure:true, newOrigin:item.kind === "new"});
        task.total = task.items.length;
      } else {
        if (item.kind === "retry") {
          await retryAction(item, "failed", task);
          task.manual_pending = (task.manual_pending || 0) + 1;
        }
      }
      task.processed++; task.errors.push({id:task.currentId, error});
      if (task.mode === "backfill") await recordResult(task, {status:"failed", error});
      // 下架商品（2026-10-06 用户指令）：无法补录，跳过并标记失败，但不计入
      // 连续失败——否则几个下架号就会把整队误暂停。
      if (error === "delisted") {
        task.delisted = (task.delisted || 0) + 1;
        task.phase = "商品已下架，已跳过并标记补录失败";
        return navigate(task);
      }
      task.consecutiveFailures++;
      if (task.consecutiveFailures >= 3) {
        return finish(task, "paused", "连续 3 张失败，已暂停；请检查网站或网关后重采", true);
      }
      return navigate(task);
    }
    async function verificationPause(task, tabId, stage) {
      if (task.status !== "waiting_verification") {
        task.verification = {tabId, stage, phase:task.phase, startedAt:Date.now()};
        if (task.workerTab) task.nonce = `${task.id}:${task.processed}:verification:${Date.now()}`;
        task.status = "waiting_verification";
        task.phase = "等待完成网站验证；完成后继续当前账号，已入库进度保留";
        await chrome.alarms.clear(ALARM);
        await save(task);
        try { await chrome.tabs.update(tabId, {active:true}); } catch (_) { /* 标签页关闭会取消任务 */ }
      }
      return {ok:true, paused:true, nonce:task.nonce, progress:view(task)};
    }
    function ownsVerificationPage(task, sender, reported) {
      if (task.verification?.tabId !== sender.tab?.id) return false;
      return samePage(sender, task.workerTab ? task.items[task.processed]?.url : task.sourceUrl, reported);
    }
    async function recordResult(task, result) {
      const key = "pxb7-backfill-results";
      const results = (await chrome.storage.local.get(key))[key] || {};
      const item = task.items.find(item => item.id === task.currentId);
      results[task.currentId] = {...result, snapshot_round:item.snapshot_round, at:Date.now()};
      await chrome.storage.local.set({[key]:results});
      // 尽力上报网关台账，让完整看板也能看到最近补录状态；失败静默（本地记录兜底）。
      if (task.mode === "backfill" && deps.reportResult) {
        try { await deps.reportResult({id:task.currentId, snapshot_round:item.snapshot_round,
          status:result.status, error:result.error ?? null}); } catch (_) { /* 台账非关键路径 */ }
      }
    }
    // 补录任务组装（弹窗 prepare 与看板中转领取共用）：清单校验 → 建任务 → 开工作标签页。
    async function beginBackfillTask(prepared) {
      const items = prepared.items;
      if (!Array.isArray(items) || !items.length || new Set(items.map(item => item?.id)).size !== items.length || items.some(item =>
        !item ||
        !/^\d{6,24}$/.test(item.id) || item.url !== `https://www.pxb7.com/product/${item.id}/1`
        || typeof item.snapshot_round !== "string" || !Number.isFinite(Date.parse(item.snapshot_round)))) {
        return {ok:false, err:"补录清单无效"};
      }
      let config;
      try { config = await getConfig(); } catch (_) { return {ok:false, err:"无法读取采集间隔，请检查网关"}; }
      if (config?.detail_interval_ms === undefined) return {ok:false, err:"无法读取采集间隔，请检查网关"};
      let returnTab = null;
      try { returnTab = (await chrome.tabs.query({active:true, currentWindow:true}))[0]?.id; } catch (_) { /* 可从任意页面启动 */ }
      const task = {id:`${Date.now()}-${Math.random()}`, mode:"backfill", returnTab,
        requested:items.length, total:items.length, items, backup:prepared.backup,
        processed:0, succeeded:0, failed:0, incomplete:0, errors:[], consecutiveFailures:0,
        detail_interval_ms:clampInterval(config?.detail_interval_ms),
        startedAt:Date.now(), status:"running", phase:"准备补录"};
      await save(task);
      try { task.workerTab = (await chrome.tabs.create({url:"about:blank", active:false})).id; }
      catch (_) { return finish(task,"error","无法创建补录标签页"); }
      return navigate(task);
    }
    const api = {
      progress: async () => ({ok:true, progress:view(await read())}),
      deleteBackfill: options => serial(async () => {
        if (active(await read())) return {ok:false, err:"请先结束或取消采集，再删除数据"};
        const response = await deps.deleteBackfill(options);
        if (response?.data?.ok) {
          const key = "pxb7-backfill-results", results = (await chrome.storage.local.get(key))[key] || {};
          for (const id of response.data.deleted_ids) delete results[id];
          await chrome.storage.local.set({[key]:results});
        }
        return response;
      }),
      // prepare 由背景服务固定端点代理；调用入口只向扩展弹窗开放。
      startBackfill: options => serial(async () => {
        if (active(await read())) return {ok:false, err:"已有采集任务进行中，请等待或取消"};
        const response = await deps.prepareBackfill(options);
        if (!(response?.ok && response.data?.ok)) return {ok:false, err:response?.data?.error || response?.err || "补录准备失败"};
        return beginBackfillTask(response.data);
      }),
      // 完整看板发起的中转任务：background.js 空闲时轮询领取（无任务时返回 no-pending-job）。
      claimBackfillJob: () => serial(async () => {
        if (active(await read())) return {ok:false, err:"busy"};
        if (!deps.claimBackfillJob) return {ok:false, err:"unsupported"};
        const response = await deps.claimBackfillJob();
        if (!(response?.ok && response.data?.ok)) return {ok:false, err:response?.data?.error || response?.err || "no-pending-job"};
        const started = await beginBackfillTask(response.data);
        // 启动结果回报网关：失败让看板停止显示"等待领取"，成功标记已被扩展接管。
        if (deps.finishBackfillJob) {
          try { await deps.finishBackfillJob(started?.ok ? {ok:true}
            : {ok:false, error:started?.err || "补录任务启动失败"}); } catch (_) { /* 台账非关键路径 */ }
        }
        return started;
      }),
      begin: (sender, options) => serial(async () => {
        const old = await read();
        if (active(old)) return {ok:false, err:"已有采集任务进行中，请等待或取消"};
        if (!sender.tab || !/^https:\/\/www\.pxb7\.com\/(buy|product)\//.test(sender.url || sender.tab.url || "")) {
          return {ok:false, err:"不支持的采集页面"};
        }
        const sourceUrl = options.pageUrl || sender.tab.url || sender.url;
        if (!samePage(sender, sourceUrl, options.pageUrl)) return {ok:false, err:"采集页面地址不匹配"};
        const task = {id:`${Date.now()}-${Math.random()}`, sourceTab:sender.tab.id, sourceUrl,
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
        if (task.status === "waiting_verification") {
          if (["error", "cancelled"].includes(msg.status)) return finish(task, msg.status, String(msg.phase || "页面已变化"));
          return {ok:false};
        }
        for (const key of ["total", "processed", "succeeded", "failed", "incomplete", "new_succeeded", "updated", "duplicates", "manual_pending"]) {
          if (Number.isInteger(msg[key]) && msg[key] >= 0 && msg[key] <= 2000) task[key] = msg[key];
        }
        task.phase = String(msg.phase || task.phase).slice(0, 200);
        if (["done", "partial", "error", "cancelled"].includes(msg.status)) return finish(task, msg.status, task.phase);
        await save(task);
        await chrome.alarms.create(ALARM, {when:Date.now() + 180000});
        return {ok:true};
      }),
      start: (sender, msg) => serial(async () => {
        const task = await read();
        if (!running(task) || task.id !== msg.taskId || task.sourceTab !== sender.tab?.id || task.workerTab) return {ok:false, err:"任务已取消或变化"};
        if (typeof msg.round !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/.test(msg.round)) return finish(task,"error","列表轮次无效，详情采集未启动");
        const items = [], seen = new Set();
        let duplicates = 0;
        let newCount = 0;
        for (const item of Array.isArray(msg.items) ? msg.items.slice(0, 1000) : []) {
          let url;
          try { url = new URL(item.url); } catch (_) { continue; }
          const match = url.pathname.match(/^\/product\/(\d{6,24})(?:\/|$)/);
          if (url.origin !== "https://www.pxb7.com" || !match || match[1] !== item.id) continue;
          if (seen.has(item.id)) { duplicates++; continue; }   // 重复账号不入队，计数可见
          const kind = ["new", "changed", "retry"].includes(item.kind) ? item.kind : "new";
          if (kind === "new" && newCount >= task.requested) continue;
          if (kind === "new") newCount++;
          if (item.snapshot_round !== undefined && (typeof item.snapshot_round !== "string"
              || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$/.test(item.snapshot_round))) continue;
          seen.add(item.id);
          items.push({id:item.id, url:url.href, kind, snapshot_round:item.snapshot_round || msg.round,
            prefix:String(item.prefix || "").slice(0, 500)});
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
        task.duplicates = duplicates + (Number(msg.duplicates) || 0);
        task.manual_pending = Number(msg.manual_pending) || 0;
        task.new_succeeded = Number(msg.new_succeeded) || 0;
        task.updated = Number(msg.updated) || 0; task.retried = 0;
        task.newIds = task.new_succeeded ? items.filter(item => item.kind === "new").map(item => item.id) : [];
        task.items = items; task.total = items.length; task.round = msg.round;
        try {
          const tab = await chrome.tabs.create({url:"about:blank", active:false});
          task.workerTab = tab.id;
        } catch (_) { return finish(task,"error","无法创建详情采集标签页"); }
        return navigate(task);
      }),
      context: (sender, msg = {}) => serial(async () => {
        const task = await read();
        if (task?.status === "waiting_verification" && !task.workerTab && task.sourceTab === sender.tab?.id) {
          if (!ownsVerificationPage(task, sender, msg.pageUrl)) return finish(task, "cancelled", "列表地址已变化，已保留入库数据");
          return {ok:true, worker:false, resumeTask:{id:task.id, requested:task.requested, mode:task.mode}};
        }
        if (!active(task) || task.workerTab !== sender.tab?.id) return {ok:true, worker:false};
        const item = task.items[task.processed];
        let url;
        try { url = new URL(sender.url || sender.tab.url); } catch (_) { return {ok:true, worker:true}; }
        if (!item || url.origin !== "https://www.pxb7.com" || !url.pathname.startsWith(`/product/${item.id}/`)) return {ok:true, worker:true};
        return {ok:true, worker:true, job:{taskId:task.id, nonce:task.nonce, ...item, snapshot_round:item.snapshot_round || task.round,
          waiting_verification:task.status === "waiting_verification"}};
      }),
      verificationPause: (sender, msg) => serial(async () => {
        const task = await read();
        if (!active(task) || task.id !== msg.taskId) return {ok:false};
        const tabId = task.workerTab || task.sourceTab;
        const url = task.workerTab ? task.items[task.processed]?.url : task.sourceUrl;
        if (sender.tab?.id !== tabId || !samePage(sender, url, msg.pageUrl)) return {ok:false};
        if (task.workerTab && task.status === "running" && task.nonce !== msg.nonce) return {ok:false};
        return verificationPause(task, tabId, task.workerTab ? "detail" : "source");
      }),
      verificationResume: (sender, msg) => serial(async () => {
        const task = await read();
        if (task?.status !== "waiting_verification" || task.id !== msg.taskId || !ownsVerificationPage(task, sender, msg.pageUrl)) return {ok:false};
        task.status = "running"; task.phase = task.verification.phase;
        delete task.verification;
        await save(task);
        await chrome.alarms.create(ALARM, {when:Date.now() + (task.workerTab ? 60000 : 180000)});
        return {ok:true, nonce:task.nonce, progress:view(task)};
      }),
      claimVerificationAttempt: (sender, msg) => serial(async () => {
        const task = await read();
        if (task?.status !== "waiting_verification" || task.id !== msg.taskId
            || !ownsVerificationPage(task, sender, msg.pageUrl)) return {ok:false, error:"task-changed"};
        if (task.workerTab && task.nonce !== msg.nonce) return {ok:false, error:"task-changed"};
        if (task.verification.autoAttempted) return {ok:false, error:"attempt-used"};
        // 在输入前持久化，刷新页面/后台重启不会反复操作同一个等待事件。
        task.verification.autoAttempted = true;
        await save(task);
        return {ok:true, startedAt:task.verification.startedAt};
      }),
      verificationAttemptActive: async (sender, msg, startedAt) => {
        const task = await read();
        return task?.status === "waiting_verification" && task.id === msg.taskId
          && task.verification.startedAt === startedAt && ownsVerificationPage(task, sender, msg.pageUrl);
      },
      confirmVerification: () => serial(async () => {
        const task = await read();
        if (task?.status !== "waiting_verification") return {ok:false, err:"没有等待验证的任务"};
        try {
          return await chrome.tabs.sendMessage(task.verification.tabId, {type:"verification-confirmed", taskId:task.id});
        } catch (_) { return {ok:false, err:"验证页面不可用，请在原标签页刷新后继续"}; }
      }),
      result: (sender, msg) => serial(async () => {
        const task = await read();
        if (!running(task) || task.workerTab !== sender.tab?.id || task.id !== msg.taskId || task.nonce !== msg.nonce) return {ok:false, err:"过期详情结果"};
        if (msg.error) return failed(task, String(msg.error).slice(0, 80));
        const item = task.items[task.processed];
        if (!msg.payload || msg.payload.listing_id !== item.id || msg.payload.url !== item.url) return failed(task,"detail-id-mismatch");
        const res = await ingest("/ingest/detail", {...msg.payload, snapshot_round:item.snapshot_round || task.round,
          ...(item.retryToken ? {retry_token:item.retryToken} : {}),
          ...(task.mode === "backfill" ? {backfill:true} : {})});
        if (!(res.ok && res.data?.ok && res.data.snapshot_rows_updated > 0)) return failed(task, res.data?.error || res.err || "detail-not-stored");
        if (item.initialFailure) task.failed = Math.max(0, task.failed - 1);
        const weapons = res.data.attributes?.five_star_weapons;
        if (task.mode === "backfill") {
          const result = res.data.backfill;
          if (!result?.complete) task.incomplete++;
          await recordResult(task, {status:result?.complete ? "complete" : "incomplete", missing:result?.missing || []});
        } else {
          const incomplete = Array.isArray(res.data.missing) ? res.data.missing.length > 0
            : /五星角色|五星武器|光锥|音擎/.test(item.prefix)
              && (weapons == null || (weapons > 0 && !res.data.weapon_details_complete));
          if (incomplete && ["new", "changed"].includes(item.kind) && !item.retryQueued) {
            item.retryQueued = true;
            task.items.push({...item, kind:"retry", retryQueued:true, newOrigin:item.kind === "new"});
            task.total = task.items.length;
          } else if (incomplete) {
            task.incomplete++;
            if (item.kind === "retry") {
              await retryAction(item, "incomplete", task);
              task.manual_pending = (task.manual_pending || 0) + 1;
            }
          }
          if ((item.kind === "new" || item.newOrigin) && !task.newIds.includes(item.id)) {
            task.newIds.push(item.id); task.new_succeeded++;
          }
          if (item.kind === "changed") task.updated++;
        }
        task.succeeded++; task.processed++; task.consecutiveFailures = 0;
        return navigate(task);
      }),
      cancel: () => serial(async () => {
        const task = await read();
        if (!active(task)) return {ok:true};
        if (task.sourceTab) {
          try { await chrome.tabs.sendMessage(task.sourceTab, {type:"collect-cancel", taskId:task.id}); } catch (_) { /* 源页已关闭 */ }
        }
        const result = await finish(task,"cancelled","已取消；已经入库的数据保留，可重新开始");
        // 验证会前置详情工作页；关闭它后显式返回源页，确保下一次「采集本页」
        // 作用于原列表，而不是浏览器自动选中的其他网站。
        const returnTab = task.sourceTab || task.returnTab;
        if (returnTab) { try { await chrome.tabs.update(returnTab, {active:true}); } catch (_) { /* 源页已关闭 */ } }
        return result;
      }),
      timeout: () => serial(async () => {
        const task = await read();
        if (!running(task)) return;
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
