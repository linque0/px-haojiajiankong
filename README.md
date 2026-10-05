# 螃蟹游戏服务网（pxb7）账号价格监测与分析

监控游戏账号价格变化趋势、支持购买策略决策的**趋势看板 + 浏览器插件**项目。

## 大方向（先读）

**[00-大方向与产品形态](docs/00-大方向与产品形态.md)** —— 一个内核（采集管线 + 游戏词表 + 估值模型 + 事件库），双前端：

- **前端 A 趋势看板**（Streamlit）：市场总览 / 价格指数 / 趋势阶段 / 去化 / 趋势周报
- **前端 B 购买决策插件**（Tampermonkey 用户脚本）：在 pxb7 列表/详情页原位注入 捡漏分、预测价、五因子归因、价格历史
- **通知层**：飞书/webhook 推送（捡漏 / 降价 / 趋势转折）

阶段：一（内核+看板，W1–W3）→ 二（插件 MVP，W4）→ 三（策略回测与铺开，W5+）。

## 文档导航

| 文档 | 内容 |
|---|---|
| [00-大方向与产品形态](docs/00-大方向与产品形态.md) | 最终目的、双前端决策、购买策略三层体系、阶段路线、边界 |
| [01-项目方案与采集规范](docs/01-项目方案与采集规范.md) | 站点实测、采集姿态与风控退避、数据模型、指标 M1–M8、预警 A1–A5、排期 |
| [02-游戏分类与关键词体系](docs/02-游戏分类与关键词体系.md) | 10 品类 28 款游戏的分类、词表、高价/底价判定、五因子模型、校准闭环 |
| [03-各游戏价值估算规律](docs/03-各游戏价值估算规律.md) | 逐游戏价值估算规律推理（六段式），价值体系的建模依据 |
| [05-交付说明与验收结果](docs/05-交付说明与验收结果.md) | 管线与插件的逐项交付、验收、试运行与修复记录（持续追加） |
| [06-入库字段修复说明](docs/06-入库字段修复说明.md) | 扩展 v0.4.2–v0.4.5：列表标题全文补齐演进、字段解析修复、历史数据修复工具 |
| [07-全文采集效率与网络异常排查](docs/07-全文采集效率与网络异常排查.md) | 全文补采失败机制调查、可优化点与四阶段实施方案（未改代码） |
| [09-项目变更总结](docs/09-项目变更总结.md) | 变更总台账：功能更新、双采集模式与进度、游戏表自动同步、对比实验附录（原分散文档已整并入此） |
| [10-工作规则](docs/10-工作规则.md) | 开发与维护纪律：文档/Git/测试纪律、采集合规红线、口径诚实、安全钩子处置 |

## 合规姿态（要点）

**登录态优先**（自有专用小号持久化登录态，主备双号冗余；游客态仅作失效降级通道）+ Playwright 真实浏览器渲染；限速与风控退避写进采集器硬逻辑；**不做协议层逆向/打码/指纹伪造**；插件零新增请求（只读用户自己浏览的页面 + 本地 API）；个人研究用途，不做自动下单/代购。

---

## 采集脚本

### 采集通道：前端 B 浏览器插件（当前唯一可用通道，2026-10-03 已端到端验证）

> **为什么**：试运行实测（docs/05「试运行记录」）——站点 WAF 按**自动化环境特征**拦截 Playwright
> （无头/有头、完整登录 Cookie 均被滑块挑战），而用户自己的真实浏览器畅通。按 docs/01 §3.2
> 红线（不做指纹伪造），采集端改为 **Tampermonkey 用户脚本**：只读取你正在浏览的页面 DOM、
> 发给本机网关入库，**零新增 pxb7 请求**（docs/01 §8 W4 口径）。

#### 浏览器扩展（推荐，v0.5.2，独立 MV3 扩展，不依赖油猴）

> v0.5.2 弹窗一键启停本机网关：网关未运行时弹窗显示「启动网关」按钮，点一下即由浏览器经
> Native Messaging 拉起本机网关（停用仍用「停止服务」）；无需再回桌面双击程序。
> 首次使用需双击「安装网关启停.bat」登记本机宿主（HKCU 注册表，免管理员），
> 若自动推导的扩展 ID 与扩展管理页显示不一致，按脚本提示用 `--ext-id <ID>` 重装登记。

> v0.4.4取消自动悬浮：优先复用列表响应中的完整商品标题，缺失时在当前页面会话中按商品编号串行、限速补采，获取公开角色链数及武器精炼，无需进入详情。补采会产生站点请求，遇验证或限流停止并显示待补齐数量；“零新增请求”仅指被动读取模式。应用扩展更新后刷新列表再采集，详见[字段修复说明](docs/06-入库字段修复说明.md)。

