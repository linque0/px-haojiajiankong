# 11 操作提示词 —— 按新数据重填游戏数据 CSV

> 定位：把「采集了新数据 → 重填某游戏的数据表格 CSV」固化成一条可复用的提示词（2026-10-04 首版，以鸣潮 10302 实跑校准）。
> 用法：新会话（AI 助手）直接粘贴 §1 提示词全文，替换 `<>` 占位符后发送；提示词的每条要求与仓库规则的对应关系见 §3。

## 1. 提示词（复制使用）

```text
任务：用本机数据库 data/pxb7.duckdb 中的最新采集数据，重填游戏「<游戏名>」（game_id=<gameId>）
的数据表格 CSV，并按仓库要求验收、落档。全程只读本机库，不访问站点，不干扰正在运行的采集。

一、前置检查
1) 若 data/state/crawl.lock 存在（采集进程持锁）：轮询等待其释放（每 20 秒查一次，最长 5 分钟），
   到期仍在则如实报告并停止；绝不删除或抢占锁文件。
2) 先查库确认确有新数据：
   SELECT CAST(snapshot_at AS DATE) d, count(*) FROM v_listing_analysis
   WHERE game_name = '<游戏名>' GROUP BY 1 ORDER BY 1;
   只有出现新的快照日期才继续；没有新数据就说明"无新数据可填"，不导出重复表。

二、重填（必须走既有管线，不手工造数、不从网页抓数）
3) 重建分析视图并导出主表：
   .venv/Scripts/python.exe run.py export-csv --game <gameId>
   产出 data/analysis/pxb7-listings-<当日日期>.csv（一行 = 一个 listing 的最新一轮，
   来源视图 v_listing_analysis，UTF-8 带 BOM，列 = 该视图全部列）。
4) 按游戏拆分：
   .venv/Scripts/python.exe run.py split-csv --in data/analysis/pxb7-listings-<当日日期>.csv
   得到 data/analysis/by_game/pxb7-listings-<日期>-<游戏名>-<gameId>.csv。
5) 旧日期的分表文件保留，不覆盖、不删除；data/analysis/ 与 *.duckdb 均不入 git。

三、验收（逐项报数，全过才算完成）
6) 行数 = v_listing_analysis 中该游戏的行数；listing_id 无重复。
7) 列数与列序 = 与上一版同名分表逐列一致（v_listing_analysis 列集未变时应为 59 列）。
8) 文件头 BOM 存在（EF BB BF）。
9) 按最新快照日期分组行数与库内查询一致；新日期行的关键字段
   （level / yellow_cnt / five_star_chars / feat_constellation_cnt / extracted_features）
   覆盖率如实报告，缺失保持空值，不填 0、不伪装成功。
10) 抽 2–3 行新数据与库内同一 listing 的行核对字段一致。

四、落档（docs/10 §1）
11) docs/05-交付说明与验收结果.md 追加一节，按「改动 / 验收 / 诚实边界」三段写清：
    哪些日期的数据入了表、哪些没入、不能声称什么（如：单轮快照不能做轨迹分析）。
12) docs/09-项目变更总结.md「追加变更」表加一行并链接对应文档。
13) 不改任何代码；若用户要求提交：先全量 pytest，提交只含文档，绝不包含 data/analysis、
    *.duckdb、登录态或任何红线文件（docs/10 §2.4）。
```

## 2. 使用说明

- 占位符只有两个：`<游戏名>` 与 `<gameId>`，取值见 `config/tasks.yaml`（当前：原神 10026、
  鸣潮 10302、火影忍者 10032、三角洲行动 10371）。
- 需要鸣潮的**「看板列」版式**（14 列：listing_id/游戏/价格 ¥/等级/黄数/五星角色/五星武器/
  共鸣链（N命）/武器精炼（精N）/资源/额外付费商品/区服/商品发布时间/收藏，2026-10-04
  用户指定）时，在重填后追加：
  `run.py export-csv --game <gameId> --layout game` → `by_game/pxb7-listings-<日期>-鸣潮-10302-看板列.csv`；
  该版式按游戏登记（当前仅鸣潮），未登记的游戏会明确报错（docs/05 §看板列导出）。
- 输出文件按**导出当日日期**命名：同一天重跑会覆盖当天的同名文件（重导以最新库态为准），
  不同日期各存一份历史表；这是既有管线行为，提示词不改变它。
- 「重填」的口径 = `v_listing_analysis`（每号最新一轮）：老 listing 若被重新采到会更新为其
  最新快照；未再浏览到的老号保留其最后一次快照，不丢行。
- 需要并行重填多个游戏时，把 §1 的任务段按游戏重复执行即可；`export-csv` 不带 `--game`
  则一次导出全部游戏再拆分，效果等价。

## 3. 要求对照表（提示词条款 → 仓库依据）

| 提示词条款 | 依据 |
|---|---|
| 走 export-csv / split-csv 管线，不手工造数 | README「分析就绪层与 CSV 导出」；docs/01 §4 |
| 一行 = 一个 listing 最新轮、列不裁剪、UTF-8 BOM | `pxb7/analysis.py::export_main_csv` / `split_csv_by_game` |
| 持锁等待、不干扰采集 | docs/10 §4.3（限速与退避档不得突破） |
| 不编造、缺失留空、不填 0 | docs/10 §5.1（不伪造） |
| 保留旧文件、产物不入库 | docs/10 §2.4（data/analysis、*.duckdb 在绝不入库清单） |
| 验收逐项报数 | docs/10 §1.3（诚实边界必填） |
| docs/05 三段落档 + docs/09 台账 | docs/10 §1.2、§1.4 |

## 4. 诚实边界

- 提示词只固化**数据重填**这一件事：不含采集、不含词表/解析修复；库内数据本身的覆盖缺口
  （如某字段整批缺失）会如实带出，提示词不负责补数。
- `export-csv` 会重建分析视图（DDL 写库），因此必须等采集进程释放锁；这与 docs/10 §4.3 一致。
- 首版以鸣潮 10302 实跑校准（204 行 × 59 列，含 10-04 新数据 92 行）；其余游戏仅共用同一管线，
  未逐游戏试跑。
