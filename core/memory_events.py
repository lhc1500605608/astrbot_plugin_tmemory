"""Standalone memory audit-event helper.

Extracted from ``core/memory_ops.py`` per ADR-009 (module boundary split).
Physical move only — no behavior change.
"""

import json
from typing import Dict


def log_memory_event(
    plugin,
    canonical_user_id: str,
    event_type: str,
    payload: Dict[str, object],
    conn=None,
):
    """记录记忆相关事件到审计日志 memory_events。"""
    row = (
        canonical_user_id,
        event_type,
        json.dumps(payload, ensure_ascii=False),
        plugin._now(),
    )
    sql = (
        "INSERT INTO memory_events(canonical_user_id, event_type, payload_json, created_at)"
        " VALUES(?, ?, ?, ?)"
    )
    if conn is not None:
        conn.execute(sql, row)
    else:
        with plugin._db() as _conn:
            _conn.execute(sql, row)