> v0.4.5默认在上一全文请求完成后等待3秒，每8次请求暂停；失败商品在整页结束后单独补采一轮。设置中的“全文请求间隔”可调 0–10 秒（0.1 秒步进，v0.5.1；0 = 不等待），与“去重间隔（分钟）”独立。缺失原因按商品ID保存在raw元数据，当前页未补齐时暂停加载更多。

```text
1) 安装（每个浏览器各一次）：双击「安装浏览器扩展.bat」
   → 控制台列出本机所有 Chromium 系浏览器（Edge/Chrome/夸克…）及其扩展页地址
   → 脚本逐个启动浏览器并把扩展页地址复制到剪贴板：地址栏 Ctrl+V 回车
   → 开启「开发者模式」→「加载已解压的扩展程序」→ 选择 pxb7-extension 目录
   → 工具栏固定「pxb7 采集助手」
1b) 一键启停（可选，每台机器一次）：双击「安装网关启停.bat」
   → 登记 Native Messaging 宿主（extension/native-host/，HKCU 注册表，免管理员）
   → 之后网关未运行时，扩展弹窗顶部出现「启动网关」按钮，点击即启动；停止用「停止服务」
2) 日常：正常浏览 pxb7 即自动采集（自动采集需网关在跑；未运行时弹窗一键启动）
   → 点工具栏图标即弹出实时看板（状态卡/轮次图/最近批次/采集目标/采集张数/设置/数据路径）
3) 更新：v0.3.0 起扩展**自更新**——仓库代码更新后，扩展每 30 分钟比对磁盘 manifest 版本，
   发现新版即自动重载（Chrome/Edge/夸克通用；弹窗里也会出现「应用扩展更新」按钮）。
   仅"从 ≤v0.2.0 升到 v0.3.0"这最后一次需要手动「重新加载」（旧版没有自更新能力）。
```

#### 为什么一次只采到 16 张 / 怎么调大（v0.4.0 新增「每次采集张数」）

**16 是站点一页的渲染量**：2026-10-03 实测原神/鸣潮/三角洲/火影四个游戏的列表页 raw dump
都恰好 16 张卡片，且页面里没有分页控件——插件只读"当前已渲染的 DOM"，所以一次采集就是 16 张。

**调大办法**：在扩展弹窗 / 完整看板 / 油猴面板把「每次采集张数」设为 16–200（默认 16，step 16）。
脚本会在**你当前这个页面里**把卡片加载出来再入库：

- 优先点站点自己渲染的「下一页/加载更多」控件；没有控件就滚动到底触发站点自身的懒加载；
- 每 2 秒一轮，连续两轮没有新卡片即停（说明没有更多了），轮次上限 12（≈192 张）；
- 先把当前页已有的 16 张立刻入库，加载完成后把整页 DOM 再发一次——网关按「轮次+listing」
  幂等合并，不会产生重复行；批次记录里带 `page_no` 与 `sweep`（目标/实际/轮次/停止原因）可复核；
- 全程不猜 URL、不请求站外接口、不绕过站点风控；触发的是站点自己的加载，等同你手动滚动。

需要翻页看更多账号时，仍然推荐：改站内筛选器缩小切片，或按站点分页正常浏览（每翻一页采一页）。

#### 采集目标（按游戏多选，v0.3.0/v0.5.0 新增）

任务在 `config/tasks.yaml` 定义（当前：原神、鸣潮、火影忍者、三角洲行动，gameId 照
docs/02 §2 实测表）。在**扩展弹窗 / 看板 / 油猴面板**的「采集目标」区勾选参与采集的游戏：

- 只采集勾选游戏的页面；未勾选任何目标 = 使用网关启动任务（`serve --task`，缺省第一个 enabled 任务）；
- 列表页由内容脚本按 URL 的 `game_id` 预检直接跳过；即使手动触发或详情页，网关也会按
  实际游戏拒收（`target-not-selected`），**不落任何 raw/DB**；
- 详情页 URL 不含 gameId，网关按「本地库 → 面包屑 `/buy/{game}/{biz}` 链接 → 推荐位
  `gameid=` 属性」识别所属游戏；识别不了如实拒收（`game-unresolved`），不猜；
- 各游戏按 `dim_task` 独立入库、独立词表画像（未建画像的游戏不使用别家词表顶替，docs/02 §G）。

#### 数据存放位置与自定义路径

