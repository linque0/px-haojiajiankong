/**
 * pxb7 采集助手 —— 单页内"多采几张"的加载决策（扩展与油猴共用同一套判定）
 *
 * 为什么需要它：pxb7 列表页一次只在 DOM 里渲染 16 张卡片（2026-10-03 实测：原神/鸣潮/
 * 三角洲/火影四个列表页的 raw dump 都恰好 16 张，且页面里没有分页控件）。采集器只读
 * "用户当前页面已渲染的 DOM"，所以一次采集就是 16 张。想要更多，只能在页面里触发站点
 * 自己的"加载更多"（滚动到底，或点击站点渲染出的加载控件），等新卡片渲染出来后再把整页
 * DOM 发给网关——不猜 URL、不请求站外接口、不绕过站点风控。
 *
 * 本文件是纯逻辑 + DOM 只读辅助：数卡片 / 找加载控件 / 给下一步动作 / 滚到底。
 * 不发任何网络请求。同时导出给 Node（tests/test_sweep_js.py 用 node 跑决策表）。
 */
(function (root) {
  "use strict";

  const DEFAULTS = Object.freeze({
    minCards: 1,
    defaultCards: 16,      // 默认仍采集一页，可填写任意整数
    maxCards: 200,         // 目标张数上限（12 轮 × 16 ≈ 192）
    maxRounds: 12,         // 加载轮次上限
    maxStalled: 2,         // 连续 N 轮没有新卡片即停（说明没有更多了）
    intervalMs: 2000,      // 轮次间隔：每 2 秒触发一次站点自身的懒加载（礼貌节奏）
  });

  const MORE_SELECTORS = [
    ".t-pagination__btn-next",
    "[class*='load-more']",
    "[class*='loadMore']",
    "[class*='next-page']",
  ];
  const MORE_TEXTS = ["下一页", "加载更多", "查看更多", "更多商品"];

  /** 目标张数：非法值回落一页（16），上限 200 */
  function clampTarget(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return DEFAULTS.defaultCards;
    return Math.min(DEFAULTS.maxCards, Math.max(DEFAULTS.minCards, Math.round(n)));
  }

  /** 当前 DOM 里的卡片数（与 parser 的 CARD_SELECTORS 同源：语义属性优先，逐级兜底） */
  function countCards(doc) {
    if (!doc || !doc.querySelectorAll) return 0;
    const byAttr = doc.querySelectorAll("[productid]").length;
    if (byAttr > 0) return byAttr;
    const byClass = doc.querySelectorAll(".middleCard").length;
    if (byClass > 0) return byClass;
    return doc.querySelectorAll("a[href*='/product/']").length;
  }

  /** 只选择前 N 个唯一商品；链接直接来自页面，不拼接或猜测详情 URL。 */
  function selectCards(doc, target, baseUrl) {
    let cards = Array.from(doc.querySelectorAll(".middleCard[productid]"));
    if (!cards.length) cards = Array.from(doc.querySelectorAll("[data-listing-id], a[href*='/product/']"));
    const seen = new Set(), out = [];
    for (const card of cards) {
      const link = card.closest("a[href*='/product/']") || card.querySelector("a[href*='/product/']");
      const href = link && link.getAttribute("href");
      if (!href) continue;
      let url;
      try { url = new URL(href, baseUrl); } catch (_) { continue; }
      const match = url.pathname.match(/^\/product\/(\d{6,24})(?:\/|$)/);
      if (url.origin !== "https://www.pxb7.com" || !match) continue;
      const id = card.getAttribute("productid") || card.getAttribute("data-listing-id") || match[1];
      if (id !== match[1] || seen.has(id)) continue;
      seen.add(id);
      out.push({id, url:url.href, card, node:link || card});
      if (out.length >= target) break;
    }
    return out;
  }

  /** 在上传副本中标记选中卡片，parser 优先使用语义属性，避免把推荐位/超额卡片入库。 */
  function capture(doc, selected, titles = {}) {
    const clone = doc.documentElement.cloneNode(true);
    for (const node of clone.querySelectorAll("[data-listing-id]")) node.removeAttribute("data-listing-id");
    const chosen = new Set(selected.map(item => item.id));
    const refs = selectCards({querySelectorAll:s => clone.querySelectorAll(s)}, 200, "https://www.pxb7.com");
    for (const item of refs) {
      if (!chosen.has(item.id)) continue;
      item.node.setAttribute("data-listing-id", item.id);
      if (titles[item.id]) {
        item.card.setAttribute("data-pxb7-full-title", titles[item.id]);
        item.card.setAttribute("data-pxb7-full-title-id", item.id);
      }
    }
    return clone.outerHTML;
  }

  function isDisabled(el) {
    if (!el) return true;
    if (el.disabled) return true;
    if (el.getAttribute && el.getAttribute("aria-disabled") === "true") return true;
    return /disabled/.test(String(el.className || ""));
  }

  /** 站点若渲染了"加载更多/下一页"控件就点它；找不到返回 null（退回滚动触发） */
  function findMoreControl(doc) {
    if (!doc || !doc.querySelector) return null;
    for (const selector of MORE_SELECTORS) {
      const el = doc.querySelector(selector);
      if (el && !isDisabled(el)) return el;
    }
    const nodes = doc.querySelectorAll("button, a, [role='button'], .t-button");
    for (const el of nodes) {
      const text = String(el.textContent || "").trim();
      if (MORE_TEXTS.indexOf(text) >= 0 && !isDisabled(el)) return el;
    }
    return null;
  }

  /** 纯决策：done-target / done-rounds / done-stalled / click / scroll */
  function nextAction(state) {
    if (state.cards >= state.target) return "done-target";
    if (state.rounds >= state.maxRounds) return "done-rounds";
    if (state.stalled >= state.maxStalled) return "done-stalled";
    return state.hasMoreControl ? "click" : "scroll";
  }

  /** 把页面（窗口或列表内部滚动容器）滚到底，触发站点自身的懒加载 */
  function scrollForMore(win, doc) {
    if (!doc) return false;
    let moved = false;
    if (win && typeof win.scrollTo === "function") {
      const before = typeof win.scrollY === "number" ? win.scrollY : 0;
      const docEl = doc.documentElement || doc.body || {};
      win.scrollTo(0, docEl.scrollHeight || 0);
      moved = moved || (typeof win.scrollY === "number" && win.scrollY !== before);
    }
    // 部分页面把列表放进内部滚动容器：向上找第一个可滚动的祖先并滚到底
    let node = doc.querySelector(".product-list, [class*='product-list'], main");
    while (node && node !== doc.body) {
      const style = win && win.getComputedStyle ? win.getComputedStyle(node) : null;
      const overflowY = style ? style.overflowY : "";
      if ((overflowY === "auto" || overflowY === "scroll")
          && node.scrollHeight > node.clientHeight) {
        node.scrollTop = node.scrollHeight;
        moved = true;
        break;
      }
      node = node.parentElement;
    }
    return moved;
  }

  const api = { DEFAULTS, clampTarget, countCards, selectCards, capture, findMoreControl, nextAction, scrollForMore };
  root.PXB7_SWEEP = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
