/**
 * pxb7 采集助手（前端 B）—— 弹窗看板（扩展 UI）
 *
 * 数据与操作全部对接本机网关的回环 HTTP 接口（host_permissions 已声明，扩展页 fetch 不受页面 CORS 限制）。
 * 说明（安全边界）：本文件**不含任何 SQL / 数据库访问**——所有持久化都在 Python 网关内
 * 以参数化 DuckDB 查询完成；标签页操作集中在背景服务，此处只有固定的 HTTP 路径与 UI 渲染。
 */
"use strict";

const GATEWAY = "http://127.0.0.1:8765";
// Native Messaging 宿主：一键启停本机网关（安装：双击 安装网关启停.bat，登记 HKCU 注册表）
const NATIVE_HOST = "com.pxb7.gateway";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s === null || s === undefined ? "—" : s)
  .replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString("zh-CN");

async function gfetch(path, options) {
  try {
    const response = await fetch(GATEWAY + path, options);
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) { /* 非 JSON 响应 */ }
    return { ok: true, status: response.status, data };
  } catch (e) {
    return { ok: false, err: "网关未运行；点上方「启动网关」或双击桌面「pxb7采集看板」" };
  }
}

function note(text, bad) {
  const el = $("note");
  el.textContent = text;
  el.className = "note" + (bad ? " bad" : "");
  setTimeout(() => { el.textContent = ""; }, 5000);
}

function errorText(result, fallback) {
  if (result && result.data && typeof result.data.error === "string") return result.data.error;
  if (result && typeof result.err === "string") return result.err;
  if (result && typeof result.error === "string") return result.error;
  return fallback;
}