看板「数据存放位置」区（扩展弹窗同区）展示全部落盘位置：数据库、原始页面 DOM（bronze）、
轮次 summary、日志、登录态/风控状态文件、插件配置；支持「复制路径」与「打开目录」（仅本机）。

自定义（看板「自定义数据路径」表单 / CLI，同一份 `config/paths_override.json`）：

```bat
:: 查看当前全部路径
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py paths
:: 自定义（可只改其中几项；自动建目录；新库缺失时自动初始化）
... run.py paths --set db=D:\pxb7-data\pxb7.duckdb --set raw_root=D:\pxb7-data\raw\pxb7
... run.py paths --set runs=D:\pxb7-data\runs --set log_dir=D:\pxb7-data\logs
:: 恢复默认
... run.py paths --reset
```

可自定义：`db / raw_root / runs / log_dir`（采集产物四件套；环境变量 `PXB7_DB_PATH` /
`PXB7_RAW_ROOT` 优先级最高）。**登录态与风控状态目录不开放界面迁移**（安全相关，避免
登录态/退避状态「凭空消失」）；看板改路径即时对网关生效，CLI 下次运行生效。

#### Tampermonkey 用户脚本（可选替代，二者等价，v0.5.0）

```text
浏览器装 Tampermonkey → 新建脚本 → 粘贴 extension/pxb7-collector.user.js 保存
或在看板「安装/状态」区点安装链接（按 @updateURL 自动检查更新）
```

#### 桌面前端（服务 + 完整看板窗口）

#### 看板面板（油猴版：页面内左下角 📊；扩展版：工具栏图标弹窗）

油猴脚本（v0.6.0）在 pxb7 页面内置可开关的实时看板面板（左下角 📊 / 油猴菜单「打开看板面板」）；
独立扩展（v0.4.0）对应的是**工具栏图标弹窗**（同一套数据与操作）：状态卡（快照/解析率/新增/
词表命中/详情/拦截拒收）、最近 5 轮采集柱图、最近批次、**采集目标多选**、**每次采集张数**、
设置区（自动采集/去重间隔/SPA 等待/调试日志，保存即生效）、**数据存放位置**（复制/打开目录）、
按钮区（采集本页 / 应用扩展更新 / 完整看板 / 停止服务）——与采集同一通道，边逛边看边采。

#### 命令行方式（等价）

```bat
:: 打开看板（服务未运行会自动后台拉起）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py dashboard
:: 停止后台服务
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py stop-gateway
```

#### 看板内容

实时状态卡（快照/解析率/新增 listing/词表命中/详情回填/拦截拒收）、最近批次表、
采集轮次与价格分布图、**按游戏的词表命中 Top10**（原神看原石/满命、鸣潮看星声/共鸣链/精N…，
各游戏词表不混算）、**入库数据浏览**（游戏筛选 + 每页 15/30/50/100 + 首页/上一页/下一页/末页，
列名随所选游戏换成该游戏自己的关键词：原神=原石/纠缠之源，鸣潮=共鸣链（N命）/武器精炼（精N）；
不筛游戏时用通用列，不把某家术语硬套到别家行上）、**插件设置**（自动采集开关 / 同页去重间隔 /
SPA 等待 / 每次采集张数 / 调试日志，脚本 ≤5 分钟自动应用）、脚本与扩展版本、后台服务停止按钮。

合规要点：插件只读你正在看的页面（页内"加载更多"触发的是站点自身的懒加载，等同手动滚动，
不猜 URL、不请求站外接口）；网关只绑回环（桌面组件另有 SSRF 白名单硬校验）；
拦截页拒收不入库；下架推断在插件通道关闭（覆盖不完整）；`collected_via=login`（数据来自你的登录会话）。

### 传统管线（Playwright，当前被 WAF 拦截，保留待通道恢复）

工作区根目录下执行（所有命令都在仓库根跑，路径参数按 CWD 解析、内部路径按项目根解析）：

### 1. 安装

```bat
:: 依赖（PyPI 很慢；如默认源不可用可换镜像）
pxb7-price-monitor\.venv\Scripts\python.exe -m pip install -r pxb7-price-monitor\requirements.txt
:: 或：-i https://mirrors.aliyun.com/pypi/simple/

:: Playwright 浏览器内核（官方 CDN 不可达时用 npmmirror）
pxb7-price-monitor\.venv\Scripts\python.exe -m playwright install chromium
:: 网络受限时：
:: set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
:: pxb7-price-monitor\.venv\Scripts\python.exe -m playwright install chromium
```

### 2. 初始化数据库（幂等）

```bat
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py init-db
```

