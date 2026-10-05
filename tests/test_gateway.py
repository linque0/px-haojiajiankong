"""本地采集网关（前端 B）测试：入库处理纯逻辑 + 详情字段回填（全部离线，不监听端口）。"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pxb7 import config as cfg  # noqa: E402
from pxb7 import db  # noqa: E402
from pxb7 import extract as X  # noqa: E402
from pxb7 import gateway as G  # noqa: E402

REAL_LIST_HTML = (PROJECT_ROOT / "tests" / "fixtures" / "real_card_pxb7_20261003.html"
                  ).read_text(encoding="utf-8")

CARDS_PAYLOAD = {"url": "https://www.pxb7.com/buy/10026/1", "html": REAL_LIST_HTML}
# 详情页夹具带面包屑 href="/buy/10026/1"（本地 raw 实测的详情页游戏识别锚点）
DETAIL_HTML = ("<html><body><a href=\"/buy/10026/1\">原神</a>"
               "<div class='viewer-count'>1234人正在浏览</div>"
               "<div class='collect-count'>56人已收藏</div></body></html>")
DETAIL_PAYLOAD = {"url": "https://www.pxb7.com/product/2428022628844555568/1",
                  "html": DETAIL_HTML}
CAPTCHA_HTML = ("<html><body><div>访问验证 为保证您的正常访问,请进行如下验证</div>"
                "<div>请按住滑块，拖动到最右边</div></body></html>")


def _post(state: G.GatewayState, path: str, payload: dict) -> tuple[int, dict]:
    return G.handle_ingest(state, path, json.dumps(payload).encode("utf-8"))


@pytest.fixture()
def settings(tmp_path: Path) -> cfg.Settings:
    base = cfg.load_settings()
    return dataclasses.replace(base, project_root=tmp_path, paths=dataclasses.replace(
        base.paths, db=tmp_path / "gw.duckdb", raw_root=tmp_path / "raw",
        state_dir=tmp_path / "state", runs=tmp_path / "runs",
        risk_state=tmp_path / "risk.json"))


@pytest.fixture()
def state(settings: cfg.Settings) -> G.GatewayState:
    db.init_db(settings.paths.db)
    with db.connect(settings.paths.db) as conn:      # 与 init-db 一致：词表种子入库
        db.upsert_dim_keywords(conn, X.seed_db_rows())
    tasks = cfg.load_tasks(settings=settings)
    return G.GatewayState(settings, tasks.tasks, default_task_id="genshin_official")


# --------------------------------------------------------------------------- #
# 列表页入库
# --------------------------------------------------------------------------- #
def test_ingest_cards_real_dom(state: G.GatewayState, settings: cfg.Settings) -> None:
    """【端到端等价】真实 DOM（校准夹具）经网关入库：parser v0.2.0 + 词表抽取 + collected_via=login。"""
    status, res = _post(state, "/ingest/cards", CARDS_PAYLOAD)
    assert status == 200 and res["ok"] is True
    assert res["cards_seen"] == 2 and res["cards_parsed"] == 2
    assert res["parse_success_rate"] == 1.0
    assert res["snapshots_inserted"] == 2 and res["new_listings"] == 2

    conn = db.connect(settings.paths.db, read_only=True)
    try:
        row = conn.execute("SELECT collected_via, price_yuan, level FROM fct_listing_snapshot"
                           " WHERE listing_id='2428022628844555568'").fetchone()
        assert row[0] == "login", "插件数据来自用户登录态浏览的页面，如实标注 login"
        assert float(row[1]) == 300.0, "price 属性（分）必须 ÷100"
        features = conn.execute("SELECT extracted_features FROM fct_listing_snapshot"
                                " WHERE listing_id='2428022628844555568'").fetchone()[0]
        assert features is not None
        assert conn.execute("SELECT count(*) FROM fct_listing_keyword").fetchone()[0] >= 1
    finally:
        conn.close()

    # raw 落盘 + summary（bronze 可重放；raw_root/{task}/{date}/{run_id} 三层）
    htmls = list(Path(settings.paths.raw_root).rglob("list_p01.html"))
    assert len(htmls) == 1
    run_dir = htmls[0].parent
    assert (run_dir / "list_p01.json").is_file()
    meta = json.loads((run_dir / "list_p01.json").read_text(encoding="utf-8"))
    assert meta["channel"] == "plugin" and meta["collected_via"] == "login"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed" and summary["cards_seen"] == 2


def test_ingest_cards_is_idempotent(state: G.GatewayState) -> None:
    """用户刷新页面重复发送 → 同轮先删后插，不产生重复行。"""
    s1 = _post(state, "/ingest/cards", CARDS_PAYLOAD)[1]
    s2 = _post(state, "/ingest/cards", CARDS_PAYLOAD)[1]
    assert s1["snapshots_inserted"] == 2 and s2["snapshots_inserted"] == 2
    status_res = G.handle_status(state)
    assert status_res["db"]["snapshot_rows"] == 2


def test_waf_page_rejected_and_not_stored(state: G.GatewayState, settings: cfg.Settings) -> None:
    """【防线回归】拦截页（滑块文本）必须拒收且不落任何 raw/DB（与 parse-raw 同一防线）。"""
    status, res = _post(state, "/ingest/cards",
                        {"url": "https://www.pxb7.com/buy/10026/1", "html": CAPTCHA_HTML})
    assert status == 422 and res["error"] == "risk-page"
    status, res = _post(state, "/ingest/detail",
                        {"url": "https://www.pxb7.com/product/123/1", "html": CAPTCHA_HTML})
    assert status == 422 and res["error"] == "risk-page"
    assert G.handle_status(state)["db"]["snapshot_rows"] == 0
    assert not (Path(settings.paths.raw_root)).exists() or \
        not any((Path(settings.paths.raw_root)).rglob("*.html")), "拦截页不得落 raw"


def test_unselected_game_is_rejected(state: G.GatewayState) -> None:
    """目标采集：未勾选的游戏页面直接拒收（默认目标 = 网关启动任务 genshin）。"""
    status, res = _post(state, "/ingest/cards",
                        {"url": "https://www.pxb7.com/buy/10012/1", "html": REAL_LIST_HTML})
    assert status == 422 and res["error"] == "target-not-selected"
    assert res["selected_tasks"] == ["genshin_official"]
    assert "采集目标" in res["message"]
    assert state.stats["target_skipped"] == 1


def test_ingest_records_page_no_and_sweep(state: G.GatewayState,
                                          settings: cfg.Settings) -> None:
    """页内「加载更多」的透明记录：page_no 进 raw 文件名与元数据、sweep 统计随批次透传。"""
    payload = {**CARDS_PAYLOAD, "page_no": 2,
               "sweep": {"target": 48, "initial": 16, "final": 48, "rounds": 3,
                         "stalled": 0, "stop": "done-target"}}
    status, res = _post(state, "/ingest/cards", payload)
    assert status == 200 and res["ok"] and res["page_no"] == 2
    htmls = sorted(Path(settings.paths.raw_root).rglob("list_p02.html"))
    assert len(htmls) == 1, "raw 文件名须带页码（list_pNN）"
    meta = json.loads(htmls[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["page_no"] == 2 and meta["sweep"]["final"] == 48
    assert meta["sweep"]["stop"] == "done-target"
    batch = list(state.recent_batches)[-1]
    assert batch["page_no"] == 2 and batch["sweep_rounds"] == 3
    # 非法页码/统计：静默回落，不阻断入库
    bad = _post(state, "/ingest/cards", {**CARDS_PAYLOAD, "page_no": "x",
                                         "sweep": {"target": "big"}})[1]
    assert bad["ok"] and bad["page_no"] == 1


# --------------------------------------------------------------------------- #
# 数据浏览（/listings：按游戏换列名 + 翻页）
# --------------------------------------------------------------------------- #
def test_listings_pagination_and_per_game_columns(state: G.GatewayState) -> None:
    """列名用该游戏自己的词表说法；翻页由 offset/limit 控制（不再固定最近 15 条）。"""
    for page in (1, 2):
        status, res = _post(state, "/ingest/cards",
                            {**CARDS_PAYLOAD,
                             "url": f"https://www.pxb7.com/buy/10026/1?page={page}"})
        assert status == 200 and res["ok"]
    page1 = G.handle_listings(state, {"limit": ["2"], "offset": ["0"], "game_id": ["10026"]})
    page2 = G.handle_listings(state, {"limit": ["2"], "offset": ["2"], "game_id": ["10026"]})
    assert page1["ok"] and page1["total"] == 2 and len(page1["rows"]) == 2
    assert page1["rows"][0]["game_name"] == "原神"
    # 夹具只有 2 个 listing：第 1 页 2 条，第 2 页应为空（总数以 count 为准）
    assert page2["rows"] == []
    assert [c["label"] for c in page1["columns"]][:4] == ["等级", "黄数", "五星角色", "五星武器"]
    labels = [c["label"] for c in page1["columns"]]
    assert "原石" in labels and "纠缠之源" in labels
    assert "共鸣链（N命）" not in labels, "原神视图不得出现鸣潮术语"
    assert all(r["cells"] for r in page1["rows"]), "每行都要带该游戏的列值"
    # 不筛游戏（全部）时用通用列，避免把某一家的术语硬套到别家行上
    generic = G.handle_listings(state, {"limit": ["15"], "offset": ["0"]})
    assert [c["label"] for c in generic["columns"]] == ["等级", "黄数", "区服", "邮箱", "商品发布时间"]


def test_listings_game_filter_switches_vocabulary(state: G.GatewayState) -> None:
    """切到鸣潮：列名换成鸣潮词表说法（共鸣链/精N），且只返回该游戏的行。"""
    G.handle_config_post(state, {"targets": ["genshin_official", "wuwa_official"]})
    assert _post(state, "/ingest/cards", CARDS_PAYLOAD)[0] == 200
    assert _post(state, "/ingest/cards",
                 {"url": "https://www.pxb7.com/buy/10302/1", "html": REAL_LIST_HTML})[0] == 200
    view = G.handle_listings(state, {"game_id": ["10302"]})
    labels = [c["label"] for c in view["columns"]]
    assert "共鸣链（N命）" in labels and "武器精炼（精N）" in labels
    assert "原石" not in labels and "纠缠之源" not in labels, "鸣潮视图不得出现原神术语"
    assert {c["source"] for c in view["columns"] if c["label"].startswith(("共鸣链", "武器精炼"))} \
        == {"feat"}, "鸣潮命座/精炼来自抽取特征，来源需如实标注"
    assert view["total"] == 2 and all(r["game_id"] == 10302 for r in view["rows"])


def test_delta_view_uses_delta_vocabulary_and_features(state: G.GatewayState) -> None:
    """三角洲（docs/02 §4.B）：列名用 delta 画像说法，取值走 feat:extracted_features。"""
    G.handle_config_post(state, {"targets": ["genshin_official", "delta_official"]})
    listing_id = "2429000000000000001"
    list_html = (
        "<html><body><ul>"
        f"<a href=\"/product/{listing_id}/1\"><div class=\"middleCard\""
        f" productid=\"{listing_id}\" bizprod=\"1\" status=\"1\" h5imgcount=\"10\""
        " createtime=\"2026-10-03 10:00:00\" verifiedseller=\"0\" producttype=\"1\">"
        "<div class=\"info\"><div class=\"t-space-item\"><div class=\"smallCardTitle\""
        " attrnamelist=\"steam国服\" gameid=\"10371\" gamename=\"三角洲行动\" price=\"18000\""
        " productname=\"三角洲行动 总资产：57.4M，哈夫币：100W，烽火60级：钻石，战场50级：上等兵\">"
        "三角洲行动 | steam国服</div></div></div></div></a></ul></body></html>")
    assert _post(state, "/ingest/cards",
                 {"url": "https://www.pxb7.com/buy/10371/1", "html": list_html})[0] == 200
    detail_html = (
        "<html><body><a href=\"/buy/10371/1\">三角洲行动</a>"
        "<div class=\"product-detail\"><h1>总资产：57.4M，哈夫币：100W，传说武器30，史诗武器47，"
        "烽火60级：钻石，战场50级：上等兵【干员皮肤5】露娜黑天际线【近战皮肤3】近战武器-处刑者"
        "【挂饰34】挂饰-无人机【载具3】轮式突击炮-荣耀【货币】26三角币，0曼德尔币，169三角券，"
        "流动资产35.9M，不动资产21.4M【QQ登录】【可二次实名】【找回包赔】</h1></div></body></html>")
    status, ack = _post(state, "/ingest/detail",
                        {"url": f"https://www.pxb7.com/product/{listing_id}/1",
                         "html": detail_html})
    assert status == 200 and ack["ok"] and ack["snapshot_rows_updated"] == 1

    view = G.handle_listings(state, {"game_id": ["10371"]})
    labels = [c["label"] for c in view["columns"]]
    assert "红皮/大红" in labels and "武器皮肤" in labels and "挂饰" in labels \
        and "烽火等级" in labels and "三角券" in labels and "实名受限（不可二次）" in labels
    assert "原石" not in labels and "共鸣链（N命）" not in labels, "三角洲视图不得出现别家术语"
    assert {c["source"] for c in view["columns"] if c["label"] in ("红皮/大红", "三角币")} == {"feat"}
    assert view["total"] == 1
    cells = view["rows"][0]["cells"]
    assert cells["delta_operator_skin_cnt"] == 5 and cells["delta_melee_skin_cnt"] == 3
    assert cells["delta_charm_cnt"] == 34 and cells["delta_vehicle_cnt"] == 3
    assert cells["delta_legendary_weapon_cnt"] == 30 and cells["delta_epic_weapon_cnt"] == 47
    assert cells["delta_fenghuo_level_cnt"] == 60
    assert cells["delta_triangle_coin_cnt"] == 26 and cells["delta_mandela_coin_cnt"] == 0, \
        "0曼德尔币是有效值（0 与缺失分开）"
    assert cells["delta_service_recall_flag"] is True and cells["delta_second_realname_flag"] is True


def test_listings_invalid_params_fall_back(state: G.GatewayState) -> None:
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    res = G.handle_listings(state, {"limit": ["9999"], "offset": ["-5"], "game_id": ["abc"]})
    assert res["ok"] and res["limit"] == G.LISTINGS_PAGE_DEFAULT
    assert res["offset"] == 0 and res["game_id"] is None
    assert [g["game_id"] for g in res["games"]] == [10026], "games 列表用于看板筛选"


def test_stats_keywords_grouped_by_game(state: G.GatewayState) -> None:
    """词表命中按游戏分组（各游戏词表不混算），看板据此切换。"""
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    payload = G.build_stats_payload(state)
    by_game = payload["db"]["top_keywords_by_game"]
    assert "10026" in by_game and by_game["10026"]["game_name"] == "原神"
    assert by_game["10026"]["top"], "原神应有词表命中"
    assert all(h["keyword"] for h in by_game["10026"]["top"])
    assert payload["db"]["top_keywords"], "全局 Top 列表保留（全部游戏视图用）"


def test_invalid_body_rejected(state: G.GatewayState) -> None:
    assert G.handle_ingest(state, "/ingest/cards", b"not-json")[0] == 400
    assert G.handle_ingest(state, "/ingest/cards", b"[]")[0] == 400
    assert G.handle_ingest(state, "/ingest/cards", b"")[0] == 400


# --------------------------------------------------------------------------- #
# 详情页（M5）：先到挂起、后到回填
# --------------------------------------------------------------------------- #
def test_detail_before_cards_is_pending_then_merged(state: G.GatewayState,
                                                    settings: cfg.Settings) -> None:
    """详情页先于列表到达：暂存 pending，随下一批卡片入库（replay 的 detail_map 语义）。"""
    status, res = _post(state, "/ingest/detail", DETAIL_PAYLOAD)
    assert status == 200 and res["ok"] and res["snapshot_rows_updated"] == 0
    assert "pending" in res
    assert res["viewers_masked"] == 1234 and res["favorites_cnt"] == 56

    assert G.handle_status(state)["pending_details"] == 1
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        row = conn.execute("SELECT viewers_masked, favorites_cnt FROM fct_listing_snapshot"
                           " WHERE listing_id='2428022628844555568'").fetchone()
        assert row[0] == 1234 and row[1] == 56, "pending 详情字段必须随卡片批次合并入库"
    finally:
        conn.close()
    assert G.handle_status(state)["pending_details"] == 0


def test_detail_after_cards_updates_latest_row(state: G.GatewayState,
                                               settings: cfg.Settings) -> None:
    """卡片先入库、详情后到：回填该 listing 最近一轮快照。"""
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    status, res = _post(state, "/ingest/detail", DETAIL_PAYLOAD)
    assert status == 200 and res["snapshot_rows_updated"] == 1
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        row = conn.execute("SELECT viewers_masked, favorites_cnt FROM fct_listing_snapshot"
                           " WHERE listing_id='2428022628844555568'").fetchone()
        assert row[0] == 1234 and row[1] == 56
    finally:
        conn.close()


def test_masked_detail_is_stored_honestly(state: G.GatewayState) -> None:
    """游客视角的打码详情页：NULL + mask_detected 如实标注，不得编造数值。"""
    payload = {"url": "https://www.pxb7.com/product/2428022628844555568/1",
               "html": "<html><body><a href=\"/buy/10026/1\">原神</a>"
                       "<div class='viewer-count'>登录后可见</div></body></html>"}
    status, res = _post(state, "/ingest/detail", payload)
    assert status == 200 and res["ok"]
    assert res["viewers_masked"] is None and res["viewers_mask_detected"] is True


def test_detail_feed_noise_is_not_extracted(state: G.GatewayState) -> None:
    """【真实 DOM 校准回归】详情页实时动态流（打码用户 ID + 行为）不得被当成
    正在浏览/收藏数值：'**8054 正在浏览这个商品'（无「人」字）与
    '00:41:36 收藏了这个商品'（时间戳）都必须排除；真实锚点是「6人已收藏」。"""
    feed = ("<html><body><a href=\"/buy/10026/1\">原神</a>"
            "<div class='text-Medium'>**8054 正在浏览这个商品</div>"
            "<div class='text-Medium'>**6348 刚刚收藏了这个商品</div>"
            "<div class='text-Medium'>**2042 00:41:36 收藏了这个商品</div>"
            "<span>6人已收藏</span></body></html>")
    payload = {"url": "https://www.pxb7.com/product/2428022628844555568/1", "html": feed}
    status, res = _post(state, "/ingest/detail", payload)
    assert status == 200 and res["ok"]
    assert res["viewers_masked"] is None, "动态流打码 ID 不得当浏览数"
    assert res["favorites_cnt"] == 6, "真实锚点「N人已收藏」"


# --------------------------------------------------------------------------- #
# 目标采集（多任务路由 / 详情页游戏识别）
# --------------------------------------------------------------------------- #
def test_targets_config_switches_routing(state: G.GatewayState, settings: cfg.Settings) -> None:
    """勾选鸣潮后：鸣潮列表入库、原神列表被拒；目标变化即时生效、按游戏独立入库。"""
    assert _post(state, "/ingest/cards", {"url": "https://www.pxb7.com/buy/10302/1",
                                          "html": REAL_LIST_HTML})[1]["error"] == \
        "target-not-selected"
    status, res = G.handle_config_post(state, {"targets": ["wuwa_official"]})
    assert status == 200 and res["targets"]["effective"] == ["wuwa_official"]
    ok = _post(state, "/ingest/cards", {"url": "https://www.pxb7.com/buy/10302/1",
                                        "html": REAL_LIST_HTML})[1]
    assert ok["ok"] and ok["task_id"] == "wuwa_official" and ok["game_id"] == 10302
    assert ok["snapshots_inserted"] == 2
    rejected = _post(state, "/ingest/cards", {"url": "https://www.pxb7.com/buy/10026/1",
                                              "html": REAL_LIST_HTML})
    assert rejected[0] == 422 and rejected[1]["error"] == "target-not-selected"
    conn = db.connect(settings.paths.db, read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM dim_listing WHERE game_id = 10302"
                            ).fetchone()[0] == 2
    finally:
        conn.close()


def test_config_partial_update_keeps_other_keys(state: G.GatewayState) -> None:
    """部分键更新：只改 targets/paths，不重置其它配置（PUT 整包语义的修正）。"""
    G.handle_config_post(state, {"debug": True})
    saved = G.handle_config_post(state, {"targets": ["naruto_official"]})[1]["config"]
    assert saved["debug"] is True and saved["auto_ingest"] is True
    assert saved["targets"] == ["naruto_official"]


def test_config_rejects_bad_targets(state: G.GatewayState) -> None:
    assert G.handle_config_post(state, {"targets": ["nope"]})[0] == 400, "未知任务 ID"
    assert G.handle_config_post(state, {"targets": "genshin_official"})[0] == 400, "类型错"
    assert G.handle_config_post(state, {"targets": ["bad id!"]})[0] == 400


def test_targets_payload_shape(state: G.GatewayState) -> None:
    t = G.targets_payload(state)
    assert len(t["available"]) == 4, "tasks.yaml 四个游戏任务都可选"
    assert t["mode"] == "default" and t["effective"] == ["genshin_official"]
    assert t["games"] == [10026]
    names = {x["task_id"]: x["name"] for x in t["available"]}
    assert names["wuwa_official"] == "鸣潮-官服" and names["delta_official"] == "三角洲行动-官服"
    stats = G.build_stats_payload(state)
    assert stats["targets"]["available"][0]["snapshots"] == 0
    assert isinstance(stats["paths"]["items"], list)


def test_detail_game_resolution_paths(state: G.GatewayState) -> None:
    """详情页游戏识别链：本地库 → 面包屑链接；识别不了如实拒收（不猜游戏）。"""
    _post(state, "/ingest/cards", CARDS_PAYLOAD)                 # 库内注册该 listing
    by_db = _post(state, "/ingest/detail", {**DETAIL_PAYLOAD,
                 "html": "<html><body><span>8人已收藏</span></body></html>"})  # 无锚点也能靠库识别
    assert by_db[0] == 200 and by_db[1]["snapshot_rows_updated"] == 1

    crumb = ("<html><body><a href=\"/buy/10026/1\">原神</a>"
             "<span>3人已收藏</span></body></html>")
    status, res = _post(state, "/ingest/detail",
                        {"url": "https://www.pxb7.com/product/2428022628844555570/1",
                         "html": crumb})
    assert status == 200 and res["game_id"] == 10026 and res["task_id"] == "genshin_official"

    status, res = _post(state, "/ingest/detail",
                        {"url": "https://www.pxb7.com/product/2428022628844555571/1",
                         "html": "<html><body>无任何游戏锚点</body></html>"})
    assert status == 422 and res["error"] == "game-unresolved"


def test_detail_for_unselected_game_rejected(state: G.GatewayState) -> None:
    """目标外游戏的详情页：拒收（不得只因为"详情 URL 没有 game_id"就放行）。"""
    crumb = ("<html><body><a href=\"/buy/10371/1\">三角洲行动</a>"
             "<span>9人已收藏</span></body></html>")
    status, res = _post(state, "/ingest/detail",
                        {"url": "https://www.pxb7.com/product/123123123123123123/1",
                         "html": crumb})
    assert status == 422 and res["error"] == "target-not-selected" and res["game_id"] == 10371


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #
def test_status_reports_counts_and_stats(state: G.GatewayState) -> None:
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    payload = G.handle_status(state)
    assert payload["ok"] and payload["task"] == "genshin_official"
    assert payload["db"]["snapshot_rows"] == 2
    assert payload["stats"]["batches"] == 1
    assert payload["stats"]["cards_parsed"] == 2
    assert payload["stats"]["last_run_id"]


# --------------------------------------------------------------------------- #
# 看板与远端配置（/stats /config / 看板资产）
# --------------------------------------------------------------------------- #
def test_plugin_config_defaults_and_roundtrip(settings: cfg.Settings) -> None:
    assert G.load_plugin_config(settings) == G.DEFAULT_PLUGIN_CONFIG, "无文件 → 默认配置"
    saved = G.save_plugin_config(settings, {"auto_ingest": False, "reingest_interval_min": 30})
    assert saved == {**G.DEFAULT_PLUGIN_CONFIG, "auto_ingest": False,
                     "reingest_interval_min": 30}, "未提供的键保持默认"
    assert saved["targets"] == [] and saved["paths"] == {}
    assert saved["cards_target"] == 16, "默认一次一页（站点一页 16 张）"
    assert G.load_plugin_config(settings) == saved, "落盘后可读回"
    with pytest.raises(G.GatewayError):
        G.save_plugin_config(settings, {"reingest_interval_min": 0})       # 范围外
    with pytest.raises(G.GatewayError):
        G.save_plugin_config(settings, {"cards_target": 0})                # 低于一张
    with pytest.raises(G.GatewayError):
        G.save_plugin_config(settings, {"cards_target": 500})              # 超上限
    assert G.save_plugin_config(settings, {"cards_target": 48})["cards_target"] == 48
    for count in (1, 8, 17, 31, 200):
        assert G.save_plugin_config(settings, {"cards_target": count})["cards_target"] == count
    for mode in ("list", "detail"):
        assert G.save_plugin_config(settings, {"collection_mode": mode})["collection_mode"] == mode
    for invalid in ("fast", None, 1, True):
        with pytest.raises(G.GatewayError):
            G.save_plugin_config(settings, {"collection_mode": invalid})
    assert saved["title_interval_ms"] == 3000
    # 2026-10-04 用户指令：全文请求间隔可调 0–10 秒、0.1 秒步进（网关按 100ms 粒度取整）
    assert G.save_plugin_config(settings, {"title_interval_ms":6000})["title_interval_ms"] == 6000
    for valid, expect in ((0, 0), (10000, 10000), (3500.4, 3500.0),
                          (100.00000000000001, 100)):          # JS「秒*1000」浮点误差被取整吸收
        assert G.save_plugin_config(settings, {"title_interval_ms": valid})["title_interval_ms"] == expect
    for invalid in (-100, 10001, 15000, "fast", True, float("nan")):
        with pytest.raises(G.GatewayError):
            G.save_plugin_config(settings, {"title_interval_ms":invalid})
    # 详情采集间隔（2026-10-05 用户指令）：与列表间隔互不共用，同口径 0–10000ms
    assert saved["detail_interval_ms"] == 0
    for valid, expect in ((10000, 10000), (3500.4, 3500.0), (100.00000000000001, 100)):
        assert G.save_plugin_config(settings, {"detail_interval_ms": valid})["detail_interval_ms"] == expect
    for invalid in (-100, 10001, "fast", True):
        with pytest.raises(G.GatewayError):
            G.save_plugin_config(settings, {"detail_interval_ms":invalid})
    with pytest.raises(G.GatewayError):
        G.save_plugin_config(settings, {"unknown": 1})                     # 未知键
    with pytest.raises(G.GatewayError):
        G.save_plugin_config(settings, {"auto_ingest": "yes"})             # 类型错
    with pytest.raises(G.GatewayError):
        G.save_plugin_config(settings, {"targets": ["bad id!"]})           # 非法任务 ID
    with pytest.raises(G.GatewayError):
        G.save_plugin_config(settings, {"paths": {"db": "rel.duckdb"}})    # 相对路径


def test_corrupt_plugin_config_falls_back_to_defaults(settings: cfg.Settings,
                                                      tmp_path: Path) -> None:
    path = tmp_path / "state" / "plugin_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad json", encoding="utf-8")
    assert G.load_plugin_config(settings) == G.DEFAULT_PLUGIN_CONFIG


def test_recent_batches_recorded(state: G.GatewayState) -> None:
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    _post(state, "/ingest/detail", DETAIL_PAYLOAD)
    batches = list(state.recent_batches)
    assert [b["kind"] for b in batches] == ["cards", "detail"]
    assert batches[0]["cards_seen"] == 2 and batches[0]["snapshots"] == 2
    assert batches[1]["rows_updated"] == 1 and batches[1]["listing_id"] == "2428022628844555568"


def test_build_stats_payload_fields(state: G.GatewayState) -> None:
    _post(state, "/ingest/cards", CARDS_PAYLOAD)
    payload = G.build_stats_payload(state)
    assert payload["ok"] is True and payload["task"]["task_id"] == "genshin_official"
    assert payload["db"]["snapshot_rows"] == 2
    assert payload["db"]["rounds"] and payload["db"]["rounds"][-1]["rows"] == 2
    assert payload["db"]["collected_via"] == {"login": 2}
    assert payload["config"]["auto_ingest"] is True
    assert "global_stop" in payload["risk"]
    assert len(payload["recent_batches"]) == 1
    assert payload["db"]["latest_listings"][0]["price"] is not None


def test_dashboard_and_userscript_assets_exist() -> None:
    from pxb7.gateway import _DASHBOARD_FILE, _USERSCRIPT_FILE
    assert _DASHBOARD_FILE.is_file()
    dashboard = _DASHBOARD_FILE.read_text(encoding="utf-8")
    assert "pxb7 采集看板" in dashboard
    for marker in ("采集目标", "数据存放位置", 'id="targets"', 'id="paths"', "paths_override",
                   "ext-pill"):
        assert marker in dashboard, f"看板缺少「目标采集/数据路径」要素：{marker}"
    text = _USERSCRIPT_FILE.read_text(encoding="utf-8")
    assert text.startswith("// ==UserScript==")
    assert "@connect      127.0.0.1" in text and "@version      0.6.0" in text
    assert "@updateURL" in text and "@downloadURL" in text, "必须带 TM 自动更新源"
    assert "channel=userscript" in text, "版本上报需区分通道"
    # 看板面板（浏览器插件形态）：开关按钮 + 面板骨架 + 关键动作 + 采集目标 + 采集张数
    for marker in ("pxb7-panel-toggle", "pxb7-panel", "/stats", "/config", "/shutdown",
                   "pxb7-p-targets", "targetBlocked", "pxb7-p-cards", "expandCards"):
        assert marker in text, f"用户脚本缺少面板要素：{marker}"


# --------------------------------------------------------------------------- #
# 用户脚本版本上报与一键更新检测
# --------------------------------------------------------------------------- #
def test_script_version_reporting_and_update_detection(settings: cfg.Settings,
                                                       monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    fake = tmp_path / "userscript.js"
    fake.write_text("// ==UserScript==\n// @version 9.9.9\n// ==/UserScript==\n",
                    encoding="utf-8")
    monkeypatch.setattr(G, "_USERSCRIPT_FILE", fake)
    db.init_db(settings.paths.db)
    state = G.GatewayState(settings, cfg.load_tasks(settings=settings)
                           .by_id("genshin_official"))

    info = G.script_info(state)
    assert info["latest_version"] == "9.9.9" and info["installed_version"] is None
    assert info["update_available"] is False, "未收到上报时不得误报更新"

    G.record_script_version(state, "0.3.0")
    info = G.script_info(state)
    assert info["installed_version"] == "0.3.0" and info["update_available"] is True

    G.record_script_version(state, "9.9.9")
    assert G.script_info(state)["update_available"] is False, "同版本不报更新"

    G.record_script_version(state, "bad version!!")     # 非法字符 → 忽略不记录
    assert G.script_info(state)["installed_version"] == "9.9.9"


def test_extension_channel_version_reporting(state: G.GatewayState) -> None:
    """扩展与用户脚本分通道上报：看板分别显示（扩展基线=仓库 manifest 版本）。"""
    ext_latest = G._latest_extension_version()
    assert ext_latest, "仓库内扩展 manifest 必须可读"
    info = G.script_info(state)
    assert info["extension"]["latest_version"] == ext_latest
    assert info["extension"]["installed_version"] is None and not info["extension"]["update_available"]
    G.record_script_version(state, "0.0.1", channel="extension")
    assert G.script_info(state)["extension"]["update_available"] is True
    G.record_script_version(state, ext_latest, channel="extension")
    assert G.script_info(state)["extension"]["update_available"] is False
    assert G.script_info(state)["installed_version"] is None, "扩展上报不得写进用户脚本通道"


def test_stats_payload_includes_script_info(state: G.GatewayState) -> None:
    payload = G.build_stats_payload(state)
    assert "script" in payload and "update_available" in payload["script"]
    assert payload["script"]["update_url"] == "/userscript.js"
    assert "extension" in payload["script"], "扩展通道独立上报（一键更新路径区分）"


def test_origin_policy_allows_pxb7_and_extensions() -> None:
    """【回归】来源白名单：pxb7 页面 + 本机浏览器扩展 + 本机看板页放行，其余拒绝。

    独立扩展的 SW/弹窗直连网关时 Origin 为 chrome-extension://…——此前被误拒 403
    （表现为「网关不可达」，且大请求因服务器提前断连而 Failed to fetch）。"""
    assert G.origin_allowed(None), "无 Origin（本机 SW/客户端）放行"
    assert G.origin_allowed("https://www.pxb7.com")
    assert G.origin_allowed("chrome-extension://ohjdlklcnnfnokbgelpgfbcjcfabnamf")
    assert G.origin_allowed("moz-extension://whatever")
    assert G.origin_allowed("http://127.0.0.1:8765"), "看板页自身来源（同源 POST 也带 Origin）"
    assert not G.origin_allowed("https://evil.example")
    assert not G.origin_allowed("http://127.0.0.1:9999.evil.com")
    # 本机动作端点（打开目录）更严：pxb7 网页来源不放行
    assert G.origin_allowed_local(None) and G.origin_allowed_local("chrome-extension://x")
    assert G.origin_allowed_local("http://127.0.0.1:8765")
    assert not G.origin_allowed_local("https://www.pxb7.com")


# --------------------------------------------------------------------------- #
# 数据路径：展示 / 自定义 / 打开目录
# --------------------------------------------------------------------------- #
def test_paths_display_and_customization(state: G.GatewayState, settings: cfg.Settings,
                                         monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path) -> None:
    """数据路径自定义：建目录 + 新库自动初始化 + 覆写文件落盘；恢复默认回 settings.yaml。"""
    override_file = tmp_path / "paths_override.json"
    monkeypatch.setattr(G.cfgmod, "PATHS_OVERRIDE_FILE", override_file)

    def fake_load_settings(*_a: object, **_k: object) -> cfg.Settings:
        """等价 load_settings 的真实语义：settings.yaml 基准 + 覆写文件叠加。"""
        return G.cfgmod.apply_path_overrides(settings, G.cfgmod.load_path_overrides())

    monkeypatch.setattr(G.cfgmod, "load_settings", fake_load_settings)

    items = G.paths_payload(state)["items"]
    assert len(items) == 9 and not any(i["customized"] for i in items)
    assert {i["key"] for i in items if i["overridable"]} == {"db", "raw_root", "runs", "log_dir"}
    assert next(i for i in items if i["key"] == "state_dir")["overridable"] is False

    root = tmp_path / "bigdisk" / "pxb7-data"
    status, res = G.handle_config_post(state, {"paths": {
        "db": str(root / "pxb7.duckdb"), "raw_root": str(root / "raw" / "pxb7"),
        "runs": str(root / "runs"), "log_dir": str(root / "logs"),
    }})
    assert status == 200 and res["ok"], res
    assert Path(state.settings.paths.db) == root / "pxb7.duckdb"
    assert (root / "raw" / "pxb7").is_dir() and (root / "logs").is_dir()
    assert override_file.is_file(), "覆写文件必须落盘（CLI 下次运行同源生效）"
    customized = {i["key"] for i in G.paths_payload(state)["items"] if i["customized"]}
    assert {"db", "raw_root", "runs", "log_dir"} <= customized
    conn = db.connect(state.settings.paths.db, read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM dim_task").fetchone()[0] == 4, "新库自动初始化"
        assert conn.execute("SELECT count(*) FROM dim_keyword").fetchone()[0] > 0
    finally:
        conn.close()

    # 非法输入：相对路径 / 不可自定义键 / 非对象 → 400，且生效路径不变
    assert G.handle_config_post(state, {"paths": {"db": "rel.duckdb"}})[0] == 400
    assert G.handle_config_post(state, {"paths": {"state_dir": str(tmp_path)}})[0] == 400
    assert G.handle_config_post(state, {"paths": "x"})[0] == 400
    assert Path(state.settings.paths.db) == root / "pxb7.duckdb"

    # 恢复默认（paths={}）：删覆写文件 + 回 settings.yaml 路径
    status, res = G.handle_config_post(state, {"paths": {}})
    assert status == 200 and not override_file.is_file()
    assert Path(state.settings.paths.db) == settings.paths.db


def test_open_folder_whitelist(state: G.GatewayState,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    """打开目录：键走白名单（不接受任意路径）；非白名单/非对象 400。"""
    if not hasattr(G.os, "startfile"):
        pytest.skip("非 Windows 平台不支持 os.startfile")
    opened: list[str] = []
    monkeypatch.setattr(G.os, "startfile", lambda p: opened.append(p))
    status, res = G.handle_open_folder(state, {"key": "raw_root"})
    assert status == 200 and res["ok"] and opened == [str(state.settings.paths.raw_root)]
    assert G.handle_open_folder(state, {"key": "etc"})[0] == 400
    assert G.handle_open_folder(state, {"key": "../../windows"})[0] == 400
    assert G.handle_open_folder(state, "not-a-dict")[0] == 400
