/**
 * pxb7 采集助手（前端 B）—— 背景服务（MV3 Service Worker）
 *
 * 职责：唯一的出网口。内容脚本与弹窗的所有本机网关调用都经这里代理：
 * - host_permissions 已声明回环地址，扩展页面/SW 的 fetch 不走页面 CORS；
 * - 只访问本机回环网关（不请求任何 pxb7 地址），零新增 pxb7 请求（docs/01 §8 W4 口径）。
 *
 * 安全边界说明：本文件**不含任何 SQL / 数据库访问**——所有持久化都在 Python 网关内以
 * 参数化 DuckDB 查询完成；此处只有固定的 HTTP 端点与 JSON 序列化。
 */
"use strict";

const GATEWAY = "http://127.0.0.1:8765";

// 可代理的采集端点白名单：内容脚本只能提交到这两个固定路径（防御性收紧）
const INGEST_PATHS = new Set(["/ingest/cards", "/ingest/detail"]);

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
  if (autoReload) chrome.runtime.reload();     // 解包扩展按磁盘代码重载（浏览器无关）
  return { ok: true, latest, update_available: true, reloading: !!autoReload };
}

async function gfetch(path, options) {
  try {
    const response = await fetch(GATEWAY + path, options);
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { /* 非 JSON 保持 null */ }
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

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return undefined;
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
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === UPDATE_ALARM) checkSelfUpdate(true);
});

/** 让当前活动标签页的内容脚本立即采集本页（弹窗「采集本页」入口）。 */
async function collectActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || !tab.id) return { ok: false, err: "没有活动标签页" };
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "collect-now" });
    return { ok: true };
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
});
