/**
 * pxb7 采集助手（前端 B）—— 背景服务（MV3 Service Worker）
 *
 * 职责：本机网关代理。内容脚本与弹窗的所有本机网关调用都经这里代理：
 * - host_permissions 已声明回环地址，扩展页面/SW 的 fetch 不走页面 CORS；
 * - 只访问本机回环网关；MAIN 脚本可按商品编号限速请求站点公开完整标题。
 *
 * 安全边界说明：本文件**不含任何 SQL / 数据库访问**——所有持久化都在 Python 网关内以
 * 参数化 DuckDB 查询完成；此处只有固定的 HTTP 端点与 JSON 序列化。
 */
"use strict";

const GATEWAY = "http://127.0.0.1:8765";

// 可代理的采集端点白名单：内容脚本只能提交到这两个固定路径（防御性收紧）
const INGEST_PATHS = new Set(["/ingest/cards", "/ingest/detail", "/collection/check", "/collection/retry",
  "/collection/text", "/collection/text-cache/query", "/collection/text-cache/store"]);
importScripts("collection.js", "slider-input.js");
// 详情队列的间隔来自网关 /config（deps.getConfig）；列表模式不经此路径，两者互不共用。
const collection = PXB7_COLLECTION.create(chrome, (path, payload) => gfetch(path, withJsonBody(payload)),
  { getConfig: async () => {
      const res = await gfetch("/config");
      return (res && res.data && res.data.config) || {};
    }, retry: options => gfetch("/collection/retry", withJsonBody(options)),
    createBatch: async options => {
      const cfg = await gfetch("/config");
      if (cfg.data?.capabilities?.collection_protocol !== 1) return {ok:false, err:"请升级网关：批量采集协议不可用"};
      return gfetch("/collection/batch/create", withJsonBody(options));
    },
    batchProgress: payload => gfetch("/collection/batch/progress", withJsonBody(payload)),
    batchAccess: payload => gfetch("/collection/batch/access", withJsonBody(payload)),
    flushBatch: payload => gfetch("/collection/batch/flush", withJsonBody(payload)),
    claimMonitor: () => gfetch("/collection/monitor/claim", withJsonBody({})),
    prepareBackfill: options => gfetch("/backfill/prepare", withJsonBody(options)),
    deleteBackfill: options => gfetch("/backfill/delete", withJsonBody(options)),
    // 完整看板发起的补录中转：领取/回报 + 单项结果台账上报（尽力而为）。
    claimBackfillJob: () => gfetch("/backfill/job/claim", withJsonBody({})),
    finishBackfillJob: result => gfetch("/backfill/job/finish", withJsonBody(result)),
    reportResult: payload => gfetch("/backfill/result", withJsonBody(payload)) });

// 补录启动只接受本扩展 UI；站点内容脚本不能构造补录任务或触发数据库备份。
function extensionPage(sender) {
  return sender.id === chrome.runtime.id && sender.url === chrome.runtime.getURL("popup.html");
}
let syncWrites = Promise.resolve();
async function rememberSync(data) {
  const results = data?.results || (data?.analysis_sync ? [data.analysis_sync] : Object.values(data?.csv_sync_status || {}));
  if (!results.length) return;
  syncWrites = syncWrites.catch(() => {}).then(async () => {
    const key = "pxb7-csv-sync", pending = (await chrome.storage.local.get(key))[key] || {};
    for (const result of results) {
      if (result.ok) delete pending[result.game_id];
      else pending[result.game_id] = result;
    }
    await chrome.storage.local.set({[key]:pending});
  });
  await syncWrites;
}

// 自更新：解包扩展的「重新加载」按浏览器是手动操作（Chrome/Edge/夸克都忽略命令行里的
// chrome:// 地址），但扩展可以重载自己——chrome.runtime.reload() 会按磁盘目录重新读取。
// 网关 /config 的 script.extension.latest_version 来自仓库 manifest.json，据此比对即可。
const OWN_VERSION = chrome.runtime.getManifest().version;
const UPDATE_ALARM = "pxb7-self-update";
const UPDATE_CHECK_MINUTES = 30;

