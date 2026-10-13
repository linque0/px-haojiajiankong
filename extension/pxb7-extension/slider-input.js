/** 一次普通滑块输入。只读公开控件几何，通过浏览器Input拖动，不读取验证凭据。 */
(function(root) {
  "use strict";
  function allowedPage(value) {
    try {
      const url = new URL(value);
      return url.origin === "https://www.pxb7.com" && !url.username && !url.password
        && /^\/(?:buy\/\d+(?:\/\d+)?|product\/\d{6,24}\/\d+)\/?$/.test(url.pathname);
    } catch (_) { return false; }
  }
  // 固定函数，不接受页面消息传入的JS、坐标、选择器或CDP命令。
  function publicGeometry() {
    const slider = document.querySelector("#aliyunCaptcha-sliding-slider");
    const track = document.querySelector("#aliyunCaptcha-sliding-body");
    if (!slider || !track || !/请按住滑块[\s\S]{0,30}最右|拖动到最右/.test(track.innerText || track.textContent || "")) {
      return {error:"unsupported-challenge"};
    }
    for (const node of [slider, track]) {
      for (let el = node; el; el = el.parentElement) {
        const style = getComputedStyle(el);
        if (el.hidden || style.display === "none" || /hidden|collapse/.test(style.visibility)) return {error:"challenge-not-visible"};
      }
    }
    const s = slider.getBoundingClientRect(), t = track.getBoundingClientRect();
    const x = s.left + s.width / 2, y = s.top + s.height / 2, endX = t.right - s.width / 2;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !(hit === slider || slider.contains(hit))) return {error:"challenge-obscured"};
    return {url:location.href, x, y, endX, width:t.width, height:s.height,
      viewportWidth:innerWidth, viewportHeight:innerHeight};
  }
  function validGeometry(g, url) {
    return g && !g.error && g.url === url
      && [g.x, g.y, g.endX, g.width, g.height, g.viewportWidth, g.viewportHeight].every(Number.isFinite)
      && g.width >= 60 && g.width <= 1200 && g.height >= 10 && g.height <= 120
      && g.endX - g.x >= 20 && g.endX - g.x <= g.width
      && g.x >= 0 && g.endX < g.viewportWidth && g.y >= 0 && g.y < g.viewportHeight;
  }
  async function attempt(chrome, tabId, pageUrl, options = {}) {
    if (!Number.isInteger(tabId) || !allowedPage(pageUrl)) return {ok:false, error:"unsupported-page"};
    const target = {tabId}, sleep = options.sleep || (ms => new Promise(r => setTimeout(r, ms)));
    let attached = false, pressed = false, point;
    const alive = async () => (!options.alive || await options.alive())
      && (await chrome.tabs.get(tabId)).url === pageUrl;
    const send = (method, params) => chrome.debugger.sendCommand(target, method, params);
    try {
      if (!await alive()) return {ok:false, error:"task-changed"};
      await chrome.debugger.attach(target, "1.3");
      attached = true;
      if (!await alive()) return {ok:false, error:"task-changed"};
      const res = await send("Runtime.evaluate", {expression:`(${publicGeometry.toString()})()`, returnByValue:true});
      const g = res.result?.value;
      if (!validGeometry(g, pageUrl)) return {ok:false, error:g?.error || "invalid-geometry"};
      point = {x:g.x, y:g.y};
      if (!await alive()) return {ok:false, error:"task-changed"};
      await send("Input.dispatchMouseEvent", {type:"mouseMoved", ...point});
      await send("Input.dispatchMouseEvent", {type:"mousePressed", ...point, button:"left", buttons:1, clickCount:1});
      pressed = true;
      // 普通匀速拖动；不伪造设备、身份或验证结果，也不合成人类行为指纹。
      for (let step = 1; step <= 18; step++) {
        await sleep(30);
        if (!await alive()) return {ok:false, error:"task-changed"};
        point = {x:g.x + (g.endX - g.x) * step / 18, y:g.y};
        await send("Input.dispatchMouseEvent", {type:"mouseMoved", ...point, button:"left", buttons:1});
      }
      await send("Input.dispatchMouseEvent", {type:"mouseReleased", ...point, button:"left", buttons:0, clickCount:1});
      pressed = false;
      return {ok:true, inputSent:true}; // 页面稳定解除与商品业务就绪由原采集逻辑核验。
    } catch (_) {
      return {ok:false, error:"debugger-unavailable"}; // 不传输任意异常正文或页面数据。
    } finally {
      if (attached) {
        if (pressed) {
          try {
            if ((await chrome.tabs.get(tabId)).url === pageUrl) await send("Input.dispatchMouseEvent",
              {type:"mouseReleased", ...point, button:"left", buttons:0, clickCount:1});
          } catch (_) { /* 标签页已关闭/浏览器已断开 */ }
        }
        try { await chrome.debugger.detach(target); } catch (_) { /* 已断开 */ }
      }
    }
  }
  root.PXB7_SLIDER_INPUT = {allowedPage, attempt};
  if (typeof module !== "undefined" && module.exports) module.exports = root.PXB7_SLIDER_INPUT;
})(typeof self !== "undefined" ? self : globalThis);
