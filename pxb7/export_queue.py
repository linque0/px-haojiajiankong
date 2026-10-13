"""游戏级持久导出代次；后台连接独立，CSV错误不影响已提交事实。"""
from __future__ import annotations

import threading
import time

from . import analysis, db, pipeline as PL
from .collection_jobs import now


def dirty(conn, game_id):
    conn.execute("INSERT INTO collection_export_state (game_id,dirty_generation) VALUES (?,1)"
                 " ON CONFLICT(game_id) DO UPDATE SET dirty_generation=collection_export_state.dirty_generation+1",
                 [game_id])


class ExportQueue:
    def __init__(self, settings, *, interval=30):
        self.settings = settings
        self.interval = interval
        self.lock = threading.Lock()
        self.timer = None
        self.closed = False

    def schedule(self):
        with self.lock:
            if self.closed or self.timer is not None:
                return
            self.timer = threading.Timer(self.interval, self._tick)
            self.timer.daemon = True
            self.timer.start()

    def _tick(self):
        try:
            self.flush()
        except Exception:
            pass  # dirty代次仍在库中，下一轮本地重试；不重采站点。
        finally:
            with self.lock:
                self.timer = None
            if self.pending():
                self.schedule()

    def pending(self):
        try:
            with PL.run_lock(self.settings), db.connect(self.settings.paths.db) as conn:
                return bool(conn.execute("SELECT count(*) FROM collection_export_state WHERE dirty_generation>exported_generation").fetchone()[0])
        except Exception:
            return True

    def flush(self, *, game_id=None):
        started = time.perf_counter()
        results = []
        # 不持有timer.lock或网关state.lock。run_lock保证查询/写文件/确认代次与入库互斥。
        with PL.run_lock(self.settings), db.connect(self.settings.paths.db) as conn:
            rows = conn.execute("SELECT game_id,dirty_generation FROM collection_export_state"
                                " WHERE dirty_generation>exported_generation AND (? IS NULL OR game_id=?)", [game_id, game_id]).fetchall()
            for gid, generation in rows:
                try:
                    result = analysis.sync_game_csv(self.settings, conn, game_id=gid)
                except Exception as exc:
                    result = {"ok": False, "game_id": gid, "error": type(exc).__name__}
                if result["ok"]:
                    conn.execute("UPDATE collection_export_state SET exported_generation=?,last_export_at=?,error=NULL WHERE game_id=?",
                                 [generation, now(), gid])
                else:
                    conn.execute("UPDATE collection_export_state SET error=? WHERE game_id=?", [str(result.get("error", "export-failed"))[:300], gid])
                results.append({**result, "generation": generation})
        return {"ok": all(r["ok"] for r in results), "results": results,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}

    def close(self):
        with self.lock:
            self.closed = True
            if self.timer:
                self.timer.cancel()
        try:
            return self.flush()
        except Exception as exc:
            return {"ok": False, "pending": True, "error": type(exc).__name__}