建表（`data/pxb7.duckdb`）+ 列口径注释 + 预置 dim_game/dim_task + 词表种子（`config/keywords_seed.yaml` → dim_keyword）。

### 3. 采集一轮

```bat
:: 单任务一轮（默认取第一个 enabled 任务 = genshin_official）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py crawl --task genshin_official --pages 2 --detail 1 --guest --summary-json pxb7-price-monitor\data\runs\smoke.json
```

- 退出码：**0** 完成 / **21** 风控终止 / **1** 错误；
- 指标写入 `--summary-json`（键名照契约：run_id / cards_seen / cards_parsed / parse_success_rate / extract_hit_rate / snapshots_inserted …）；
- 原始 DOM 落盘 `data/raw/pxb7/{task}/{YYYYMMDD}/{run_id}/`，抽取诊断在同目录 `extraction_report_*.json`；
- 不加 `--guest` 时按 主登录态 → 备登录态 → 游客态 顺序自动选择；
- 没有登录态文件时会以游客态运行并在 `collected_via=guest` 如实标注。

### 4. 状态与风控

```bat
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py status
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py status --json
```

打印各表行数、词表启用分布、最近轮次，以及 `data/state/risk_state.json` 的 level / reason / backoff_until / consecutive 与限速档。

### 5. 登录助手（人工扫码，单次尝试不重试）

```bat
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py login --slot primary
:: 备用小号：--slot backup
```

打开有头浏览器 → 人工登录 → 回车保存 `data/state/storage_primary.json`（已在 .gitignore，**不得外传**）。

### 6. 离线重放 raw（不发起任何请求）

```bat
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py parse-raw --task genshin_official --date 20261002
:: 或指定目录：--run-dir pxb7-price-monitor\data\raw\pxb7\genshin_official\20261002\<run_id>
```

站点改版或修好解析器后，用历史 raw 重解析入库（同轮幂等先删后插）。

### 7. 词表、QC 与登录态增益

```bat
:: 词表种子 → dim_keyword（幂等；--dry-run 只统计）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py extract --dry-run
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py extract

:: 每日 QC（价格非负 / 快照断档≤36h / 解析率 / 关键字段缺失率 / listing_id 去重）
::   + W1 验收四指标（采集成功率≥95%、解析率≥85%、词表命中≥70%、风控触发=0）
:: 退出码：0=通过 / 22=未通过 / 1=错误
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py qc --out pxb7-price-monitor\data\runs\qc.json

:: 登录态增益实测（单页条数上限 /「正在浏览」完整值 / 收藏可见性）
:: 退出码：0=实测完成 / 24=无可用登录态（如实 skipped，不推断增益）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py session-gain --pages 1 --detail 1
```

`load` 是 `parse-raw` 的等价入口（raw → DuckDB），按契约模块名保留。

### 分析就绪层与 CSV 导出（docs/01 §4「分析层」）

采集数据先整理成"分析就绪"的视图/表，再导出一份可直接分析的 CSV：

```bat
:: ① 一份 CSV（一行 = 一个 listing 的最新一轮：价格 + 结构化字段 + 词表命中 + 质量标记）
::    默认写到 data/analysis/pxb7-listings.csv（固定名持续更新：每次导出以库内最新态
::    整体覆盖同一文件，2026-10-05 起不再按日期另起新文件）；UTF-8 带 BOM，Excel 双击可看中文
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py export-csv
:: 只导某个游戏（gameId 见 config/tasks.yaml / docs/02 §2）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py export-csv --game 10302

:: 按游戏「看板列」版式导出（列名用该游戏词表说法；鸣潮=共鸣链（N命）/武器精炼（精N）/
::   资源（星声/月相/余波珊瑚/浮金波纹/铸潮波纹）/额外付费商品（车架模组/摩托饰品/人物皮肤）…）
::   输出 by_game\pxb7-listings-鸣潮-10302-看板列.csv（同样固定名持续更新）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py export-csv --game 10302 --layout game

:: ② 分析视图全套 + 数据字典（要 CSV 之外的 Parquet/多表时用）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py prepare-analysis --out pxb7-price-monitor\data\analysis

:: ③ 把导出的 CSV 按游戏拆成多个表（每游戏一份，列与源文件一致；默认输出到同级 by_game\，
::    文件名 = 源文件名-游戏标签，源为固定名时输出也是固定名）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py split-csv --in pxb7-price-monitor\data\analysis\pxb7-listings.csv
```

- 视图：`v_listing_latest`（每号最新态）、`v_listing_analysis`（分析主表，CSV 的来源）、
  `v_keyword_hits`（命中长表）、`keyword_feature_matrix`（特征矩阵表）、`v_listing_daily`、
  `v_price_index_weekly`、`v_price_band`、`v_deals`、`v_delist_speed`；
