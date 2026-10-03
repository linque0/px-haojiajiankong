# tests/fixtures —— 真实样本与来源说明

## pxb7_waf_challenge_20261002.list.html / .list.meta.json

**这是本仓库目前唯一的一份真实站点 DOM 样本**，采集于 2026-10-02 22:58（UTC+8）。

| 项 | 值 |
|---|---|
| 采集命令 | `pxb7-price-monitor/.venv/Scripts/python.exe pxb7-price-monitor/run.py crawl --task genshin_official --pages 2 --detail 1 --guest --summary-json pxb7-price-monitor/data/runs/smoke.json` |
| URL | `https://www.pxb7.com/buy/10026/1`（原神·账号，docs/01 §2 实测 URL 规律） |
| run_id | `genshin_official-20261002T225748-2146` |
| 采集姿态 | `guest`（无登录态文件，按 v1.3 降级通道运行） |
| HTTP 状态 | 200（HTML 正常返回） |
| 文件大小 | 729,597 字节 |
| 页面性质 | **阿里云 WAF 滑块验证页**（不是商品列表页） |
| 关键证据 | 可见文本含「访问验证 为保证您的正常访问,请进行如下验证 TraceID: 0a24432f17909530707736241e4e86」与「请按住滑块，拖动到最右边」；`<div id="waf_nc_block" style="display: block;">`、`<div id="aliyunCaptcha-window-embed" class="aliyunCaptcha-show" …display:block>`；列表区为「加载中」/「暂无相关内容」 |
| 商品数据 | **无**：`¥` 0 处、`/product/` 链接 0 处、`window.__NUXT_DATA__` 仅 28 字节 → 列表 XHR 被 WAF 拦截，卡片从未渲染 |
| 误判排查 | 「满命哥伦比娅」等字样位于 `<header>` 的热搜占位（`<span class="truncate text-14px …">`），**不是商品卡片** |

**用途**：① 回归测试风控识别（文本层 + DOM 层必须命中 captcha）；② 回归测试解析器在 WAF 页上
`cards_seen == 0`（不得凭空产出卡片）；③ 记录真实页面的 Tailwind 类名风格（`text-14px`/`p-24px`），
供后续拿到真实列表页后校准选择器时对照。

**未包含**：任何账号/Cookie/登录态（本次为游客态，无凭据）、任何商品数据（未渲染）。

**注意**：本样本**不能**用于校准卡片选择器——列表页从未渲染。卡片选择器仍以
`tests/test_parser.py` 的手工构造 HTML 为准，待真实列表页样本（需有效登录态/通过验证）补齐。