function verTuple(version) {
  return String(version || "0").split(".").map((p) => {
    const n = parseInt(p, 10);
    return Number.isFinite(n) ? n : 0;
  });
}

function isNewer(candidate, current) {
  const a = verTuple(candidate);
  const b = verTuple(current);
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const x = a[i] || 0;
    const y = b[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}

/** 自更新检查：磁盘 manifest 版本更新 → 角标提示；autoReload 时直接重载。 */
async function checkSelfUpdate(autoReload) {
  const params = new URLSearchParams();
  params.set("version", OWN_VERSION);
  params.set("channel", "extension");
  const res = await gfetch("/config?" + params.toString());
  const info = res && res.data && res.data.ok && res.data.script && res.data.script.extension;
  const latest = info ? info.latest_version : null;
  if (!latest || !isNewer(latest, OWN_VERSION)) return { ok: true, latest, update_available: false };
  chrome.action.setBadgeBackgroundColor({ color: "#f59e0b" });
  chrome.action.setBadgeText({ text: "↑" });
  const running = ["running", "waiting_verification"].includes((await collection.progress()).progress?.status);
  if (autoReload && !running) chrome.runtime.reload(); // 等采集结束再自动更新
  return { ok: true, latest, update_available: true, reloading: !!autoReload && !running };
}

async function gfetch(path, options) {
  try {
    const response = await fetch(GATEWAY + path, {...options, signal:AbortSignal.timeout(20000)});
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { /* 非 JSON 保持 null */ }
    await rememberSync(data);
    return { ok: true, status: response.status, data };
  } catch (e) {
    const detail = String((e && e.message) || e);
    return { ok: false, err: "网关不可达（双击桌面「pxb7采集看板」可启动服务）：" + detail };
  }
}

function withJsonBody(payload) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload === undefined ? {} : payload),
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return undefined;
  if (msg.type === "verification-attempt") {
    (async () => {
      if (sender.id !== chrome.runtime.id || sender.frameId !== 0 || !PXB7_SLIDER_INPUT.allowedPage(msg.pageUrl)) {
        return {ok:false, error:"unsupported-page"};
      }
      const cfg = await gfetch("/config");
      if (!cfg.ok || cfg.data?.config?.verification_mode !== "auto_slider") return {ok:false, error:"auto-disabled"};
      const claim = await collection.claimVerificationAttempt(sender, msg);
      if (!claim.ok) return claim;
      return PXB7_SLIDER_INPUT.attempt(chrome, sender.tab.id, msg.pageUrl,
        {alive:() => collection.verificationAttemptActive(sender, msg, claim.startedAt)});
    })().then(sendResponse).catch(() => sendResponse({ok:false, error:"automatic-input-unavailable"}));
    return true;
  }
  if (["backfill-query", "backfill-start", "backfill-state", "backfill-sync", "backfill-delete"].includes(msg.type)) {
    if (!extensionPage(sender)) { sendResponse({ok:false, err:"extension-page-required"}); return true; }
    let job;
    if (msg.type === "backfill-start") job = collection.startBackfill(msg.options || {});
    else if (msg.type === "backfill-delete") job = collection.deleteBackfill(msg.options || {});
    else if (msg.type === "backfill-query") {
      const query = new URLSearchParams({game_id:String(msg.game_id || "selected"),
        offset:String(msg.offset || 0), limit:String(msg.limit || 10)});
      job = gfetch("/backfill/candidates?" + query.toString());
    } else if (msg.type === "backfill-sync") {
      job = gfetch("/backfill/sync", withJsonBody({game_ids:msg.game_ids}));
    } else job = chrome.storage.local.get(["pxb7-backfill-results", "pxb7-csv-sync"])
      .then(data => ({ok:true, results:data["pxb7-backfill-results"] || {}, pending:data["pxb7-csv-sync"] || {}}));
    job.then(sendResponse).catch(error => sendResponse({ok:false, err:String(error.message || error)}));
    return true;
  }
  const jobs = {
    "collection-begin":() => collection.begin(sender, msg),
    "collection-progress":() => collection.update(sender, msg),
    "collection-status":() => collection.progress(),
    "collection-access":() => collection.authorize(sender, msg),
    "collection-cancel":() => collection.cancel(),
    "detail-start":() => collection.start(sender, msg),
    "detail-context":() => collection.context(sender, msg),
    "detail-result":() => collection.result(sender, msg),
    "verification-pause":() => collection.verificationPause(sender, msg),
    "verification-resume":() => collection.verificationResume(sender, msg),
    "verification-confirm":() => collection.confirmVerification(),
  };
  if (jobs[msg.type]) {
    jobs[msg.type]().then(sendResponse).catch(e => sendResponse({ok:false, err:String(e.message || e)}));
    return true;
  }
  if (msg.type === "ingest") {
    if (!INGEST_PATHS.has(msg.path)) {          // 路径白名单：只代理固定采集端点
      sendResponse({ ok: false, err: "unsupported-path" });
      return true;
    }
    gfetch(msg.path, withJsonBody(msg.payload)).then(sendResponse);
    return true;                       // 异步应答
  }
  if (msg.type === "config") {
    // 版本号与通道以 URLSearchParams 编码注入查询串（本文件无 SQL；仅回环 HTTP 查询参数）
    const params = new URLSearchParams();
    params.set("version", String(msg.version || "0"));
    params.set("channel", "extension");     // 与油猴脚本通道分开上报（看板分别显示更新状态）
    const configPath = "/config?" + params.toString();
    gfetch(configPath).then(sendResponse);
    return true;
  }
  if (msg.type === "config-save") {
    gfetch("/config", withJsonBody(msg.config)).then(sendResponse);
    return true;
  }
  if (msg.type === "stats") {
    gfetch("/stats").then(sendResponse);
    return true;
  }
  if (msg.type === "shutdown") {
    gfetch("/shutdown", withJsonBody({})).then(sendResponse);
    return true;
  }
  if (msg.type === "collect-active") {
    collectActiveTab().then(sendResponse);
    return true;
  }
  if (msg.type === "check-update") {
    checkSelfUpdate(false).then(sendResponse);
    return true;
  }
  if (msg.type === "apply-update") {
    // 用户在弹窗点「应用扩展更新」：重载扩展（重载后本页/弹窗自动关闭，属预期）
    sendResponse({ ok: true, version: OWN_VERSION });
    setTimeout(() => chrome.runtime.reload(), 50);
    return true;
  }
  return undefined;
});