- **样本不足不发布**：价格带 `unpublished`、周聚合 `publishable=false`（门槛
  `settings.quality.min_cell_sample`，默认 30）；`v_deals` 是 v0 规则候选，非 M2 模型结论；
- 列口径：`data/analysis/README.md`（`prepare-analysis` 生成的数据字典）与 `docs/01 §4`；
- pandas 读法：`pd.read_csv(..., encoding="utf-8-sig")`（BOM 是给 Excel 的）。

### 8. 测试
```bat
pxb7-price-monitor\.venv\Scripts\python.exe -m pytest pxb7-price-monitor -q
```

离线运行：解析器夹具（假 HTML + 真实 WAF 页样本）、风控状态机、DB 幂等、价格变化与下架推断、CLI、通知（不访问站点）。

### 9. 解析校准流程（需登录态；不越过风控退避）

2026-10-02 冒烟实测结论：**游客态首轮即被站点 WAF 滑块验证拦截**（见 `tests/fixtures/README.md`）。
**2026-10-03 已用内置浏览器抓到的真实 DOM 完成校准（parser v0.2.0：真实样本 16/16 解析率 100%、
词表命中率 100%）**，夹具见 `tests/fixtures/real_card_pxb7_20261003.html`。

**2026-10-03 试运行结论（重要）**：站点 WAF 按**自动化环境特征**拦截——Playwright 无头/有头、
带完整人工登录 Cookie（32 项，含 httpOnly WAF 令牌）均被滑块挑战；同一时刻用户真实浏览器畅通。
按 docs/01 §3.2 红线（不做指纹伪造/隐蔽化），**Playwright 自动化采集通道在 pxb7 当前不可用**；
监控数据的可行路径是 docs/00 的**前端 B 浏览器插件**（跑在用户真实浏览器、零新增请求）。
登录态文件 `data/state/storage_primary.json` 已通过 `login --auto-save` 完整建立，供通道恢复后使用。
后续校准步骤：

```bat
:: 1) 保存专用小号登录态（人工扫码；单次尝试，失败不重试、不换指纹、不用代理）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py login --slot primary

:: 2) 先看风控状态：backoff_until 未过期时 crawl 会被前置闸门直接拒绝（这是设计行为，勿绕过）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py status

:: 3) 退避期过后，采一轮（≤2 页；命令自身遵守 3–6s/页限速）
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py crawl --task genshin_official --pages 2 --detail 1 --summary-json pxb7-price-monitor\data\runs\smoke.json

:: 4) 之后一律离线：对这次 raw 目录重放，反复调选择器，不再上站
pxb7-price-monitor\.venv\Scripts\python.exe pxb7-price-monitor\run.py parse-raw --task genshin_official --run-dir pxb7-price-monitor\data\raw\pxb7\genshin_official\<YYYYMMDD>\<run_id>
```

- **字段级诊断**：`crawl` 与 `parse-raw` 都会打印「字段命中/缺失/命中率/命中策略/样例值」表
  （`*` = 契约 required 字段），并写出 `extraction_report_*.json`（含逐字段 `samples`、失败卡片样例、
  卡片选择器与渲染等待命中情况）——按这张表改 `pxb7/parser.py` 的 `CARD_SELECTORS` / `FIELD_SPECS` 即可。
- **拦截页安全**：`parse-raw` 对风控/拦截页面重新识别（文本层 + DOM 层，不依赖元数据），
  一律判为 `aborted`、**不写任何表**、退出码 21 —— 防止改版后被拦截的页面被当成正常页入库。
- **纪律**：出现验证码/滑块即停采 24h（`risk_state.json` 记录），到期后才以 1/4 频率试探；不人工过验证码、
  不改用有头模式试探、不重置 `risk_state`、不换指纹/代理。

### 限速与红线（写死在采集器里）

单页 3–6s 随机 / 任务间 ≥60s / 每任务每轮 ≤5 页 / 每日 2–4 轮；登录态**不放松**限速；验证码→停采 24h 后 1/4 频率试探（连续 2 次停并告警）；空响应率 >30%→当轮终止退避 6h；IP 不可达→停采人工介入；主登录态被踢→切备用降频 1/2，主备均失效→游客态 + 72h 观察；解析率 <80%→只入 raw 层并告警。所有请求过 URL 守卫（仅 http/https、拒绝 localhost/环回/私有/保留地址）；不做协议逆向、不做指纹伪造与代理池。

