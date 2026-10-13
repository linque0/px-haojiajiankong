"""最新快照查缺与补录；只使用本地数据，站点访问由扩展串行队列负责。"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import db, extract as X, pipeline as PL
from . import weapons as W

SCALARS = ("level", "yellow_cnt", "five_star_chars", "five_star_weapons")
LABELS = {"level": "等级", "yellow_cnt": "黄数", "five_star_chars": "五星角色数",
          "five_star_weapons": "五星武器数", "delta_fenghuo_level_cnt": "烽火等级",
          "delta_battlefield_level_cnt": "战场等级", "delta_legendary_weapon_cnt": "传说武器数",
          "delta_epic_weapon_cnt": "史诗武器数", "delta_knife_skin_cnt": "刀皮数",
          "five_star_character_chains": "共鸣链（N命）", "five_star_weapon_refinements": "武器精炼（精N）",
          "wuwa_resources": "资源",
          "server": "区服", "publish_time_text": "商品发布时间", "favorites_cnt": "收藏"}
RULES = {10026: SCALARS, 10302: SCALARS, 10032: ("level",),
         10371: ("level", "delta_fenghuo_level_cnt", "delta_battlefield_level_cnt",
                 "delta_legendary_weapon_cnt", "delta_epic_weapon_cnt", "delta_knife_skin_cnt")}
# 鸣潮必填列（docs/20）：资源与区服/发布时间/收藏沿用原规则；
# 额外付费商品是可选信息，允许未展示或未采到，不参与缺失判定。
WUWA_GAME_ID = 10302
WUWA_COVERAGE = [LABELS[k] for k in SCALARS] + ["共鸣链（N命）", "武器精炼（精N）",
               "资源", "区服", "商品发布时间", "收藏"]
FALLBACKS = {"level": "account_level_cnt", "yellow_cnt": "yellow_cnt",
             "five_star_chars": "five_star_chars_cnt", "five_star_weapons": "five_star_weapons_cnt"}
ROSTERS = (("five_star_chars", "five_star_character_chains", "constellation_cnt"),
           ("five_star_weapons", "five_star_weapon_refinements", "five_star_weapon_refined"))


def features(raw: Any) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def value(row: dict, key: str) -> Any:
    scalar = row.get(key)
    return scalar if scalar is not None else row["features"].get(FALLBACKS.get(key, key))


def missing(row: dict) -> list[dict]:
    reasons = []
    for key in RULES.get(row["game_id"], ("level",)):
        number = value(row, key)
        if key == "five_star_weapons" and row["game_id"] == WUWA_GAME_ID \
                and W.status(number, row["features"])["state"] == "unreported":
            continue  # 完整详情未公示，保持数量未知；不因同一空字段反复补采。
        if not valid_count(number):
            reasons.append({"key": key, "label": LABELS[key], "reason": "缺失或无效"})
    if row["game_id"] in (10026, WUWA_GAME_ID):
        for count_key, roster_key, _ in ROSTERS:
            count = value(row, count_key)
            if not valid_count(count) or count <= 0:
                continue   # 数量为 0 不要求明细（既有 0 值纪律）；count>0 的名单校验在下方
            roster = row["features"].get(roster_key)
            valid = isinstance(roster, list) and all(
                isinstance(item, dict) and isinstance(item.get("name"), str)
                and X._ROSTER_NAME.fullmatch(item["name"].strip())
                and not X._ROSTER_NON_NAME.search(item["name"])
                and isinstance(item.get("value"), int)
                and not isinstance(item["value"], bool)
                and (0 <= item["value"] <= 6 if count_key == "five_star_chars"
                     else 1 <= item["value"] <= 5) for item in roster)
            if valid and count_key == "five_star_chars":
                valid = len({item["name"].strip() for item in roster}) == len(roster)
            if not valid or len(roster) != count:
                label = ("共鸣链明细" if row["game_id"] == WUWA_GAME_ID else "角色命座明细") \
                    if count_key == "five_star_chars" else "武器精炼明细"
                reasons.append({"key": roster_key, "label": label,
                                "reason": "明细缺失/无效或与数量不匹配"})
    if row["game_id"] == WUWA_GAME_ID:
        reasons.extend(_wuwa_extra_missing(row))
    return reasons


def _wuwa_extra_missing(row: dict) -> list[dict]:
    """鸣潮其余必填列；资源全键缺失才记缺失，采到 0 是有效值。"""
    feats = row["features"]
    reasons = []
    if not any(feats.get(key) is not None for key, _ in X.WUWA_RESOURCES):
        reasons.append({"key": "wuwa_resources", "label": LABELS["wuwa_resources"], "reason": "缺失"})
    if row.get("server") is None or not str(row["server"]).strip():
        reasons.append({"key": "server", "label": LABELS["server"], "reason": "缺失"})
    if row.get("publish_time_text") is None or not str(row["publish_time_text"]).strip():
        reasons.append({"key": "publish_time_text", "label": LABELS["publish_time_text"], "reason": "缺失"})
    if row.get("favorites_cnt") is None:
        reasons.append({"key": "favorites_cnt", "label": LABELS["favorites_cnt"], "reason": "缺失"})
    return reasons


def valid_count(number: Any) -> bool:
    return (isinstance(number, (int, float)) and not isinstance(number, bool)
            and math.isfinite(number) and number >= 0 and int(number) == number)


def latest_rows(conn, listing_id: str | None = None) -> list[dict]:
    # LEFT JOIN：快照存在而档案（dim_listing）缺失的"孤儿行"也要进清单，
    # 否则它们既不可补录也不可达删除，永远滞留库中。
    rows = conn.execute(
        "WITH latest AS (SELECT *, row_number() OVER (PARTITION BY listing_id"
        " ORDER BY snapshot_at DESC) AS rn FROM fct_listing_snapshot"
        " WHERE (? IS NULL OR listing_id=?))"
        " SELECT s.listing_id,s.snapshot_at,l.game_id,g.game_name,s.level,s.yellow_cnt,"
        " s.five_star_chars,s.five_star_weapons,s.server,s.publish_time_text,s.favorites_cnt,"
        " s.extracted_features"
        " FROM latest s LEFT JOIN dim_listing l USING(listing_id)"
        " LEFT JOIN dim_game g ON g.game_id=l.game_id AND g.biz_prod=1 WHERE rn=1"
        " ORDER BY s.snapshot_at DESC,s.listing_id DESC", [listing_id, listing_id]).fetchall()
    return [dict(id=r[0], snapshot_round=r[1].isoformat(), game_id=r[2],
                 game_name=r[3] or ("未知档案" if r[2] is None else str(r[2])),
                 **dict(zip(SCALARS, r[4:8])),
                 server=r[8], publish_time_text=r[9], favorites_cnt=r[10],
                 features=features(r[11])) for r in rows]


def _scope(state, payload: dict) -> set[int] | None:
    game = payload.get("game_id", "selected")
    if game == "selected":
        return {t.game_id for t in state.effective_tasks()}
    if game == "all":
        return None
    try:
        return {int(game)}
    except (ValueError, TypeError):
        raise ValueError("invalid-game-id") from None


def _candidates(state, conn, payload) -> tuple[list[dict], list[dict]]:
    scope = _scope(state, payload)
    candidates, games = [], {}
    for row in latest_rows(conn):
        gid = row["game_id"]
        reasons = missing(row)
        if gid is not None:
            # 孤儿行（game_id 缺失）只进"全部游戏"清单，不计入任何游戏的统计。
            game = games.setdefault(gid, {"game_id": gid, "game_name": row["game_name"],
                                          "total": 0, "missing": 0})
            game["total"] += 1
            game["missing"] += bool(reasons)
        if reasons and (scope is None or gid in scope):
            candidates.append({k: row[k] for k in ("id", "snapshot_round", "game_id", "game_name")}
                              | {"missing": reasons,
                                 "collectable": gid is not None and state.match_task(gid) is not None})
    return candidates, list(games.values())


def candidates(state, query: dict) -> dict:
    payload = {k: v[0] if isinstance(v, list) else v for k, v in query.items()}
    offset = max(0, int(payload.get("offset", 0)))
    limit = max(1, min(200, int(payload.get("limit", 10))))
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db, read_only=True) as conn:
        rows, games = _candidates(state, conn, payload)
    results = load_results(state)
    for row in rows:
        last = results.get(row["id"])
        if last:
            row["last_backfill"] = last
    return {"ok": True, "rows": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "limit": limit, "games": games,
            "can_backfill_all": bool(rows) and all(row["collectable"] for row in rows),
            "coverage": {str(gid): WUWA_COVERAGE if gid == WUWA_GAME_ID
                         else [LABELS[k] for k in RULES.get(gid, ("level",))]
                         + (["角色及武器明细"] if gid in (10026, WUWA_GAME_ID) else [])
                         for gid in (g["game_id"] for g in games)}}


def prepare(state, payload: dict) -> dict:
    with PL.run_lock(state.settings):
        conn = db.connect(state.settings.paths.db)
        try:
            rows, _ = _candidates(state, conn, payload)
            if payload.get("all") is not True:
                selections = payload.get("items")
                if not isinstance(selections, list) or not selections:
                    raise ValueError("empty-selection")
                selected = {(str(item["id"]), str(item["snapshot_round"])) for item in selections}
                rows = [r for r in rows if (r["id"], r["snapshot_round"]) in selected]
                if len(rows) != len(selected):
                    raise ValueError("selection-changed-refresh-list")
            if not rows:
                raise ValueError("no-candidates")
            if any(not row["collectable"] for row in rows):
                raise ValueError("target-not-selected")
            if any(not re.fullmatch(r"\d{6,24}", row["id"]) for row in rows):
                raise ValueError("invalid-listing-id")
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        backup = state.settings.paths.db.parent / "backups" / (
            "backfill-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S%f") + ".duckdb")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(state.settings.paths.db, backup)
    items = [{"id": r["id"], "snapshot_round": r["snapshot_round"], "game_id": r["game_id"],
              "url": f'https://www.pxb7.com/product/{r["id"]}/1'} for r in rows]
    return {"ok": True, "items": items, "backup": str(backup), "total": len(items), "batch_size": 200}


def delete(state, payload: dict) -> dict:
    """人工确认的缺失商品删除；全部轮次及关联事实，备份失败不写库。"""
    if payload.get("confirm_all_rounds") is not True:
        raise ValueError("delete-confirmation-required")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError("delete-selection-limit")
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
           or not re.fullmatch(r"\d{6,24}", item["id"])
           or not isinstance(item.get("snapshot_round"), str) for item in items):
        raise ValueError("invalid-delete-selection")
    selected = {item["id"]: item["snapshot_round"] for item in items}
    if len(selected) != len(items):
        raise ValueError("invalid-delete-selection")
    from .analysis import sync_game_csv
    # 锁顺序与 handle_ingest 一致；清除入库前缓存也在锁内完成。
    with state.lock, PL.run_lock(state.settings):
        conn = db.connect(state.settings.paths.db)
        try:
            rows = {row["id"]: row for row in latest_rows(conn) if row["id"] in selected}
            if len(rows) != len(selected) or any(
                    row["snapshot_round"] != selected[key] or not missing(row)
                    for key, row in rows.items()):
                raise ValueError("selection-changed-refresh-list")
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        backup = state.settings.paths.db.parent / "backups" / (
            "delete-backfill-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S%f") + ".duckdb")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(state.settings.paths.db, backup)
        counts = {}
        with db.connect(state.settings.paths.db) as conn:
            conn.execute("BEGIN TRANSACTION")
            try:
                # 物化分析表也删除相应行；视图随源表即时更新。
                tables = {r[0] for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main' "
                    "AND table_type='BASE TABLE'").fetchall()}
                for table in ("fct_listing_keyword", "fct_price_change", "fct_delist_event",
                              "fct_listing_revision", "collection_listing_state",
                              "keyword_feature_matrix", "fct_listing_snapshot", "dim_listing"):
                    if table not in tables:
                        continue
                    counts[table] = 0
                    for listing_id in selected:
                        counts[table] += conn.execute(
                            f"SELECT count(*) FROM {table} WHERE listing_id=?", [listing_id]).fetchone()[0]
                        conn.execute(f"DELETE FROM {table} WHERE listing_id=?", [listing_id])
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            for listing_id in selected:
                state.pending_details.pop(listing_id, None)
            purge_results(state, selected)
            results = []
            for game_id in sorted({r["game_id"] for r in rows.values()} - {None}):
                try:
                    result = sync_game_csv(state.settings, conn, game_id=game_id)
                except Exception as exc:
                    result = {"ok": False, "game_id": game_id, "error": str(exc)}
                if not result["ok"]:
                    result["message"] = "数据已删除，游戏表同步失败；关闭占用文件后点击重试同步"
                results.append(result)
    return {"ok": True, "deleted_ids": list(selected), "deleted": len(selected),
            "counts": counts, "backup": str(backup), "results": results}


def apply(conn, entry: dict) -> int:
    """调用者持有项目锁；检查最新轮次、刷新资产组和写入在同一事务中。"""
    conn.execute("BEGIN TRANSACTION")
    try:
        from . import collection as CC
        has_history = conn.execute("SELECT count(*) FROM information_schema.tables"
                                   " WHERE table_name='fct_listing_revision'").fetchone()[0]
        old = next(iter(latest_rows(conn, entry["listing_id"])), None)
        if old is None or old["snapshot_round"] != entry["snapshot_at"].isoformat():
            entry["backfill_result"] = {"error": "latest-snapshot-changed"}
            conn.execute("ROLLBACK")
            return 0
        if has_history:
            CC.record(conn, entry["listing_id"], "baseline", entry["snapshot_at"])
        incoming = dict(entry["features"])
        attributes = dict(entry["attributes"])
        for key in SCALARS:
            if key not in attributes and incoming.get(FALLBACKS[key]) is not None:
                attributes[key] = incoming[FALLBACKS[key]]
            if key in attributes:
                incoming[FALLBACKS[key]] = attributes[key]
        merged = dict(old["features"])
        # 当前数量已改变或已取得新明细：不能留下上一份数量对应的旧资产组。
        for count_key, roster_key, scalar_key in ROSTERS:
            if (count_key in attributes and attributes[count_key] != value(old, count_key)) \
                    or roster_key in incoming or attributes.get(count_key) == 0:
                merged.pop(roster_key, None)
                merged.pop(scalar_key, None)
            if attributes.get(count_key) == 0:
                incoming.pop(roster_key, None)
                incoming.pop(scalar_key, None)
        merged.update(incoming)
        # 没有新值的详情字段/名单仍保留原来源标记，后续短列表不能降级它们。
        merged["_detail_fields"] = sorted(set(attributes) | {
            key for key in SCALARS if key in old["features"].get("_detail_fields", [])})
        merged["_detail_rosters"] = [key for _, key, _ in ROSTERS if key in merged and (
            key in incoming or key in old["features"].get("_detail_rosters", []))]
        merged["_backfilled_at"] = dt.datetime.now().isoformat(timespec="seconds")
        # 先保存清理后的 JSON；update_detail_fields 的普通合并不会删除旧键。
        conn.execute("UPDATE fct_listing_snapshot SET extracted_features=?"
                     " WHERE listing_id=? AND snapshot_at=?",
                     [json.dumps(merged, ensure_ascii=False), old["id"], entry["snapshot_at"]])
        updated = db.update_detail_fields(conn, entry["listing_id"],
            viewers_masked=entry.get("viewers_masked"), favorites_cnt=entry.get("favorites_cnt"),
            attributes=attributes, snapshot_at=entry["snapshot_at"])
        refreshed = old | attributes | {"features": merged}
        reasons = missing(refreshed)
        entry["backfill_result"] = {"complete": not reasons, "missing": reasons}
        if has_history:
            if entry.get("title"):
                conn.execute("UPDATE dim_listing SET title=? WHERE listing_id=?", [entry["title"], entry["listing_id"]])
            entry["revision_id"] = CC.record(conn, entry["listing_id"], "backfill", entry["snapshot_at"])
            CC.complete_retry_state(conn, entry["listing_id"])
        conn.execute("COMMIT")
        return updated
    except Exception:
        conn.execute("ROLLBACK")
        raise


def sync(state, payload: dict) -> dict:
    from .analysis import sync_game_csv
    raw_games = payload.get("game_ids")
    if not isinstance(raw_games, list) or not raw_games:
        raise ValueError("empty-game-ids")
    games = sorted({int(game) for game in raw_games})
    with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
        available = {r[0] for r in conn.execute(
            "SELECT game_id FROM dim_listing UNION SELECT game_id FROM dim_game WHERE biz_prod=1").fetchall()}
        if any(game not in available for game in games):
            raise ValueError("unknown-game-id")
        results = []
        for game in games:
            try:
                result=sync_game_csv(state.settings, conn, game_id=game)
                results.append(result)
                tables={r[0] for r in conn.execute("SHOW TABLES").fetchall()}
                if result.get("ok") and "collection_export_state" in tables:
                    conn.execute("UPDATE collection_export_state SET exported_generation=dirty_generation,error=NULL WHERE game_id=?",[game])
            except Exception as exc:
                results.append({"ok": False, "game_id": game, "error": str(exc)[:300],
                                "message": "数据表同步失败，请检查分析视图或目录后重试"})
    return {"ok": True, "results": results}


# --------------------------------------------------------------------------- #
# 补录结果台账（跨弹窗/看板共享最近补录状态）与任务中转（完整看板发起、扩展领取）。
# 台账持久化在 state_dir/backfill-results.json；中转任务只存网关内存——网关重启
# 即清空，看板可重新发起。所有写入都在 state.lock 内串行。
# --------------------------------------------------------------------------- #
RESULTS_FILE = "backfill-results.json"
RESULT_STATUSES = ("complete", "incomplete", "failed")


def _results_path(state) -> Path:
    return Path(state.settings.paths.state_dir) / RESULTS_FILE


def load_results(state) -> dict:
    try:
        data = json.loads(_results_path(state).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_results(state, data: dict) -> None:
    path = _results_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)   # 不走 Path.replace：CSV 占用测试按后者模拟锁，台账独立于数据表同步


def record_result(state, listing_id: str, *, snapshot_round: str, status: str,
                  error: str | None = None) -> None:
    with state.lock:
        data = load_results(state)
        data[str(listing_id)] = {"snapshot_round": snapshot_round, "status": status,
                                 "error": error or None,
                                 "at": dt.datetime.now().isoformat(timespec="seconds")}
        _save_results(state, data)


def purge_results(state, listing_ids) -> None:
    with state.lock:
        data = load_results(state)
        for listing_id in listing_ids:
            data.pop(str(listing_id), None)
        _save_results(state, data)


def report_result(state, payload: dict) -> dict:
    """扩展队列上报单项补录结果（尽力而为通道；失败由扩展本地记录兜底）。"""
    listing_id = payload.get("id")
    snapshot_round = payload.get("snapshot_round")
    status = payload.get("status")
    if (not isinstance(listing_id, str) or not re.fullmatch(r"\d{6,24}", listing_id)
            or not isinstance(snapshot_round, str) or status not in RESULT_STATUSES):
        raise ValueError("invalid-result")
    error = payload.get("error")
    record_result(state, listing_id, snapshot_round=snapshot_round, status=status,
                  error=str(error)[:120] if error else None)
    return {"ok": True}


def enqueue(state, payload: dict) -> dict:
    """完整看板发起补录：复用 prepare 的校验与备份，任务挂到网关内存等待扩展领取。"""
    prepared = prepare(state, payload)
    with state.lock:
        state.backfill_job = {"status": "pending", "items": prepared["items"],
                              "backup": prepared["backup"], "total": prepared["total"],
                              "created_at": dt.datetime.now().isoformat(timespec="seconds")}
    return {"ok": True, "total": prepared["total"], "backup": prepared["backup"],
            "note": "已排队，等待扩展领取执行（需浏览器与扩展在线）"}


def job_status(state) -> dict:
    with state.lock:
        job = state.backfill_job
        if not job:
            return {"ok": True, "job": None}
        keys = ("status", "total", "backup", "created_at", "error")
        return {"ok": True, "job": {k: job[k] for k in keys if k in job}}


def claim_job(state) -> dict:
    with state.lock:
        job = state.backfill_job
        if not job or job.get("status") != "pending":
            return {"ok": False, "error": "no-pending-job"}
        job["status"] = "claimed"
        return {"ok": True, "items": job["items"], "backup": job["backup"],
                "total": job["total"]}


def finish_job(state, payload: dict) -> dict:
    with state.lock:
        job = state.backfill_job
        if not job:
            return {"ok": False, "error": "no-pending-job"}
        job["status"] = "accepted" if payload.get("ok") else "failed"
        if payload.get("error"):
            job["error"] = str(payload["error"])[:200]
        return {"ok": True}
