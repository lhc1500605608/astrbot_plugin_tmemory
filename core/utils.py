import time
import json
from typing import Dict, Optional
import sqlite3

class MemoryLogger:
    def __init__(self, db_manager):
        self._db_mgr = db_manager

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def log_memory_event(
        self,
        canonical_user_id: str,
        event_type: str,
        payload: Dict[str, object],
        conn: Optional[sqlite3.Connection] = None,
    ):
        row = (
            canonical_user_id,
            event_type,
            json.dumps(payload, ensure_ascii=False),
            self._now(),
        )
        sql = (
            "INSERT INTO memory_events(canonical_user_id, event_type, payload_json, created_at)"
            " VALUES(?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, row)
        else:
            with self._db_mgr.db() as db_conn:
                db_conn.execute(sql, row)