// 自更新调度：SW 随时会被回收，用 alarms 唤醒（这是 MV3 里唯一可靠的周期任务机制）
chrome.alarms.create(UPDATE_ALARM, { periodInMinutes: UPDATE_CHECK_MINUTES });
// 完整看板发起的补录任务：空闲时每分钟轮询领取一次（浏览器开着扩展在线即接管执行）
const JOB_ALARM = "pxb7-backfill-job";
chrome.alarms.create(JOB_ALARM, { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === UPDATE_ALARM) checkSelfUpdate(true);
  if (alarm && alarm.name === JOB_ALARM) {
    collection.claimBackfillJob().then(()=>collection.claimMonitorJob()).catch(() => {});
  }
});

/** 让当前活动标签页的内容脚本立即采集本页（弹窗「采集本页」入口）。 */
async function collectActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || !tab.id) return { ok: false, err: "没有活动标签页" };
  try {
    const result = await chrome.tabs.sendMessage(tab.id, { type: "collect-now" });
    return result || { ok: false, err: "采集未返回结果" };
  } catch (e) {
    return { ok: false, err: "当前页不是 pxb7 列表/详情页（或扩展刚安装需刷新页面）" };
  }
}

// 安装/更新/浏览器启动时：点亮工具栏角标 + 检查是否有更新版本
chrome.runtime.onInstalled.addListener(async () => {
  const result = await gfetch("/stats");
  const up = !!(result.ok && result.data && result.data.ok);
  chrome.action.setBadgeBackgroundColor({ color: up ? "#16a34a" : "#dc2626" });
  chrome.action.setBadgeText({ text: up ? "✓" : "!" });
  await checkSelfUpdate(false);
});

chrome.runtime.onStartup.addListener(() => {
  checkSelfUpdate(false);
  collection.claimBackfillJob().catch(() => {});   // 浏览器启动即检查是否有看板排队任务
});
