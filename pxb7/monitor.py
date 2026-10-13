"""显式启用的任务定时队列；浏览器离线合并错过轮次，不启动第二个采集器。"""
from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from urllib.parse import urlsplit

from . import collection_jobs as J, db, pipeline as PL


def validate(raw, *, game_id, biz_prod):
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("enabled", False), bool):
        raise ValueError("monitor必须是映射，enabled必须是布尔值")
    if not raw.get("enabled"):
        return {"enabled": False}
    spec = dict(raw)
    # 沿用每任务每日最多4轮的现有档位；不随新执行器放松。
    J.positive(spec.get("interval_minutes"), "monitor-interval", 10080, 360)
    J.positive(spec.get("target_count"), "monitor-target", J.MAX_TARGET)
    J.positive(spec.get("scan_budget"), "monitor-scan-budget", 100000)
    J.positive(spec.get("daily_access_budget"), "monitor-access-budget", 100000)
    J.positive(spec.get("max_scan_rounds", 12), "monitor-rounds", 1000)
    if spec.get("purpose") not in J.PURPOSES:
        raise ValueError("monitor.purpose无效")
    ids=spec.get("ids",[])
    if not isinstance(ids,list) or len(ids)>J.MAX_TARGET or any(not isinstance(i,str) or not J.C.ID_RE.fullmatch(i) for i in ids):
        raise ValueError("monitor.ids必须是商品字符串ID数组")
    if not isinstance(spec.get("source_url"),str):
        raise ValueError("monitor.source_url必须是列表地址字符串")
    url = urlsplit(spec["source_url"])
    if url.scheme != "https" or url.netloc != "www.pxb7.com" or url.path.rstrip("/") not in (
            f"/buy/{game_id}", f"/buy/{game_id}/{biz_prod}"):
        raise ValueError("monitor.source_url必须是该任务的真实列表地址")
    if any(word in url.query.lower() for word in ("token", "cookie", "authorization", "captcha", "password", "timestamp__")):
        raise ValueError("monitor.source_url不能包含认证或验证参数")
    return spec


class Monitor:
    def __init__(self, state):
        self.state = state
        self.timer = None
        self.closed = False

    def tick(self, *, clock=None, online=False):
        at = clock or J.now()
        state = self.state
        with state.lock, PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
            for task in state.tasks:
                spec = task.monitor
                if not task.enabled or not spec or not spec.get("enabled"):
                    continue
                interval = spec["interval_minutes"] * 60
                slot = int(at.timestamp()) // interval
                latest = conn.execute("SELECT max(slot) FROM monitor_job WHERE task_id=?", [task.task_id]).fetchone()[0]
                if latest is not None and slot <= latest:
                    continue
                conn.execute("UPDATE monitor_job SET status='missed',reason='newer-slot-coalesced' WHERE task_id=? AND status IN ('queued','deferred')", [task.task_id])
                conn.execute("INSERT INTO monitor_job VALUES (?,?,?,?,?,?,?,?,?)", [uuid.uuid4().hex,task.task_id,slot,
                    dt.datetime.fromtimestamp(slot*interval,dt.timezone.utc),"queued" if online else "deferred",
                    None,"" if online else "browser-offline",json.dumps(spec,ensure_ascii=False),max(0,slot-latest-1) if latest is not None else 0])

    def claim(self):
        self.tick(online=True)
        state = self.state
        with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
            J.reconcile_expired(conn)
            if J.hold(conn):
                conn.execute("UPDATE monitor_job SET status='deferred',reason='site-access-held' WHERE status IN ('queued','deferred')")
                return {"ok":False,"error":"site-access-held"}
            if conn.execute("SELECT count(*) FROM collection_batch WHERE status IN ('running','waiting_verification','needs_reconcile')").fetchone()[0]:
                return {"ok":False,"error":"batch-busy"}
            pending=conn.execute("SELECT job_id,task_id,spec_json FROM monitor_job WHERE status IN ('queued','deferred') AND due_at<=? ORDER BY due_at LIMIT 1",[J.now()]).fetchone()
        if not pending:
            return {"ok":False,"error":"no-pending-job"}
        spec=json.loads(pending[2])
        task=state.task_by_id(pending[1])
        if not task or not task.enabled or not task.monitor or not task.monitor.get("enabled"):
            with PL.run_lock(state.settings),db.connect(state.settings.paths.db) as conn:
                conn.execute("UPDATE monitor_job SET status='missed',reason='task-disabled' WHERE job_id=?",[pending[0]])
            return {"ok":False,"error":"task-disabled"}
        batch=J.create(state,{"url":spec["source_url"],"purpose":spec["purpose"],"target_count":spec["target_count"],
            "collection_mode":"auto","ids":spec.get("ids",[]),"scan_budget":spec["scan_budget"],"max_scan_rounds":spec.get("max_scan_rounds",12)},
            override={"daily_access_budget":spec["daily_access_budget"],"collection_mode":"auto"})
        with PL.run_lock(state.settings), db.connect(state.settings.paths.db) as conn:
            conn.execute("UPDATE monitor_job SET status='claimed',batch_id=?,reason='' WHERE job_id=?",[batch["batch_id"],pending[0]])
        return {**batch,"job_id":pending[0],"source_url":spec["source_url"]}

    def start(self):
        if not any(t.monitor and t.monitor.get("enabled") for t in self.state.tasks):
            return
        def tick():
            if self.closed:
                return
            try:
                seen = self.state.extension_seen_at
                online = bool(seen and (dt.datetime.now()-dt.datetime.fromisoformat(seen)).total_seconds()<180)
                self.tick(online=online)
            except Exception:
                pass  # 下次tick重试；不访问站点。
            if not self.closed:
                self.timer=threading.Timer(30,tick);self.timer.daemon=True;self.timer.start()
        tick()

    def close(self):
        self.closed=True
        if self.timer:
            self.timer.cancel()
