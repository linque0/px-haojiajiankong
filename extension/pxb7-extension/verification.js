/** 访问验证观察器：只读取可见DOM；等待用户完成验证，不操作验证控件。 */
(function(root) {
  "use strict";
  const SELECTORS = ["#aliyunCaptcha-sliding-slider", ".nc_iconfont.btn_slide", "#waf_nc_block", ".aliyunCaptcha-show", "#nocaptcha"];
  const PROMPT = /请按住滑块|拖动.{0,12}(?:最右|验证)|访问验证|安全验证|请完成.{0,8}验证/;
  function visible(node, win) {
    if (!node || node.isConnected === false) return false;
    for (let el = node; el; el = el.parentElement) {
      if (el.hidden) return false;
      const style = win.getComputedStyle ? win.getComputedStyle(el) : el.style || {};
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
    }
    const rect = node.getBoundingClientRect?.();
    return !rect || (rect.width > 0 && rect.height > 0);
  }
  function detect(doc, win) {
    for (const selector of SELECTORS) {
      for (const node of Array.from(doc.querySelectorAll?.(selector) || [])) {
        if (!visible(node, win)) continue;
        const slider = selector.includes("slider") || selector.includes("btn_slide");
        if (slider || PROMPT.test(node.innerText || node.textContent || "")) {
          return {blocked:true, kind:slider ? "slider" : "verification", selector};
        }
      }
    }
    // 全页挑战常没有原站点卡片。只使用页面标题，不把角标/商品描述当验证码。
    if (/验证码|安全验证|访问验证|访问受限/.test(doc.title || "")) return {blocked:true, kind:"verification", selector:"title"};
    return {blocked:false};
  }
  async function waitForClear(doc, win, options = {}) {
    const sleep = options.sleep || (ms => new Promise(resolve => win.setTimeout(resolve, ms)));
    const now = options.now || Date.now;
    let paused = !!options.alreadyPaused, observed = !options.requireObserved, clearSince = null;
    while (!options.alive || options.alive()) {
      const state = detect(doc, win);
      if (state.blocked) {
        observed = true; clearSince = null;
        if (!paused) {
          if (options.onBlocked && await options.onBlocked(state) === false) return {ok:false, error:"task-changed"};
          paused = true;
        }
      } else if (!paused) {
        return {ok:true, waited:false};
      } else if (observed || options.confirmed?.()) {
        if (clearSince === null) clearSince = now();
        if (now() - clearSince >= (options.stableMs ?? 1000)) {
          if (options.onClear && await options.onClear() === false) return {ok:false, error:"task-changed"};
          return {ok:true, waited:true};
        }
      }
      await sleep(250); // 被动观察，没有站点请求、滑块拖动或刷新重试。
    }
    return {ok:false, error:"cancelled-or-route-change"};
  }
  root.PXB7_VERIFICATION = {detect, visible, waitForClear};
  if (typeof module !== "undefined" && module.exports) module.exports = root.PXB7_VERIFICATION;
})(typeof window !== "undefined" ? window : globalThis);
