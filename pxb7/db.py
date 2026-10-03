"""DuckDB 数据层：DDL、预置数据、轮次幂等写入。

口径来源：docs/01 §4 数据模型（星型模型）/ §4 幂等（snapshot_at 截断到轮次、同轮先删后插）/
§2 实测 gameId / §3.3 风控；词表画像口径来自 docs/02 §2/§3/§4/§5/§6。

设计要点：
- **幂等建库**：`init_db()` 可重复执行（CREATE TABLE IF NOT EXISTS + 预置行 INSERT OR IGNORE）。
- **轮次幂等写入**：`write_snapshot_round()` 把 snapshot_at 截断到轮次桶，同轮重跑先删后插；
  快照与关键词命中同轮共删共插。
- **不伪造**：缺失字段写 NULL（如游客态 viewers_masked），不用 0 冒充。
- **SQL 一律为内联字面量 + 参数绑定**（不接受外部拼接 SQL）。
- 本模块不发起网络请求，也不含任何凭据。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import duckdb

# 轮次截断粒度（分钟）：一个"轮次"= 该时长的对齐时间桶；可被 settings.snapshot.round_minutes 覆盖
ROUND_MINUTES_DEFAULT = 30

SCHEMA_VERSION = "v1.0"

# dim_task 列（落库列序，与 pxb7.config.Task.as_row 对齐）
DIM_TASK_COLUMNS: tuple[str, ...] = (
    "task_id", "game_id", "biz_prod", "task_name", "filter_json", "keyword_filter",
    "sort", "pages_per_run", "runs_per_day", "enabled",
)

# fct_listing_snapshot 列序（列名与 docs/01 §4 一致）
SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "snapshot_at", "listing_id", "price_yuan",
    "level", "yellow_cnt", "five_star_chars", "five_star_weapons",
    "primogems", "intertwined_fate", "artifacts", "skins",
    "server", "mail_status", "tap_status", "psn_status", "trade_code_status",
    "has_compensation", "official_verified", "featured_chars", "img_cnt",
    "viewers_masked",
    # 契约 cardFields 有、docs/01 §4 未列的字段（ImplContract.openQuestions 建议增列；
    # 本项目采用「并集」策略：publish_time_text 原文入 raw，mc_gender/favorites_cnt 入快照）
    "publish_time_text", "mc_gender", "favorites_cnt",
    "extracted_features",
    "parser_version", "collected_via",
)

# 增量列（老库用 ALTER TABLE ADD COLUMN IF NOT EXISTS 补齐，保证 init_db 幂等）
SNAPSHOT_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("publish_time_text", "VARCHAR"),
    ("mc_gender", "VARCHAR"),
    ("favorites_cnt", "INTEGER"),
)

assert len(SNAPSHOT_COLUMNS) == 27, "fct_listing_snapshot 列数应为 27（与 DDL/INSERT 占位符一一对应）"

# 业务表清单（不含 duckdb 内部表）
TABLES: tuple[str, ...] = (
    "dim_game", "dim_task", "dim_listing", "dim_keyword",
    "fct_listing_snapshot", "fct_listing_keyword", "fct_price_change",
    "fct_delist_event", "fct_event", "meta_column_comments",
)

# collected_via 允许值（docs/01 §3.1：登录态优先，游客态仅降级通道，口径如实标注）
COLLECTED_VIA_VALUES = ("login", "guest")


# --------------------------------------------------------------------------- #
# 预置游戏（dim_game）
#   game_id：docs/01 §2 2026-10-02 实测（27 款）+ docs/02 §2 分类表另列 4 款实测 ID
#   genre/keyword_profile/feature_profile/season_reset：docs/02 §2/§4/§5
#   platform 留 NULL：docs/02 §2 定义了该字段但未给逐游戏取值，接入时实测填写
# --------------------------------------------------------------------------- #
def _g(game_id: int, game_name: str, genre: str, keyword_profile: str | None,
       feature_profile: str | None, season_reset: bool = False,
       enabled: bool = True, biz_prod: int = 1) -> dict[str, Any]:
    return {
        "game_id": game_id, "game_name": game_name, "biz_prod": biz_prod,
        "genre": genre, "platform": None,
        "keyword_profile": keyword_profile, "feature_profile": feature_profile,
        "season_reset": season_reset, "enabled": enabled,
    }


PRESET_GAMES: tuple[dict[str, Any], ...] = (
    # --- docs/01 §2 实测（二游抽卡系，词表见 docs/02 §4.A） ---
    _g(10026, "原神", "gacha", "genshin_v0", "gacha_5slot_v0"),
    _g(10161, "崩坏：星穹铁道", "gacha", "hsr_v0", "gacha_5slot_v0"),
    _g(10312, "绝区零", "gacha", "zzz_v0", "gacha_5slot_v0"),
    _g(10302, "鸣潮", "gacha", "wuwa_v0", "gacha_5slot_v0"),
    _g(10630, "异环", "gacha", "nte_v0", "gacha_5slot_v0"),      # §4.A5 词表降级（source=template）
    _g(10605, "明日方舟：终末地", "gacha", None, None),            # §4 未覆盖词表，接入时补齐
    # --- 搜打撤（docs/02 §4.B / §4.H）---
    _g(10371, "三角洲行动", "extraction", "delta_v0", "extraction_asset_v0", season_reset=True),
    _g(10110, "暗区突围", "extraction", "arena_v0", "extraction_asset_v0", season_reset=True),
    # --- MOBA（docs/02 §4.C / §4.L）---
    _g(10013, "王者荣耀", "moba", "hok_v0", "moba_skin_v0", season_reset=True),
    _g(10012, "英雄联盟", "moba", "lol_v0", "moba_skin_v0", season_reset=True),
    _g(10036, "英雄联盟手游", "moba", "lol_v0", "moba_skin_v0", season_reset=True),
    # --- 大逃杀射击（docs/02 §4.D / §4.J）---
    _g(10011, "和平精英", "shooter_br", "pubgm_v0", "shooter_br_skin_v0", season_reset=True),
    _g(10021, "绝地求生 PUBG", "shooter_br", "pubg_v0", "shooter_br_skin_v0", season_reset=True),
    _g(10052, "使命召唤手游", "shooter_br", "codm_v0", "shooter_br_skin_v0", season_reset=True),
    # --- 战术射击（docs/02 §4.E）---
    _g(10148, "无畏契约", "tactical_fps", "valorant_v0", "tactical_fps_skin_v0", season_reset=True),
    # --- 皮肤计数射击（docs/02 §4.J）---
    _g(10039, "穿越火线", "shooter_skin", "cf_v0", "shooter_skin_v0", season_reset=True),
    _g(10033, "穿越火线-枪战王者", "shooter_skin", "cfm_v0", "shooter_skin_v0", season_reset=True),
    _g(10050, "APEX", "shooter_skin", "apex_v0", "shooter_skin_v0", season_reset=True),
    _g(10023, "永劫无间", "shooter_skin", "naraka_v0", "shooter_skin_v0", season_reset=True),
    _g(10460, "逆战：未来", "shooter_skin", "nz_v0", "shooter_skin_v0", season_reset=True),
    # --- 格斗卡牌（docs/02 §4.F）---
    _g(10032, "火影忍者", "fighting_card", "naruto_v0", "fighting_card_v0"),
    # --- 题材动作养成（docs/02 §4.K）---
    _g(154643349307491, "超自然行动组", "themed_action", "supernatural_v0", "themed_action_v0"),
    # --- 非对称/休闲派对（docs/02 §4.M / §4.N）---
    _g(10059, "第五人格", "asym_party", "identity_v0", "asym_party_v0", season_reset=True),
    _g(10142, "蛋仔派对", "asym_party", "eggy_v0", "asym_party_v0"),
    _g(10040, "光遇", "asym_party", "sky_v0", "asym_party_v0"),
    # --- 经典端游/手游（docs/02 §4.I / §G）---
    _g(10025, "地下城与勇士", "classic_mmo", "dnf_v0", "classic_mmo_v0"),
    _g(10163, "逆水寒手游", "classic_mmo", None, None),           # §G 待接入，不预填词表
    # --- docs/02 §2 分类表另列 ID（不在 docs/01 §2 实测清单内）---
    _g(154999114350603, "无畏契约手游", "tactical_fps", "valorant_v0", "tactical_fps_skin_v0",
       season_reset=True),
    _g(155726340587583, "遗忘之海", "gacha", None, None, enabled=False),
    _g(212104821071937, "诡秘之主", "gacha", None, None, enabled=False),
    _g(212107283128394, "冒险岛怀旧服", "classic_mmo", None, None, enabled=False),
)

GAME_COLUMNS: tuple[str, ...] = (
    "game_id", "game_name", "biz_prod", "genre", "platform",
    "keyword_profile", "feature_profile", "season_reset", "enabled",
)


# --------------------------------------------------------------------------- #
# 列口径注释（meta_column_comments）
#   (表, 列) -> (口径说明, 出处)
# --------------------------------------------------------------------------- #
COLUMN_COMMENTS: dict[tuple[str, str], tuple[str, str]] = {
    # dim_game
    ("dim_game", "game_id"): ("平台游戏 ID（docs/01 §2 2026-10-02 选游页逐一点击实测）", "docs/01 §2"),
    ("dim_game", "game_name"): ("游戏名称，取 docs/02 §2 分类表口径", "docs/02 §2"),
    ("dim_game", "biz_prod"): ("业务线 1=账号 / 4=道具 / 2=充值（列表页 /buy/{gameId}/{bizProd}）；本项目只监测账号线", "docs/01 §2"),
    ("dim_game", "genre"): ("价值结构大类 gacha/extraction/moba/shooter_br/tactical_fps/shooter_skin/fighting_card/themed_action/asym_party/classic_mmo", "docs/02 §2"),
    ("dim_game", "platform"): ("端形态 mobile/pc/both；docs/02 §2 定义字段但未给逐游戏取值，接入时实测填写，未填=NULL", "docs/02 §2"),
    ("dim_game", "keyword_profile"): ("词表画像 ID（docs/02 §4 各游戏词表 v0）；NULL=词表未建，须先按 docs/02 §7 三步补齐再接入", "docs/02 §4"),
    ("dim_game", "feature_profile"): ("估值特征字典 ID（进 M2 回归的字段集，按 docs/02 §5 五因子组织）", "docs/02 §5"),
    ("dim_game", "season_reset"): ("段位/赛季资源是否赛季重置（决定 F2 特征降权与事件窗）；文档明说者 TRUE，未查证者保守 FALSE 待校准", "docs/02 §5"),
    ("dim_game", "enabled"): ("是否纳入采集：FALSE=不派发任务（词表未建的游戏先不接入）", "本项目约定"),
    # dim_task
    ("dim_task", "task_id"): ("任务主键，与 config/tasks.yaml 的 task_id 一致", "docs/01 §4"),
    ("dim_task", "game_id"): ("关联 dim_game.game_id", "docs/01 §4"),
    ("dim_task", "biz_prod"): ("业务线（冗余自 dim_game，构造列表 URL 用）", "docs/01 §2"),
    ("dim_task", "task_name"): ("任务显示名，如 原神-官服", "本项目约定"),
    ("dim_task", "filter_json"): ("站内筛选器配置 JSON（筛选面板即属性字典：价格/冒险等级/五星角色数/黄数/原石/纠缠区间）；配置变更前后不可直接比较（M6）", "docs/01 §2"),
    ("dim_task", "keyword_filter"): ("词表分域词数组（segment 词切片）；空数组=不做词表分域过滤", "docs/02 §3"),
    ("dim_task", "sort"): ("排序：comprehensive 综合 / newest 最新 / price 价格 / favorite 收藏", "docs/01 §2"),
    ("dim_task", "pages_per_run"): ("每任务每轮页数上限（docs/01 §3.1-4 硬上限 5 页）", "docs/01 §3.1"),
    ("dim_task", "runs_per_day"): ("每任务每日轮次（docs/01 §3.1-4 为 2–4 轮；捡漏类可加密并缩小页数）", "docs/01 §3.1"),
    ("dim_task", "enabled"): ("是否启用该任务", "docs/01 §4"),
    ("dim_task", "updated_at"): ("任务配置写入时间", "本项目约定"),
    # dim_listing
    ("dim_listing", "listing_id"): ("平台挂牌 ID（详情页 /product/{listingId}/1）", "docs/01 §2"),
    ("dim_listing", "game_id"): ("所属游戏", "docs/01 §4"),
    ("dim_listing", "title"): ("挂牌标题原文（词表抽取的输入之一，原样保存以便重解析）", "docs/02 §3"),
    ("dim_listing", "first_seen"): ("首次出现在采集结果的时间", "docs/01 §4"),
    ("dim_listing", "last_seen"): ("最近一次确认仍在售的时间", "docs/01 §4"),
    ("dim_listing", "is_active"): ("是否在售；FALSE 由下架检测置位。注意：下架≠成交（可能撤牌/平台下架）", "docs/01 §4"),
    # dim_keyword
    ("dim_keyword", "keyword_id"): ("关键词主键，建议按 {profile_id}:{keyword_type}:{keyword} 稳定派生", "本项目约定"),
    ("dim_keyword", "profile_id"): ("所属词表画像（对应 dim_game.keyword_profile）", "docs/02 §3"),
    ("dim_keyword", "keyword"): ("词/短语，如 满命 / 荣耀典藏 / 大红", "docs/02 §3"),
    ("dim_keyword", "keyword_type"): ("ceiling 天花板 / floor 地板 / risk 折价 / resource 资源 / segment 分域 / structure 结构（§4 词表实际使用 structure 表回归主特征）", "docs/02 §3/§4"),
    ("dim_keyword", "extract_pattern"): ("从标题/详情抽数值的正则，如 (\\d+)命、精(\\d)、0+1 记法 (\\d)\\+\\d", "docs/02 §3/§4.A0"),
    ("dim_keyword", "feature_map"): ("命中后写入的估值特征名，如 five_star_weapon_refined", "docs/02 §3"),
    ("dim_keyword", "price_anchor"): ("ceiling / floor / none —— 是否作为价格锚点进入高价底价双层判定第二层", "docs/02 §6"),
    ("dim_keyword", "weight_v0"): ("v0 人工先验权重（资料相对重要性）", "docs/02 §7"),
    ("dim_keyword", "weight_v1"): ("v1 标题挖掘校准权重（上线约 2 周后）", "docs/02 §7"),
    ("dim_keyword", "weight_v2"): ("v2 模型系数反哺权重（≥4 周数据后）", "docs/02 §7"),
    ("dim_keyword", "source"): ("来源：site 站内实测 / domain 公开资料查证 / template 待校准模板", "docs/02 §4"),
    ("dim_keyword", "enabled"): ("是否启用", "docs/02 §3"),
    ("dim_keyword", "updated_at"): ("词表变更时间；走 changelog 保证历史抽取可复现", "docs/02 §7"),
    # fct_listing_snapshot
    ("fct_listing_snapshot", "snapshot_at"): ("快照时间，**截断到轮次**（settings.snapshot.round_minutes）；与 listing_id 共同唯一，同轮重跑先删后插", "docs/01 §4"),
    ("fct_listing_snapshot", "listing_id"): ("挂牌 ID（→ dim_listing）", "docs/01 §4"),
    ("fct_listing_snapshot", "price_yuan"): ("**挂牌价**（元）。成交价平台不公开，本表不含成交价——所有结论限定挂牌价口径并进看板横幅", "docs/01 §4"),
    ("fct_listing_snapshot", "level"): ("冒险等级（列表卡片结构化字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "yellow_cnt"): ("黄数（卡片字段；回归主特征之一）", "docs/01 §2"),
    ("fct_listing_snapshot", "five_star_chars"): ("五星角色数（卡片/筛选器同源）", "docs/01 §2"),
    ("fct_listing_snapshot", "five_star_weapons"): ("五星武器数（卡片/筛选器同源）", "docs/01 §2"),
    ("fct_listing_snapshot", "primogems"): ("原石数量（卡片/筛选器）", "docs/01 §2"),
    ("fct_listing_snapshot", "intertwined_fate"): ("纠缠之源数量（卡片/筛选器）", "docs/01 §2"),
    ("fct_listing_snapshot", "artifacts"): ("圣遗物数（卡片字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "skins"): ("时装数（卡片字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "server"): ("区服（官服/B服/小米服/国际服及子区）；跨区服不可混算，是 M1 指数与回归的分域键", "docs/01 §2"),
    ("fct_listing_snapshot", "mail_status"): ("邮箱状态（出售/实名/注销等 8 态）；F5 安全折价系数表的输入", "docs/02 §5"),
    ("fct_listing_snapshot", "tap_status"): ("TAP 绑定状态（卡片字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "psn_status"): ("PSN 绑定状态（卡片字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "trade_code_status"): ("换绑码状态（活邮/提供换绑码为溢价项）", "docs/02 §4.A1"),
    ("fct_listing_snapshot", "has_compensation"): ("是否找回包赔（卡片保障标签）", "docs/01 §2"),
    ("fct_listing_snapshot", "official_verified"): ("是否官方验号（卡片字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "featured_chars"): ("特色角色名，JSON 数组字符串（卡片字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "img_cnt"): ("图片数（卡片字段）", "docs/01 §2"),
    ("fct_listing_snapshot", "viewers_masked"): ("详情页「正在浏览」人数：游客态打码/不可得写 NULL，登录态写完整值；**缺失不得填 0**（v1.3 登录态主要增益之一）", "docs/01 §2/§3.1"),
    ("fct_listing_snapshot", "publish_time_text"): ("卡片时间标签**原文**（如『21分钟内发布』）；契约 cardFields 字段，docs/01 §4 未列列名——本项目按并集增列。只作首见/断档核对，不硬当绝对上架时间", "契约 cardFields；docs/01 §2"),
    ("fct_listing_snapshot", "mc_gender"): ("主角性别（卡片属性行）；契约 cardFields 字段（required=false），docs/01 §4 未列列名——本项目按并集增列；缺失写 NULL", "契约 cardFields；docs/01 §2"),
    ("fct_listing_snapshot", "favorites_cnt"): ("详情页收藏数（M5 需求侧代理）；游客态不可见写 NULL。契约 openQuestions 指出 §4 无此列，本项目增列", "docs/01 §5 M5；契约 openQuestions"),
    ("fct_listing_snapshot", "extracted_features"): ("词表抽取的结构化特征 JSON（命座数/精炼等级/总资产等卡片没有的字段）", "docs/02 §3"),
    ("fct_listing_snapshot", "parser_version"): ("解析器版本；**解析成功率 <80% = 改版信号**（采集继续但只入 raw 层 + 告警），W1 验收要求 ≥85%——两档口径不同勿混用；站点改版可重放 raw", "docs/01 §3.3/§4/§8"),
    ("fct_listing_snapshot", "collected_via"): ("采集姿态 login（主/备登录态）/ guest（游客态降级通道），如实标注不伪造", "docs/01 §3.1"),
    # fct_listing_keyword
    ("fct_listing_keyword", "snapshot_at"): ("轮次时间戳，与同轮快照取值一致（同轮共删共插）", "docs/01 §4"),
    ("fct_listing_keyword", "listing_id"): ("挂牌 ID", "docs/01 §4"),
    ("fct_listing_keyword", "keyword_id"): ("命中的 dim_keyword.keyword_id", "docs/02 §3"),
    ("fct_listing_keyword", "hit_text"): ("命中原文片段（用于 price_band 与估价报告的归因明细）", "docs/02 §6"),
    # fct_price_change
    ("fct_price_change", "price_change_id"): ("主键，按 {listing_id}:{ts} 稳定派生，保证重算幂等", "本项目约定"),
    ("fct_price_change", "listing_id"): ("挂牌 ID", "docs/01 §4"),
    ("fct_price_change", "ts"): ("观测到价格变化的时间（对比相邻轮次快照得出）", "docs/01 §4"),
    ("fct_price_change", "old_price"): ("变化前挂牌价（元）", "docs/01 §4"),
    ("fct_price_change", "new_price"): ("变化后挂牌价（元）；A2 降价提醒阈值：降幅 ≥10% 或 ≥500 元", "docs/01 §6"),
    ("fct_price_change", "pct"): ("变化幅度 (new-old)/old，负值=降价", "docs/01 §4"),
    # fct_delist_event
    ("fct_delist_event", "listing_id"): ("挂牌 ID（在售消失的 listing）", "docs/01 §4"),
    ("fct_delist_event", "first_seen"): ("首次在售时间", "docs/01 §4"),
    ("fct_delist_event", "last_seen"): ("最后一次在售时间", "docs/01 §4"),
    ("fct_delist_event", "days_on_market"): ("在售天数（last_seen-first_seen，M4 去化速度输入）；**下架≠成交**，只是成交的推断口径，写作 delist 不写 sold", "docs/01 §4"),
    ("fct_delist_event", "last_price"): ("下架前最后一次挂牌价（元）", "docs/01 §4"),
    # fct_event
    ("fct_event", "event_id"): ("事件主键", "docs/01 §4"),
    ("fct_event", "event_date"): ("事件发生日期（趋势图竖线/事件研究窗口锚点）", "docs/01 §4"),
    ("fct_event", "event_type"): ("version 版本 / banner 卡池 / season 赛季 / confiscation 追缴 / other", "docs/01 §4"),
    ("fct_event", "game_id"): ("关联游戏；跨游戏通用事件可置 NULL", "docs/01 §4"),
    ("fct_event", "title"): ("事件标题", "docs/01 §4"),
    ("fct_event", "source_url"): ("事件来源链接（可复核，如萌娘百科追缴记录）", "docs/01 §4"),
    # meta_column_comments
    ("meta_column_comments", "table_name"): ("被注释的表名；table_name='policy' 的行存放**跨表口径**（样本门槛/断档/解析率阈值等），不对应物理表", "docs/01 §4"),
    ("meta_column_comments", "column_name"): ("被注释的列名（policy 行的 column_name 为口径名）", "docs/01 §4"),
    ("meta_column_comments", "comment"): ("该列/该口径的业务口径（看板/模型口径披露来源）", "docs/01 §4"),
    ("meta_column_comments", "source_doc"): ("口径出处（docs 章节）", "本项目约定"),
    ("meta_column_comments", "updated_at"): ("注释写入时间", "本项目约定"),
    # policy 行：跨表口径（契约要求必覆盖的三项）
    ("policy", "min_cell_sample"): ("**样本门槛：子域样本 <30 不发布**——M1 挂牌价指数、M7 趋势阶段/关键词段指数、M8 价格带判定均适用（异环等新游提高到 50）；宁缺不编，不用小样本充数", "docs/01 §5 M1/M7；docs/02 §6/§5"),
    ("policy", "snapshot_gap_max_hours"): ("**快照断档 ≤36h**（每日 QC 项）：超过即视为采集断档，同期指数/去化指标不可用并在看板披露", "docs/01 §5 QC"),
    ("policy", "parse_success_ratio"): ("**解析成功率阈值两档**：<80% = 站点改版信号（采集继续但**只入 raw 层** + 解析告警，等修复）；W1 验收要求 ≥85%（成功率口径不同，勿混用）", "docs/01 §3.3/§8"),
    ("policy", "extract_hit_rate"): ("**词表抽取命中率（W1 ≥70%）口径 = 标题/卡片文本命中 ≥1 词表关键词的 listing 数 / cards_parsed**；[卡] 字段通道（原石/纠缠之源直取）写入特征与桥表但不计入本口径，另以 card_field_coverage 披露——防止结构性数值稀释指标", "docs/01 §8；2026-10-03 口径修正"),
    ("policy", "delist_inference_scope"): ("**下架推断仅在任务切片被本轮完整覆盖时进行**：被页数上限截断/翻页未生效/有被拒页/只入 raw 的轮次一律不推断（5 页窗口滑出 ≠ 下架）；filter={} 的全量切片任务 delist 恒为 0 属正确行为，要启用需收窄 filter 至可完整覆盖的切片", "docs/01 §4/§5 M4"),
    ("policy", "listing_price_scope"): ("**挂牌价 ≠ 成交价**：平台不公开成交价，全部指标与分析结论限定挂牌价口径；下架仅作成交**推断**（delist 不写 sold）", "docs/01 §4"),
    ("policy", "collected_via_policy"): ("采集姿态如实标注：login（主/备登录态）与 guest（游客态降级通道）；限速档两者**完全一致**，登录只解锁可见性不加压", "docs/01 §3.1/§3.3"),
}


# --------------------------------------------------------------------------- #
# 连接与建库
# --------------------------------------------------------------------------- #
def connect(db_path: str | Path | None = None, *,
            read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """打开（必要时创建）数据库连接；db_path=None 时取 settings 的默认路径。"""
    if db_path is None:
        from .config import load_settings  # 延迟导入，避免模块级循环依赖
        db_path = load_settings().paths.db
    path = Path(db_path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def _table_names(conn: duckdb.DuckDBPyConnection) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def init_db(db_path: str | Path | None = None, *,
            conn: duckdb.DuckDBPyConnection | None = None,
            games: Sequence[Mapping[str, Any]] | None = None,
            tasks: Sequence[Mapping[str, Any]] | None = None,
            refresh_tasks: bool = False) -> dict[str, Any]:
    """幂等初始化：建表 + 索引 + 列口径注释 + 预置 dim_game / dim_task。

    - 重复执行安全：CREATE TABLE IF NOT EXISTS；预置行 INSERT OR IGNORE（不覆盖运行期改动）。
    - refresh_tasks=True 时对 dim_task 用 INSERT OR REPLACE 强制对齐 config/tasks.yaml。
    - games / tasks 为 None 时分别取内置 PRESET_GAMES / config/tasks.yaml 的 enabled 任务。
    """
    owns_conn = conn is None
    own_path: Path | None = None
    if conn is None:
        if db_path is None:
            from .config import load_settings
            db_path = load_settings().paths.db
        own_path = Path(db_path)
        conn = connect(own_path)

    created: list[str] = []
    try:
        before = set(_table_names(conn))
        # 建表 + 索引：单条多语句 DDL（幂等）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dim_game ("
            "  game_id BIGINT NOT NULL,"
            "  game_name VARCHAR NOT NULL,"
            "  biz_prod INTEGER NOT NULL DEFAULT 1,"
            "  genre VARCHAR,"
            "  platform VARCHAR,"
            "  keyword_profile VARCHAR,"
            "  feature_profile VARCHAR,"
            "  season_reset BOOLEAN NOT NULL DEFAULT FALSE,"
            "  enabled BOOLEAN NOT NULL DEFAULT TRUE,"
            "  PRIMARY KEY (game_id, biz_prod));"
            "CREATE TABLE IF NOT EXISTS dim_task ("
            "  task_id VARCHAR NOT NULL PRIMARY KEY,"
            "  game_id BIGINT NOT NULL,"
            "  biz_prod INTEGER NOT NULL DEFAULT 1,"
            "  task_name VARCHAR,"
            "  filter_json VARCHAR,"
            "  keyword_filter VARCHAR[],"
            "  sort VARCHAR,"
            "  pages_per_run INTEGER,"
            "  runs_per_day INTEGER,"
            "  enabled BOOLEAN NOT NULL DEFAULT TRUE,"
            "  updated_at TIMESTAMP);"
            "CREATE TABLE IF NOT EXISTS dim_listing ("
            "  listing_id VARCHAR NOT NULL PRIMARY KEY,"
            "  game_id BIGINT,"
            "  title VARCHAR,"
            "  first_seen TIMESTAMP,"
            "  last_seen TIMESTAMP,"
            "  is_active BOOLEAN NOT NULL DEFAULT TRUE);"
            "CREATE TABLE IF NOT EXISTS dim_keyword ("
            "  keyword_id VARCHAR NOT NULL PRIMARY KEY,"
            "  profile_id VARCHAR NOT NULL,"
            "  keyword VARCHAR NOT NULL,"
            "  keyword_type VARCHAR NOT NULL,"
            "  extract_pattern VARCHAR,"
            "  feature_map VARCHAR,"
            "  price_anchor VARCHAR,"
            "  weight_v0 DOUBLE,"
            "  weight_v1 DOUBLE,"
            "  weight_v2 DOUBLE,"
            "  source VARCHAR,"
            "  enabled BOOLEAN NOT NULL DEFAULT TRUE,"
            "  updated_at TIMESTAMP,"
            "  UNIQUE (profile_id, keyword));"
            "CREATE TABLE IF NOT EXISTS fct_listing_snapshot ("
            "  snapshot_at TIMESTAMP NOT NULL,"
            "  listing_id VARCHAR NOT NULL,"
            "  price_yuan DECIMAL(18,2),"
            "  level INTEGER,"
            "  yellow_cnt INTEGER,"
            "  five_star_chars INTEGER,"
            "  five_star_weapons INTEGER,"
            "  primogems BIGINT,"
            "  intertwined_fate INTEGER,"
            "  artifacts INTEGER,"
            "  skins INTEGER,"
            "  server VARCHAR,"
            "  mail_status VARCHAR,"
            "  tap_status VARCHAR,"
            "  psn_status VARCHAR,"
            "  trade_code_status VARCHAR,"
            "  has_compensation BOOLEAN,"
            "  official_verified BOOLEAN,"
            "  featured_chars VARCHAR,"
            "  img_cnt INTEGER,"
            "  viewers_masked INTEGER,"
            "  publish_time_text VARCHAR,"
            "  mc_gender VARCHAR,"
            "  favorites_cnt INTEGER,"
            "  extracted_features VARCHAR,"
            "  parser_version VARCHAR,"
            "  collected_via VARCHAR,"
            "  PRIMARY KEY (snapshot_at, listing_id));"
            "CREATE TABLE IF NOT EXISTS fct_listing_keyword ("
            "  snapshot_at TIMESTAMP NOT NULL,"
            "  listing_id VARCHAR NOT NULL,"
            "  keyword_id VARCHAR NOT NULL,"
            "  hit_text VARCHAR,"
            "  PRIMARY KEY (snapshot_at, listing_id, keyword_id));"
            "CREATE TABLE IF NOT EXISTS fct_price_change ("
            "  price_change_id VARCHAR NOT NULL PRIMARY KEY,"
            "  listing_id VARCHAR NOT NULL,"
            "  ts TIMESTAMP NOT NULL,"
            "  old_price DECIMAL(18,2),"
            "  new_price DECIMAL(18,2),"
            "  pct DOUBLE,"
            "  UNIQUE (listing_id, ts));"
            "CREATE TABLE IF NOT EXISTS fct_delist_event ("
            "  listing_id VARCHAR NOT NULL PRIMARY KEY,"
            "  first_seen TIMESTAMP,"
            "  last_seen TIMESTAMP,"
            "  days_on_market DOUBLE,"
            "  last_price DECIMAL(18,2));"
            "CREATE TABLE IF NOT EXISTS fct_event ("
            "  event_id VARCHAR NOT NULL PRIMARY KEY,"
            "  event_date DATE NOT NULL,"
            "  event_type VARCHAR NOT NULL,"
            "  game_id BIGINT,"
            "  title VARCHAR,"
            "  source_url VARCHAR);"
            "CREATE TABLE IF NOT EXISTS meta_column_comments ("
            "  table_name VARCHAR NOT NULL,"
            "  column_name VARCHAR NOT NULL,"
            "  comment VARCHAR,"
            "  source_doc VARCHAR,"
            "  updated_at TIMESTAMP,"
            "  PRIMARY KEY (table_name, column_name));"
            "CREATE INDEX IF NOT EXISTS idx_snapshot_listing ON fct_listing_snapshot (listing_id);"
            "CREATE INDEX IF NOT EXISTS idx_snapshot_round ON fct_listing_snapshot (snapshot_at);"
            "CREATE INDEX IF NOT EXISTS idx_keyword_hit_listing ON fct_listing_keyword (listing_id);"
            "CREATE INDEX IF NOT EXISTS idx_listing_game ON dim_listing (game_id);"
            "CREATE INDEX IF NOT EXISTS idx_price_change_listing ON fct_price_change (listing_id);"
            "CREATE INDEX IF NOT EXISTS idx_event_game_date ON fct_event (game_id, event_date);"
        )
        # 老库补列（幂等）：契约 cardFields 有、docs/01 §4 未列的字段
        conn.execute("ALTER TABLE fct_listing_snapshot ADD COLUMN IF NOT EXISTS publish_time_text VARCHAR")
        conn.execute("ALTER TABLE fct_listing_snapshot ADD COLUMN IF NOT EXISTS mc_gender VARCHAR")
        conn.execute("ALTER TABLE fct_listing_snapshot ADD COLUMN IF NOT EXISTS favorites_cnt INTEGER")
        created = sorted(set(_table_names(conn)) - before)

        now = _dt.datetime.now().replace(microsecond=0)

        game_rows = list(PRESET_GAMES if games is None else games)
        if game_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO dim_game (game_id, game_name, biz_prod, genre, platform,"
                " keyword_profile, feature_profile, season_reset, enabled) VALUES (?,?,?,?,?,?,?,?,?)",
                [tuple(_norm(row.get(c)) for c in GAME_COLUMNS) for row in game_rows],
            )

        if tasks is None:
            from .config import load_tasks
            tasks = [t.as_row() for t in load_tasks().tasks if t.enabled]
        task_rows = list(tasks)
        if task_rows:
            task_values = [tuple(_norm(r.get(c)) for c in DIM_TASK_COLUMNS) + (now,) for r in task_rows]
            if refresh_tasks:
                conn.executemany(
                    "INSERT OR REPLACE INTO dim_task (task_id, game_id, biz_prod, task_name,"
                    " filter_json, keyword_filter, sort, pages_per_run, runs_per_day, enabled,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    task_values,
                )
            else:
                conn.executemany(
                    "INSERT OR IGNORE INTO dim_task (task_id, game_id, biz_prod, task_name,"
                    " filter_json, keyword_filter, sort, pages_per_run, runs_per_day, enabled,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    task_values,
                )

        conn.executemany(
            "DELETE FROM meta_column_comments WHERE table_name = ? AND column_name = ?",
            [(t, c) for (t, c) in COLUMN_COMMENTS],
        )
        conn.executemany(
            "INSERT INTO meta_column_comments (table_name, column_name, comment, source_doc,"
            " updated_at) VALUES (?,?,?,?,?)",
            [(t, c, comment, doc, now) for (t, c), (comment, doc) in COLUMN_COMMENTS.items()],
        )

        counts = dict(conn.execute(
            "SELECT 'dim_game', count(*) FROM dim_game"
            " UNION ALL SELECT 'dim_task', count(*) FROM dim_task"
            " UNION ALL SELECT 'dim_listing', count(*) FROM dim_listing"
            " UNION ALL SELECT 'dim_keyword', count(*) FROM dim_keyword"
            " UNION ALL SELECT 'fct_listing_snapshot', count(*) FROM fct_listing_snapshot"
            " UNION ALL SELECT 'fct_listing_keyword', count(*) FROM fct_listing_keyword"
            " UNION ALL SELECT 'fct_price_change', count(*) FROM fct_price_change"
            " UNION ALL SELECT 'fct_delist_event', count(*) FROM fct_delist_event"
            " UNION ALL SELECT 'fct_event', count(*) FROM fct_event"
            " UNION ALL SELECT 'meta_column_comments', count(*) FROM meta_column_comments"
        ).fetchall())
    finally:
        if owns_conn:
            conn.close()

    return {
        "db_path": str(own_path) if own_path else "(existing connection)",
        "schema_version": SCHEMA_VERSION,
        "created_tables": created,
        "table_count": len(TABLES),
        "row_counts": counts,
    }


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def truncate_to_round(ts: _dt.datetime, minutes: int = ROUND_MINUTES_DEFAULT) -> _dt.datetime:
    """把时间戳截断到轮次桶（默认 30 分钟，按整点对齐）。"""
    if minutes < 1 or 60 % minutes:
        raise ValueError(f"轮次粒度必须能整除 60 且 ≥1，实际 {minutes}")
    if isinstance(ts, _dt.date) and not isinstance(ts, _dt.datetime):
        ts = _dt.datetime(ts.year, ts.month, ts.day)
    ts = ts.replace(second=0, microsecond=0)
    return ts.replace(minute=(ts.minute // minutes) * minutes)


def round_minutes_from_settings() -> int:
    """读取 settings.snapshot.round_minutes（配置缺失时回退默认值，不阻塞数据层）。"""
    try:
        from .config import load_settings
        return int(load_settings().snapshot_round_minutes)
    except Exception:
        return ROUND_MINUTES_DEFAULT


def _norm(value: Any) -> Any:
    """dict/list → JSON 文本（DuckDB 无嵌套类型，统一存 JSON 字符串）。"""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _rows_to_tuples(rows: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> list[tuple]:
    return [tuple(_norm(row.get(c)) for c in columns) for row in rows]


# --------------------------------------------------------------------------- #
# 轮次幂等写入
# --------------------------------------------------------------------------- #
def write_snapshot_round(conn: duckdb.DuckDBPyConnection,
                         snapshot_at: _dt.datetime,
                         snapshot_rows: Sequence[Mapping[str, Any]],
                         keyword_rows: Sequence[Mapping[str, Any]] | None = None,
                         *,
                         round_minutes: int = ROUND_MINUTES_DEFAULT,
                         scope: str = "round",
                         commit: bool = True) -> dict[str, Any]:
    """轮次幂等写入（docs/01 §4：同一轮次重跑先删后插）。

    :param snapshot_at: 本轮采集时间；内部截断到轮次桶。
    :param snapshot_rows: fct_listing_snapshot 行（缺失列补 NULL；listing_id 必填）。
    :param keyword_rows: fct_listing_keyword 行（与快照同轮共删共插）。
    :param scope: "round"=删除整轮后插入（文档语义，默认）；
                  "batch"=只删本批 listing_id（多任务共用同一轮次桶时避免互相覆盖）。
    :param commit: False 时由调用方管理事务。
    :return: {"snapshot_at", "snapshot_rows", "keyword_rows", "scope"}
    """
    round_ts = truncate_to_round(snapshot_at, round_minutes)

    payload: list[dict[str, Any]] = []
    for row in snapshot_rows:
        listing_id = row.get("listing_id")
        if listing_id is None or str(listing_id).strip() == "":
            raise ValueError("快照行缺少 listing_id")
        item = {c: None for c in SNAPSHOT_COLUMNS}
        item.update({k: v for k, v in row.items() if k in SNAPSHOT_COLUMNS})
        item["snapshot_at"] = round_ts
        payload.append(item)

    bad_via = sorted({str(r["collected_via"]) for r in payload
                      if r.get("collected_via") not in (None, *COLLECTED_VIA_VALUES)})
    if bad_via:
        raise ValueError(f"collected_via 只允许 {COLLECTED_VIA_VALUES}，实际出现 {bad_via}")

    # 同轮同 (listing_id, keyword_id) 去重，保留首个非空 hit_text
    hits: dict[tuple[str, str], dict[str, Any]] = {}
    for row in (keyword_rows or ()):
        listing_id, keyword_id = str(row.get("listing_id") or ""), str(row.get("keyword_id") or "")
        if not listing_id or not keyword_id:
            raise ValueError("关键词命中行缺少 listing_id / keyword_id")
        key = (listing_id, keyword_id)
        if key not in hits:
            hits[key] = {"snapshot_at": round_ts, "listing_id": listing_id,
                         "keyword_id": keyword_id, "hit_text": _norm(row.get("hit_text"))}
        elif not hits[key]["hit_text"] and row.get("hit_text"):
            hits[key]["hit_text"] = _norm(row["hit_text"])

    if commit:
        conn.execute("BEGIN TRANSACTION")
    try:
        if scope == "round":
            conn.execute("DELETE FROM fct_listing_keyword WHERE snapshot_at = ?", [round_ts])
            conn.execute("DELETE FROM fct_listing_snapshot WHERE snapshot_at = ?", [round_ts])
        elif scope == "batch":
            ids = sorted({str(r["listing_id"]) for r in payload})
            if ids:
                conn.execute(
                    "DELETE FROM fct_listing_keyword WHERE snapshot_at = ?"
                    " AND list_contains(?, listing_id)", [round_ts, ids])
                conn.execute(
                    "DELETE FROM fct_listing_snapshot WHERE snapshot_at = ?"
                    " AND list_contains(?, listing_id)", [round_ts, ids])
        else:
            raise ValueError(f"scope 只能是 round/batch，实际 {scope!r}")

        if payload:
            conn.executemany(
                "INSERT INTO fct_listing_snapshot (snapshot_at, listing_id, price_yuan, level,"
                " yellow_cnt, five_star_chars, five_star_weapons, primogems, intertwined_fate,"
                " artifacts, skins, server, mail_status, tap_status, psn_status, trade_code_status,"
                " has_compensation, official_verified, featured_chars, img_cnt, viewers_masked,"
                " publish_time_text, mc_gender, favorites_cnt, extracted_features, parser_version,"
                " collected_via)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _rows_to_tuples(payload, SNAPSHOT_COLUMNS),
            )
        if hits:
            conn.executemany(
                "INSERT INTO fct_listing_keyword (snapshot_at, listing_id, keyword_id, hit_text)"
                " VALUES (?,?,?,?)",
                _rows_to_tuples(hits.values(), ("snapshot_at", "listing_id", "keyword_id", "hit_text")),
            )
        if commit:
            conn.execute("COMMIT")
    except Exception:
        if commit:
            conn.execute("ROLLBACK")
        raise

    return {"snapshot_at": round_ts, "snapshot_rows": len(payload),
            "keyword_rows": len(hits), "scope": scope}


def upsert_dim_listings(conn: duckdb.DuckDBPyConnection,
                        rows: Sequence[Mapping[str, Any]]) -> int:
    """写入/更新 dim_listing：新 listing 记 first_seen，已存在则推进 last_seen 并置 is_active=TRUE。"""
    payload = []
    for row in rows:
        listing_id = row.get("listing_id")
        if not listing_id:
            raise ValueError("dim_listing 行缺少 listing_id")
        seen = row.get("seen_at") or _dt.datetime.now().replace(microsecond=0)
        payload.append((str(listing_id), _norm(row.get("game_id")), _norm(row.get("title")),
                        seen, seen))
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO dim_listing (listing_id, game_id, title, first_seen, last_seen, is_active)"
        " VALUES (?,?,?,?,?,TRUE) ON CONFLICT (listing_id) DO UPDATE SET"
        " last_seen = GREATEST(excluded.last_seen, dim_listing.last_seen),"
        " title = COALESCE(excluded.title, dim_listing.title),"
        " game_id = COALESCE(excluded.game_id, dim_listing.game_id),"
        " is_active = TRUE",
        payload,
    )
    return len(payload)


def write_price_changes(conn: duckdb.DuckDBPyConnection,
                        rows: Sequence[Mapping[str, Any]]) -> int:
    """价格变化事实写入（幂等：price_change_id 由 listing_id+ts 派生，重复写覆盖同键行）。"""
    payload: list[dict[str, Any]] = []
    for row in rows:
        listing_id, ts = row.get("listing_id"), row.get("ts")
        if not listing_id or ts is None:
            raise ValueError("价格变化行缺少 listing_id / ts")
        item = {c: row.get(c) for c in
                ("price_change_id", "listing_id", "ts", "old_price", "new_price", "pct")}
        item["listing_id"] = str(listing_id)
        ts = ts if isinstance(ts, _dt.datetime) else _dt.datetime.fromisoformat(str(ts))
        item["ts"] = ts
        item["price_change_id"] = str(row.get("price_change_id")
                                      or f"{listing_id}:{ts.strftime('%Y%m%dT%H%M%S')}")
        if item.get("pct") is None and row.get("old_price") and row.get("new_price") is not None:
            old = float(row["old_price"])
            if old:
                item["pct"] = (float(row["new_price"]) - old) / old
        payload.append(item)
    if not payload:
        return 0
    conn.executemany(
        # 该表有 PK + UNIQUE(listing_id, ts) 两个唯一约束，DuckDB 要求显式指定冲突目标
        "INSERT INTO fct_price_change (price_change_id, listing_id, ts, old_price, new_price, pct)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT (price_change_id) DO UPDATE SET"
        " listing_id = excluded.listing_id, ts = excluded.ts, old_price = excluded.old_price,"
        " new_price = excluded.new_price, pct = excluded.pct",
        _rows_to_tuples(payload, ("price_change_id", "listing_id", "ts", "old_price",
                                  "new_price", "pct")),
    )
    return len(payload)


def write_delist_events(conn: duckdb.DuckDBPyConnection,
                        rows: Sequence[Mapping[str, Any]],
                        *,
                        mark_inactive: bool = True) -> int:
    """下架事件写入（幂等：listing_id 为主键，重算覆盖）；可选同步 dim_listing.is_active=FALSE。"""
    payload: list[dict[str, Any]] = []
    for row in rows:
        listing_id = row.get("listing_id")
        if not listing_id:
            raise ValueError("下架事件行缺少 listing_id")
        item = {c: row.get(c) for c in
                ("listing_id", "first_seen", "last_seen", "days_on_market", "last_price")}
        item["listing_id"] = str(listing_id)
        first_seen, last_seen = item.get("first_seen"), item.get("last_seen")
        if item.get("days_on_market") is None and isinstance(first_seen, _dt.datetime) \
                and isinstance(last_seen, _dt.datetime):
            item["days_on_market"] = (last_seen - first_seen).total_seconds() / 86400.0
        payload.append(item)
    if not payload:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO fct_delist_event (listing_id, first_seen, last_seen,"
        " days_on_market, last_price) VALUES (?,?,?,?,?)",
        _rows_to_tuples(payload, ("listing_id", "first_seen", "last_seen",
                                  "days_on_market", "last_price")),
    )
    if mark_inactive:
        ids = sorted({str(r["listing_id"]) for r in payload})
        conn.execute("UPDATE dim_listing SET is_active = FALSE"
                     " WHERE list_contains(?, listing_id)", [ids])
    return len(payload)


def write_events(conn: duckdb.DuckDBPyConnection,
                 rows: Sequence[Mapping[str, Any]]) -> int:
    """事件库写入（版本/卡池/赛季/追缴；幂等：event_id 主键）。"""
    payload: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("event_id") or row.get("event_date") is None:
            raise ValueError("事件行缺少 event_id / event_date")
        if not row.get("event_type"):
            raise ValueError("事件行缺少 event_type")
        payload.append({c: row.get(c) for c in
                        ("event_id", "event_date", "event_type", "game_id", "title", "source_url")})
    if not payload:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO fct_event (event_id, event_date, event_type, game_id, title,"
        " source_url) VALUES (?,?,?,?,?,?)",
        _rows_to_tuples(payload, ("event_id", "event_date", "event_type", "game_id",
                                  "title", "source_url")),
    )
    return len(payload)


# --------------------------------------------------------------------------- #
# 词表（dim_keyword）
# --------------------------------------------------------------------------- #
KEYWORD_COLUMNS: tuple[str, ...] = (
    "keyword_id", "profile_id", "keyword", "keyword_type", "extract_pattern",
    "feature_map", "price_anchor", "weight_v0", "weight_v1", "weight_v2",
    "source", "enabled", "updated_at",
)


def upsert_dim_keywords(conn: duckdb.DuckDBPyConnection,
                        rows: Sequence[Mapping[str, Any]]) -> int:
    """词表种子写入 dim_keyword（幂等：按 keyword_id 覆盖；该表有两个唯一约束，须指定冲突目标）。"""
    payload: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("keyword_id") or not row.get("profile_id") or not row.get("keyword"):
            raise ValueError("词表行缺少 keyword_id / profile_id / keyword")
        if not row.get("keyword_type"):
            raise ValueError(f"词表行缺少 keyword_type：{row.get('keyword_id')}")
        item = {c: row.get(c) for c in KEYWORD_COLUMNS}
        item["enabled"] = bool(row.get("enabled", True))
        item["updated_at"] = row.get("updated_at") or _dt.datetime.now().replace(microsecond=0)
        payload.append(item)
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO dim_keyword (keyword_id, profile_id, keyword, keyword_type, extract_pattern,"
        " feature_map, price_anchor, weight_v0, weight_v1, weight_v2, source, enabled, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (keyword_id) DO UPDATE SET"
        " profile_id = excluded.profile_id, keyword = excluded.keyword,"
        " keyword_type = excluded.keyword_type, extract_pattern = excluded.extract_pattern,"
        " feature_map = excluded.feature_map, price_anchor = excluded.price_anchor,"
        " weight_v0 = excluded.weight_v0, weight_v1 = excluded.weight_v1,"
        " weight_v2 = excluded.weight_v2, source = excluded.source,"
        " enabled = excluded.enabled, updated_at = excluded.updated_at",
        _rows_to_tuples(payload, KEYWORD_COLUMNS),
    )
    return len(payload)


def fetch_keywords(conn: duckdb.DuckDBPyConnection, *, profile_id: str | None = None,
                   enabled_only: bool = True) -> list[dict[str, Any]]:
    """读回词表行（抽取器输入）；profile_id=None 表示全部画像。"""
    if profile_id:
        rows = conn.execute(
            "SELECT keyword_id, profile_id, keyword, keyword_type, extract_pattern, feature_map,"
            " price_anchor, weight_v0, weight_v1, weight_v2, source, enabled, updated_at"
            " FROM dim_keyword WHERE profile_id = ? AND (enabled OR NOT ?) ORDER BY keyword_id",
            [profile_id, enabled_only],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT keyword_id, profile_id, keyword, keyword_type, extract_pattern, feature_map,"
            " price_anchor, weight_v0, weight_v1, weight_v2, source, enabled, updated_at"
            " FROM dim_keyword WHERE (enabled OR NOT ?) ORDER BY keyword_id",
            [enabled_only],
        ).fetchall()
    return [dict(zip(KEYWORD_COLUMNS, row)) for row in rows]


# --------------------------------------------------------------------------- #
# 增量合并所需查询（全部参数绑定，无动态 SQL）
# --------------------------------------------------------------------------- #
def previous_round(conn: duckdb.DuckDBPyConnection,
                   before_ts: _dt.datetime) -> _dt.datetime | None:
    """上一轮快照时间（严格早于本轮轮次）。"""
    row = conn.execute(
        "SELECT max(snapshot_at) FROM fct_listing_snapshot WHERE snapshot_at < ?", [before_ts]
    ).fetchone()
    return row[0] if row and row[0] else None


def fetch_snapshot_prices(conn: duckdb.DuckDBPyConnection, snapshot_at: _dt.datetime,
                          listing_ids: Sequence[str]) -> dict[str, float]:
    """取指定轮次、指定 listing 的价格（用于价格变化 diff）。"""
    ids = [str(i) for i in listing_ids if i]
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT listing_id, price_yuan FROM fct_listing_snapshot"
        " WHERE snapshot_at = ? AND list_contains(?, listing_id)",
        [snapshot_at, ids],
    ).fetchall()
    return {str(r[0]): float(r[1]) for r in rows if r[1] is not None}


def fetch_listing_flags(conn: duckdb.DuckDBPyConnection,
                        listing_ids: Sequence[str]) -> dict[str, bool]:
    """已登记的 listing → is_active（用于新 listing 计数与挂单状态）。"""
    ids = [str(i) for i in listing_ids if i]
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT listing_id, is_active FROM dim_listing WHERE list_contains(?, listing_id)",
        [ids],
    ).fetchall()
    return {str(r[0]): bool(r[1]) for r in rows}


def fetch_delist_candidates(conn: duckdb.DuckDBPyConnection, *, game_id: int,
                            before_ts: _dt.datetime, limit: int = 5000) -> list[dict[str, Any]]:
    """下架推断候选：同游戏、仍在售、且 last_seen 早于窗口下沿的 listing。

    口径（docs/01 §4）：在售消失只作成交的**推断**，写作 delist 不写 sold；
    调用方须保证本轮是完整覆盖（风控终止/只入 raw 时不得推断下架）。
    """
    rows = conn.execute(
        "SELECT listing_id, first_seen, last_seen,"
        " (SELECT price_yuan FROM fct_listing_snapshot s WHERE s.listing_id = l.listing_id"
        "  ORDER BY snapshot_at DESC LIMIT 1) AS last_price"
        " FROM dim_listing l"
        " WHERE is_active AND game_id = ? AND last_seen IS NOT NULL AND last_seen < ?"
        " ORDER BY last_seen LIMIT ?",
        [game_id, before_ts, int(limit)],
    ).fetchall()
    return [{"listing_id": str(r[0]), "first_seen": r[1], "last_seen": r[2],
             "last_price": float(r[3]) if r[3] is not None else None} for r in rows]


def update_detail_fields(conn: duckdb.DuckDBPyConnection, listing_id: str, *,
                         viewers_masked: int | None,
                         favorites_cnt: int | None) -> int:
    """把详情页 M5 字段（正在浏览/收藏）回填该 listing **最近一轮**快照。

    前端 B 采集插件通道：详情页通常与列表卡片不在同一批到达，这里按 listing 定位
    最新快照行回填。返回更新的行数（0 = 尚无快照行，调用方应暂存待合并）。
    """
    rows = conn.execute(
        "UPDATE fct_listing_snapshot SET viewers_masked = ?, favorites_cnt = ?"
        " WHERE listing_id = ? AND snapshot_at ="
        " (SELECT max(snapshot_at) FROM fct_listing_snapshot WHERE listing_id = ?)"
        " RETURNING listing_id",
        [viewers_masked, favorites_cnt, str(listing_id), str(listing_id)]).fetchall()
    return len(rows)


def table_counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """各表行数（status 子命令用）。"""
    return dict(conn.execute(
        "SELECT 'dim_game', count(*) FROM dim_game"
        " UNION ALL SELECT 'dim_task', count(*) FROM dim_task"
        " UNION ALL SELECT 'dim_listing', count(*) FROM dim_listing"
        " UNION ALL SELECT 'dim_keyword', count(*) FROM dim_keyword"
        " UNION ALL SELECT 'fct_listing_snapshot', count(*) FROM fct_listing_snapshot"
        " UNION ALL SELECT 'fct_listing_keyword', count(*) FROM fct_listing_keyword"
        " UNION ALL SELECT 'fct_price_change', count(*) FROM fct_price_change"
        " UNION ALL SELECT 'fct_delist_event', count(*) FROM fct_delist_event"
        " UNION ALL SELECT 'fct_event', count(*) FROM fct_event"
        " UNION ALL SELECT 'meta_column_comments', count(*) FROM meta_column_comments"
    ).fetchall())


def latest_round(conn: duckdb.DuckDBPyConnection) -> _dt.datetime | None:
    row = conn.execute("SELECT max(snapshot_at) FROM fct_listing_snapshot").fetchone()
    return row[0] if row and row[0] else None