function renderTiles(s) {
  const st = s.stats;
  const rate = st.cards_seen ? Math.round((st.cards_parsed / st.cards_seen) * 100) + "%" : "—";
  const tile = (k, v) =>
    `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $("tiles").innerHTML =
    tile("快照", fmt(s.db.snapshot_rows)) + tile("解析率", rate) + tile("新增", fmt(st.new_listings)) +
    tile("词表命中", fmt(s.db.keyword_hits)) + tile("详情回填", fmt(st.details_stored)) +
    tile("拦截拒收", fmt(st.risk_pages_rejected));
}

function renderBars(rows) {
  const top = Math.max(1, ...rows.map((r) => r.rows));
  const items = rows.map((r) =>
    `<div class="bar"><span class="label">${esc(r.round)}</span>` +
    `<span class="track"><span class="fill" style="width:${(r.rows / top) * 100}%"></span></span>` +
    `<span class="num">${fmt(r.rows)}</span></div>`).join("");
  $("rounds").innerHTML = items || '<div style="color:#8b93a7">暂无轮次数据——去 pxb7 逛一圈</div>';
}

function renderBatches(batches) {
  const items = batches.slice(-4).reverse().map((b) => {
    const head = esc(String(b.at).slice(11));
    const body = b.kind === "cards"
      ? `${b.cards_parsed}/${b.cards_seen} 张 → 入库 ${b.snapshots}`
      : `收藏 ${b.favorites_cnt === null ? "未公示" : b.favorites_cnt}`;
    return `<div>${head} ${b.kind === "cards" ? "列表" : "详情"} · ${body}</div>`;
  }).join("");
  $("batches").innerHTML = items || '<div style="color:#8b93a7">暂无批次</div>';
}

function renderError(text) {
  $("state").textContent = "网关未连接";
  $("state").className = "state bad";
  $("gw-start").style.display = "inline-block";
  $("tiles").innerHTML = "";
  $("rounds").innerHTML = "";
  $("batches").innerHTML = `<div class="note bad">${esc(text)}</div>`;
}

// ---------- 一键启停网关（Native Messaging 宿主：安装网关启停.bat 登记） ----------
$("gw-start").addEventListener("click", () => {
  $("gw-start").disabled = true;
  note("正在启动网关…");
  chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "start" }, (reply) => {
    $("gw-start").disabled = false;
    if (chrome.runtime.lastError) {
      note("启动失败：" + chrome.runtime.lastError.message
        + "（请先双击 安装网关启停.bat 登记本扩展）", true);
      return;
    }
    if (reply && reply.ok && reply.running) {
      note(reply.started ? "网关已启动 ✓" : (reply.note || "网关已在运行"));
      refresh();
    } else {
      note("启动失败：" + (reply && reply.error || "未知错误"), true);
    }
  });
});

// ---------- 采集目标（多选：只采集勾选的游戏） ----------
let targetsSig = "";

function renderTargets(t) {
  const sig = JSON.stringify(t);
  if (sig === targetsSig) return;          // 轮询刷新不重绘，避免打断勾选
  targetsSig = sig;
  const box = $("targets");
  const rows = (t.available || []).map((x) =>
    `<label class="tg${x.enabled ? "" : " disabled"}">` +
    `<input type="checkbox" data-task="${esc(x.task_id)}"${x.selected ? " checked" : ""}` +
    `${x.enabled ? "" : " disabled"}> <span>${esc(x.name)}</span>` +
    `<span class="cnt">${fmt(x.snapshots)} 行</span></label>`).join("")
    || '<div class="muted">无可用任务</div>';
  const mode = t.mode === "selected"
    ? `已选 ${(t.effective || []).length} 个目标（${esc((t.effective || []).join("、"))}）`
    : `未勾选 → 使用网关启动任务（${esc(t.default_task || "—")}）`;
  box.innerHTML = rows + `<div class="mode">${mode}；只采集勾选游戏的页面</div>`;
  box.querySelectorAll("input[data-task]").forEach((el) =>
    el.addEventListener("change", saveTargets));
}

async function saveTargets() {
  const ids = Array.from($("targets").querySelectorAll("input[data-task]:checked"))
    .map((el) => el.dataset.task);
  const res = await gfetch("/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targets: ids }),      // 部分更新：其余配置项保持不变
  });
  if (res.ok && res.data && res.data.ok) {
    renderTargets(res.data.targets || {});
    bfOffset = 0; bfSelection.clear(); await refreshBackfill();
    note(ids.length ? `采集目标已保存（${ids.length} 个）✓` : "已清空目标 → 使用默认任务 ✓");
  } else {
    note("目标保存失败：" + errorText(res, "未知错误"), true);
  }
}

// ---------- 数据存放位置（展示 + 打开/复制） ----------
let pathsSig = "";

function renderPaths(p) {
  const sig = JSON.stringify(p);
  if (sig === pathsSig) return;           // 轮询刷新不重绘，避免打断点击/复制
  pathsSig = sig;
  const items = p.items || [];
  $("paths").innerHTML = items.map((it) =>
    `<div class="path-row"><span class="pl" title="${esc(it.label)}">${esc(it.label)}</span>` +
    `<code title="${esc(it.path)}">${esc(it.path)}</code>` +
    `<button class="mini" data-copy="${esc(it.path)}" title="复制路径">复制</button>` +
    `<button class="mini" data-open="${esc(it.key)}" title="在资源管理器中打开">打开</button>` +
    `</div>`).join("") +
    `<div class="muted">覆写文件：${esc(p.override_file || "—")}</div>`;
  $("paths").querySelectorAll("button[data-copy]").forEach((el) =>
    el.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(el.dataset.copy);
        note("路径已复制 ✓");
      } catch (e) {
        note("复制失败；可在完整看板手动复制", true);
      }
    }));
  $("paths").querySelectorAll("button[data-open]").forEach((el) =>
    el.addEventListener("click", async () => {
      const res = await gfetch("/open-folder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: el.dataset.open }),
      });
      if (!(res.ok && res.data && res.data.ok)) {
        note("打开失败：" + errorText(res, "无响应"), true);
      }
    }));
}

let settingsDirty = false;
function modeHelp() {
  const detail = $("c-mode").value === "detail";
  const auto = $("c-mode").value === "auto";
  $("mode-help").textContent = detail
    ? "从列表保存清单和价格，再逐个打开详情采集；关闭弹窗仍可继续。切换方式用于下一次任务。"
    : auto ? "先保存价格、核验公开全文与字段，只有缺项进入详情。逻辑任务按最多200条分块，配置在开始时冻结。新任务目的和规模仅扩展支持。"
    : "读取列表全文；缺失时按商品编号限速获取，可能受网站预检限制。";
  // 两种间隔互不共用：详情模式启用详情间隔、停用列表间隔；列表模式相反。
  $("c-title-interval").disabled = detail;
  $("c-detail-interval").disabled = !detail && !auto;
}
["c-mode", "c-purpose", "c-target", "c-ids", "c-scan", "c-rounds", "c-budget", "c-cache-ttl", "c-force-text", "c-verification", "c-auto", "c-interval", "c-cards", "c-settle", "c-title-interval", "c-detail-interval", "c-debug"].forEach(id => {
  $(id).addEventListener("input", () => {settingsDirty = true; modeHelp();});
});
function renderProgress(p) {
  if (!p) return;
  const labels = {running:"进行中", waiting_verification:"等待网站验证", needs_reconcile:"位置丢失待对账（可取消）", done:"已完成", partial:"部分完成", error:"失败", cancelled:"已取消", paused:"已暂停"};
  const busy = ["running", "waiting_verification", "needs_reconcile"].includes(p.status);
  $("resume-verification").hidden = p.status !== "waiting_verification";
  $("progress-mode").textContent = (p.mode === "backfill" ? "信息补录" : p.mode === "auto" ? "自动选择" : p.mode === "detail" ? "详情页采集" : "列表采集") + " · " + (labels[p.status] || p.status);
  const completed = p.mode === "backfill" || p.purpose === "detail_enrich" ? p.processed : p.purpose === "price_refresh" ? p.prices || 0 : p.new_succeeded ?? p.processed;
  $("progress-count").textContent = `${completed}/${p.requested} ${p.mode === "backfill" ? "张" : p.purpose === "price_refresh" ? "个价格观察" : p.purpose === "detail_enrich" ? "个补齐任务" : "个新账号"}`;
  $("collect-progress").value = Math.min(100, Math.round(completed / Math.max(1, p.requested) * 100));
  $("progress-phase").textContent = p.phase;
  const seconds = Math.max(0, Math.round(((p.completedAt || (busy ? Date.now() : p.updatedAt)) - p.startedAt) / 1000));
  $("progress-detail").textContent = `入库 ${p.succeeded} · 失败 ${p.failed} · ${seconds} 秒`
    + (p.incomplete ? ` · 待补齐 ${p.incomplete}` : "");
  if (p.mode !== "backfill") $("progress-detail").textContent +=
    ` · 更新 ${p.updated || 0} · 重复跳过 ${p.duplicates || 0} · 自动重试 ${p.retried || 0} · 待补录 ${p.manual_pending || 0}`;
  if (p.batch) $("progress-detail").textContent += ` · 扫描 ${p.scanned || 0} · 价格 ${p.prices || 0} · 完整文字 ${p.batch_counts?.text_complete || p.text_complete || 0} · 回退 ${p.fallback || 0} · 执行块 ${p.blocks || 0}`
    + (p.export && !p.export.ok ? " · 数据已入库，导出待重试" : p.export ? " · 导出已同步" : " · 导出等待中");
  if(p.concurrency) $("progress-detail").textContent += ` · 完整入库 ${p.complete_count||0} · 在途 ${p.in_flight||0} · 并发 ${p.concurrency} · 响应 ${p.response_complete||0} · DOM回退 ${p.dom_fallback||0}`;
  if(p.timings) $("progress-detail").textContent += ` · 发现 ${(p.timings.discovery_ms/1000).toFixed(1)}s · 请求累计 ${(p.timings.request_ms/1000).toFixed(1)}s · 解析累计 ${(p.timings.parse_ms/1000||0).toFixed(1)}s · 入库累计 ${(p.timings.commit_ms/1000||0).toFixed(1)}s · 提交累计 ${(p.timings.submit_ms/1000).toFixed(1)}s · 验证 ${(p.timings.verification_ms/1000).toFixed(1)}s`;
  if (p.export?.handoff) $("progress-detail").textContent += p.export.handoff.ok ? " · 趋势视图已更新（估值发布待交接）" : " · 分析交接待重试";
  $("cancel-collect").disabled = !busy;
  $("grab").disabled = busy || bfStarting;
  bfBusy = busy;
  updateBackfillButtons();
  if (p.mode === "backfill" && p.updatedAt !== bfProgressAt) {
    bfProgressAt = p.updatedAt;
    if (!busy) $("bf-note").textContent = p.phase;
    refreshBackfill();
  }
}
function refreshProgress() {
  chrome.runtime.sendMessage({type:"collection-status"}, reply => {
    if (!chrome.runtime.lastError && reply?.ok) renderProgress(reply.progress);
  });
}
$("resume-verification").addEventListener("click", () => {
  chrome.runtime.sendMessage({type:"verification-confirm"}, reply => {
    note(reply?.ok ? "已确认，页面恢复后继续原任务" : errorText(reply, "页面仍未完成验证"), !reply?.ok);
    refreshProgress();
  });
});
$("cancel-collect").addEventListener("click", () => {
  chrome.runtime.sendMessage({type:"collection-cancel"}, reply => {
    if (reply?.ok) {note("采集已取消，已入库数据保留"); refreshProgress();}
    else note(errorText(reply, "取消失败"), true);
  });
});
async function refresh() {
  const res = await gfetch("/stats");
  if (!res.ok || !res.data || !res.data.ok) {
    renderError(res.err || "无响应");
    return;
  }
  const s = res.data;
  const state = $("state");
  state.textContent = s.stats.last_batch_at
    ? "已连接 · " + s.stats.last_batch_at.slice(11) : "已连接 · 暂无批次";
  state.className = "state";
  $("gw-start").style.display = "none";   // 已连接即收起启动按钮（断开时由 renderError 显示）
  renderTiles(s);
  renderBars((s.db.rounds || []).slice(-5));
  renderBatches(s.recent_batches || []);
  if (s.targets) renderTargets(s.targets);
  if (s.paths) renderPaths(s.paths);
  if (settingsDirty) return;
  const c = s.config || {};
  $("c-mode").value = c.collection_mode || "list";
  $("c-detail-transport").value = c.detail_transport || "page";
  $("c-detail-concurrency").value = String(c.detail_concurrency ?? 0);
  $("c-purpose").value = c.purpose || "discover";
  $("c-target").value = c.target_count || 0;
  $("c-ids").value = (c.selected_listing_ids || []).join(", ");
  $("c-scan").value = c.scan_budget || 3200;
  $("c-rounds").value = c.max_scan_rounds || 12;
  $("c-budget").value = c.daily_access_budget || 1000;
  $("c-cache-ttl").value = c.text_cache_ttl_minutes || 10;
  $("c-force-text").checked = !!c.force_text_refresh;
  $("c-verification").value = c.verification_mode || "manual";
  modeHelp();
  $("c-auto").checked = !!c.auto_ingest;
  $("c-interval").value = c.reingest_interval_min;
  $("c-cards").value = c.cards_target || 16;
  $("c-settle").value = c.spa_settle_ms;
  $("c-title-interval").value = (c.title_interval_ms ?? 3000) / 1000;
  $("c-detail-interval").value = (c.detail_interval_ms ?? 0) / 1000;
  $("c-debug").checked = !!c.debug;
}

// ---------- 操作 ----------
async function saveSettings() {
  for (const id of ["c-target", "c-scan", "c-rounds", "c-budget", "c-cache-ttl", "c-cards", "c-interval", "c-settle", "c-title-interval", "c-detail-interval"]) {
    if (!$(id).checkValidity()) {$(id).reportValidity(); return false;}
  }
  const res = await gfetch("/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      collection_mode: $("c-mode").value,
      detail_transport: $("c-detail-transport").value,
      detail_concurrency: Number($("c-detail-concurrency").value),
    purpose:$("c-purpose").value, target_count:Number($("c-target").value),
    selected_listing_ids:$("c-ids").value.trim() ? $("c-ids").value.trim().split(/[\s,，]+/) : [],
    scan_budget:Number($("c-scan").value), max_scan_rounds:Number($("c-rounds").value),
    daily_access_budget:Number($("c-budget").value), text_cache_ttl_minutes:Number($("c-cache-ttl").value),
    force_text_refresh:$("c-force-text").checked,
      verification_mode: $("c-verification").value,
      auto_ingest: $("c-auto").checked,
      reingest_interval_min: Number($("c-interval").value),
      cards_target: Number($("c-cards").value),
      spa_settle_ms: Number($("c-settle").value),
      title_interval_ms: Number($("c-title-interval").value) * 1000,
      detail_interval_ms: Number($("c-detail-interval").value) * 1000,
      debug: $("c-debug").checked,
    }),
  });
  if (res.ok && res.data && res.data.ok) {
    settingsDirty = false;
    note("设置已保存并生效 ✓");
    return true;
  } else {
    note("保存失败：" + errorText(res, "未知错误"), true);
    return false;
  }
}
$("save").addEventListener("click", saveSettings);

$("grab").addEventListener("click", async () => {
  if (!(await saveSettings())) return;
  $("grab").disabled = true;
  note("正在开始采集…");
  // 标签页操作集中在背景服务（chrome.tabs.*），弹窗只发固定消息类型
  chrome.runtime.sendMessage({ type: "collect-active" }, (reply) => {
    refreshProgress();
    if (chrome.runtime.lastError) {$("grab").disabled = false; note(chrome.runtime.lastError.message, true); return;}
    if (reply && reply.ok) {
      if (reply.queued) {note("详情队列已启动，可关闭弹窗；进度自动更新"); return;}
      $("grab").disabled = false;
      const stats = reply.title_collection;
      if (stats && stats.cards) {
        const missing = stats.cards - stats.captured;
        note(`本页已入库，全文 ${stats.captured}/${stats.cards}`
          + (missing ? `；${missing} 张待补齐，请查看页面角标后重采` : ""), missing > 0);
      } else note("已请求采集当前页（看页面右下角角标）");
    }
    else {$("grab").disabled = false; note(errorText(reply, "请求失败"), true);}
  });
});

$("full").addEventListener("click", () => {
  chrome.tabs.create({ url: new URL("/", GATEWAY).href });
});

$("stop").addEventListener("click", async () => {
  if (!window.confirm("停止本机采集服务？浏览页面的自动采集将暂停，直到再次打开看板/服务。")) return;
  const res = await gfetch("/shutdown", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (res.ok) note("服务已停止；再次使用：点「启动网关」按钮即可");
  else note("停止失败：" + errorText(res, "无响应"), true);
});

// ---------- 扩展自更新（按磁盘最新代码重载；解包扩展的「重新加载」自动化） ----------
let extUpdate = { update_available: false, latest: null };

function renderExtUpdate() {
  const own = chrome.runtime.getManifest().version;
  $("upd-ext").style.display = extUpdate.update_available ? "inline-block" : "none";
  $("ver").textContent = extUpdate.update_available
    ? `扩展 v${own} → v${extUpdate.latest}` : `扩展 v${own}`;
}

function checkExtUpdate(cb) {
  try {
    chrome.runtime.sendMessage({ type: "check-update" }, (reply) => {
      if (!chrome.runtime.lastError && reply && reply.latest) {
        extUpdate = { update_available: !!reply.update_available, latest: reply.latest };
      }
      renderExtUpdate();
      if (cb) cb();
    });
  } catch (e) {
    renderExtUpdate();
  }
}

$("upd-ext").addEventListener("click", () => {
  if (!window.confirm("重新加载扩展以应用新版本？（弹窗会关闭，属正常现象）")) return;
  chrome.runtime.sendMessage({ type: "apply-update" }, () => {
    if (chrome.runtime.lastError) note(chrome.runtime.lastError.message, true);
    else window.close();
  });
});

// ---------- 最新快照查缺 / 补录 ----------
let bfOffset = 0, bfTotal = 0, bfBusy = false, bfStarting = false, bfProgressAt = null;
let bfRows = [], bfResults = {}, bfPending = {}, bfLoading = false, bfDirty = false, bfAllLoading = false;
const bfSelection = new Map();
const reasonText = reasons => (reasons || []).map(r => `${r.label}：${r.reason}`).join("；");
const bfError = error => ({"target-not-selected":"请先勾选对应游戏的采集目标",
  "selection-changed-refresh-list":"缺失清单已变化，请刷新后重新勾选",
  "latest-snapshot-changed":"已出现新快照，旧目标已跳过，请刷新清单",
  "delete-selection-limit":"每次请选择 1～200 个商品删除",
  "delete-confirmation-required":"请确认删除全部轮次",
  "delisted":"商品已下架，无法补录",
  "no-candidates":"当前范围已无待补录记录"})[error] || error;
async function extensionRequest(message) {
  try { return await chrome.runtime.sendMessage(message); }
  catch (error) { return {ok:false, err:error.message}; }
}
function updateBackfillButtons() {
  const collectable = Array.from(bfSelection.values()).some(row => row.collectable);
  $("bf-selected").textContent = `补录选中（${bfSelection.size}）`;
  $("bf-selected").disabled = bfBusy || bfStarting || !collectable;
  $("bf-all").textContent = bfSelection.size ? "取消全选" : "全选";
  $("bf-all").disabled = bfBusy || bfStarting || bfAllLoading;
  $("bf-delete").textContent = `删除选中（${bfSelection.size}）`;
  $("bf-delete").disabled = bfBusy || bfStarting || !bfSelection.size || bfSelection.size > 200;
  $("bf-check-failed").disabled = bfBusy || bfStarting;
}
function renderBackfillRows() {
  $("bf-list").innerHTML = bfRows.map(row => {
    const saved = bfResults[row.id];
    // 弹窗本地记录优先，其次网关台账（跨设备/重装后仍能看到最近补录状态）。
    const result = saved?.snapshot_round === row.snapshot_round ? saved
      : (row.last_backfill?.snapshot_round === row.snapshot_round ? row.last_backfill : null);
    const latest = result ? `最近补录：${({complete:"已补齐", incomplete:"仍有缺项", failed:"失败"})[result.status] || result.status}`
      + (result.error ? `（${bfError(result.error)}）` : "")
      + ` · ${new Date(result.at).toLocaleString("zh-CN")}` : "尚无补录记录";
    return `<label class="bf-item"><input type="checkbox" data-bf-id="${esc(row.id)}"`
      + `${bfSelection.has(row.id) ? " checked" : ""}>`
      + `<div><a href="https://www.pxb7.com/product/${encodeURIComponent(row.id)}/1" target="_blank" rel="noreferrer">${esc(row.id)}</a>`
      + ` · ${esc(row.game_name)}<div class="muted">${esc(row.snapshot_round)}</div>`
      + `<div>${esc(reasonText(row.missing))}</div><div class="muted">${esc(latest)}${row.collectable ? "" : "；请先勾选该游戏采集目标"}</div></div></label>`;
  }).join("") || '<div class="muted">该范围暂无缺失记录</div>';
  $("bf-list").querySelectorAll("input[data-bf-id]").forEach(input => input.addEventListener("change", () => {
    const row = bfRows.find(r => r.id === input.dataset.bfId);
    if (input.checked) bfSelection.set(row.id, {id:row.id, snapshot_round:row.snapshot_round, collectable:row.collectable});
    else bfSelection.delete(row.id);
    updateBackfillButtons();
  }));
  updateBackfillButtons();
}
async function refreshBackfillState() {
  const response = await extensionRequest({type:"backfill-state"});
  if (!response?.ok) return;
  bfResults = response.results || {}; bfPending = response.pending || {};
  const pending = Object.values(bfPending);
  $("bf-sync-retry").hidden = !pending.length;
  $("bf-sync").textContent = pending.length
    ? "数据表待同步：" + pending.map(r => `${r.game_id} · ${r.message || r.error}`
      + (r.path ? ` · ${r.path}` : "")).join("；")
    : "数据表同步：无待重试项";
}
async function refreshBackfill(force = false) {
  if (bfLoading) {
    if (force) bfDirty = true;   // 在飞查询结束后补跑一次：删除/启动后的强刷不被重入锁吞掉
    return;
  }
  bfLoading = true;
  const game = $("bf-game").value;
  try {
    const response = await extensionRequest({type:"backfill-query", game_id:game, offset:bfOffset});
    if (!(response?.ok && response.data?.ok)) {
      $("bf-list").textContent = "查询失败：" + errorText(response, "无响应");
      bfRows = []; bfTotal = 0; bfSelection.clear(); updateBackfillButtons(); return;
    }
    if (game !== $("bf-game").value) return;
    const data = response.data;
    bfTotal = data.total;
    if (bfOffset >= bfTotal && bfOffset > 0) {
      bfOffset = Math.max(0, Math.floor((bfTotal - 1) / 10) * 10);
      bfLoading = false; return refreshBackfill();
    }
    bfRows = data.rows;
    for (const row of bfRows) {
      if (bfSelection.has(row.id)) {
        if (bfSelection.get(row.id).snapshot_round !== row.snapshot_round) bfSelection.delete(row.id);
        else bfSelection.get(row.id).collectable = row.collectable;
      }
    }
    // 本次已补齐的选项无需再次提交；其他页选择仍保留。
    for (const [id] of bfSelection) {
      if (bfResults[id]?.status === "complete" && bfResults[id].snapshot_round === bfSelection.get(id).snapshot_round) bfSelection.delete(id);
    }
    $("bf-game").innerHTML = '<option value="selected">已勾选游戏</option><option value="all">全部游戏</option>'
      + data.games.map(g => `<option value="${esc(g.game_id)}">${esc(g.game_name)}（缺 ${g.missing}）</option>`).join("");
    $("bf-game").value = game;
    const coverage = game === "all" || game === "selected" ? "按游戏检查关键字段和角色／武器明细" : (data.coverage[game] || []).join("、");
    $("bf-coverage").textContent = "检测范围：" + coverage + "；鸣潮检查必填列；额外付费商品允许为空，不计缺失";
    $("bf-total").textContent = `${bfTotal} 条待补录`;
    $("bf-page").textContent = bfTotal ? `${Math.floor(bfOffset / 10) + 1}/${Math.ceil(bfTotal / 10)} 页` : "0 页";
    $("bf-prev").disabled = bfOffset === 0; $("bf-next").disabled = bfOffset + 10 >= bfTotal;
    await refreshBackfillState(); renderBackfillRows();
  } finally {
    bfLoading = false;
    if (game !== $("bf-game").value) { bfDirty = false; refreshBackfill(true); }
    else if (bfDirty) { bfDirty = false; refreshBackfill(true); }
  }
}
async function startBackfillSelected() {
  const items = Array.from(bfSelection.values());
  const ready = items.filter(item => item.collectable);
  const skipped = items.length - ready.length;
  if (!ready.length) {
    $("bf-note").textContent = "选中记录均未启用对应游戏采集目标；请先在「采集目标」勾选后再补录";
    return;
  }
  bfStarting = true; updateBackfillButtons();
  $("bf-note").textContent = "正在校验清单并备份数据库…";
  try {
    if (settingsDirty && !(await saveSettings())) return;
    const response = await extensionRequest({type:"backfill-start", options:{game_id:$("bf-game").value, items:ready}});
    if (response?.ok && response.progress) {
      $("bf-note").textContent = `补录已启动，可关闭弹窗${skipped ? `；已跳过 ${skipped} 条未启用游戏记录` : ""}。备份：${response.progress.backup || "已完成"}`;
      bfSelection.clear();
    } else {
      const err = errorText(response, "未知错误");
      $("bf-note").textContent = "补录未启动：" + bfError(err);
      if (err === "selection-changed-refresh-list") {
        bfSelection.clear();
        $("bf-note").textContent = "清单已变化，未启动补录；已刷新清单，请重新勾选";
      }
    }
    refreshProgress(); await refreshBackfill(true);
  } finally { bfStarting = false; updateBackfillButtons(); }
}

// 全选/取消全选（2026-10-06 用户指令，替代原「补录全部」）：全选拉取当前筛选
// 范围的全部页并入跨页选择；再次点击清空全部选择。
async function toggleSelectAll() {
  if (bfSelection.size) { bfSelection.clear(); renderBackfillRows(); return; }
  if (bfBusy || bfStarting || bfAllLoading) return;
  bfAllLoading = true; updateBackfillButtons();
  const game = $("bf-game").value;
  $("bf-note").textContent = "正在全选当前范围…";
  try {
    for (let offset = 0; offset < 2000; offset += 200) {
      const response = await extensionRequest({type:"backfill-query", game_id:game, offset, limit:200});
      if (!(response?.ok && response.data?.ok)) {
        $("bf-note").textContent = "全选失败：" + errorText(response, "无响应");
        return;
      }
      if (game !== $("bf-game").value) return;
      const rows = response.data.rows || [];
      for (const row of rows) {
        if (!bfSelection.has(row.id)) bfSelection.set(row.id,
          {id:row.id, snapshot_round:row.snapshot_round, collectable:row.collectable});
      }
      if (!rows.length || offset + 200 >= response.data.total) break;
    }
    $("bf-note").textContent = bfSelection.size
      ? `已全选 ${bfSelection.size} 条；可翻页取消个别勾选，再点「补录选中」或「删除选中」`
      : "当前范围没有可勾选的记录";
  } finally {
    bfAllLoading = false;
    renderBackfillRows();
  }
}
$("bf-game").addEventListener("change", () => {bfOffset = 0; bfSelection.clear(); refreshBackfill();});
$("bf-refresh").addEventListener("click", () => refreshBackfill(true));
$("bf-prev").addEventListener("click", () => {if (bfLoading) return; bfOffset = Math.max(0, bfOffset - 10); refreshBackfill();});
$("bf-next").addEventListener("click", () => {if (bfLoading) return; bfOffset += 10; refreshBackfill();});
$("bf-selected").addEventListener("click", () => startBackfillSelected());
$("bf-all").addEventListener("click", () => toggleSelectAll());
$("bf-check-failed").addEventListener("click", () => {
  for (const row of bfRows) {
    const result = bfResults[row.id];
    if (result?.snapshot_round === row.snapshot_round && ["failed", "incomplete"].includes(result.status)) {
      bfSelection.set(row.id, {id:row.id, snapshot_round:row.snapshot_round, collectable:row.collectable});
    }
  }
  renderBackfillRows();
});
$("bf-delete").addEventListener("click", async () => {
  const items = Array.from(bfSelection.values()).map(({id,snapshot_round}) => ({id,snapshot_round}));
  if (bfBusy || bfStarting || !items.length || items.length > 200) return;
  if (!window.confirm(`删除选中的 ${items.length} 个商品？\n将删除这些商品的全部入库轮次、价格变动、关键词及下架记录，并更新游戏 CSV。\n删除前自动备份；没有一键撤销，恢复备份会覆盖之后的数据库改动。\n商品编号：${items.map(item => item.id).join("、")}`)) return;
  bfStarting = true; updateBackfillButtons(); $("grab").disabled = true;
  $("bf-note").textContent = "正在校验缺失数据、备份并删除…";
  try {
    const response = await extensionRequest({type:"backfill-delete", options:{items,confirm_all_rounds:true}});
    if (response?.data?.ok) {
      const deleted = new Set(response.data.deleted_ids);
      for (const id of deleted) bfSelection.delete(id);
      // 立即从当前页移除已删行再强制刷新：删除效果即时可见，不等 10 秒定时器。
      bfRows = bfRows.filter(row => !deleted.has(row.id));
      renderBackfillRows();
      const synced = (response.data.results || []).filter(result => result.ok).map(result => result.game_id);
      const pending = (response.data.results || []).filter(result => !result.ok).map(result => result.game_id);
      $("bf-note").textContent = `已删除 ${response.data.deleted} 个商品、${response.data.counts.fct_listing_snapshot} 条快照。`
        + (synced.length ? `数据表已同步：${synced.join("、")}。` : "")
        + (pending.length ? `数据表待同步：${pending.join("、")}，请点击重试同步。` : "")
        + `备份：${response.data.backup}`;
    } else {
      const err = errorText(response, "无响应");
      $("bf-note").textContent = "删除失败：" + bfError(err);
      if (err === "selection-changed-refresh-list") {
        bfSelection.clear();
        $("bf-note").textContent = "选中记录已过期或已补齐，未删除任何数据；清单已刷新，请重新勾选";
      }
    }
    await refreshBackfill(true); await refresh();
  } finally {bfStarting = false; updateBackfillButtons(); $("grab").disabled = bfBusy;}
});
$("bf-sync-retry").addEventListener("click", async () => {
  $("bf-sync-retry").disabled = true;
  const response = await extensionRequest({type:"backfill-sync", game_ids:Object.keys(bfPending).map(Number)});
  $("bf-note").textContent = response?.data?.ok ? "同步重试已结束，请查看数据表状态" : "同步失败：" + errorText(response, "无响应");
  await refreshBackfillState(); $("bf-sync-retry").disabled = false;
});

// ---------- 启动 ----------
checkExtUpdate();
refresh();
setInterval(refresh, 3000);

refreshProgress();
setInterval(refreshProgress, 1000);
refreshBackfill();
setInterval(refreshBackfill, 10000);
