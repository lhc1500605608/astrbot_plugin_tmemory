"""Versioned profile/event storage and the legacy SQL compatibility projection.

``memories`` becomes a view at cutover, never a second writable source of truth.
Its numeric IDs are stable aliases; public IDs identify the actual objects.
"""
import hashlib
import json
import re

from .data_safety import quarantine, review_reason

EXTRA = {
    "source_adapter": "TEXT NOT NULL DEFAULT ''",
    "platform_id": "TEXT NOT NULL DEFAULT ''",
    "source_user_id": "TEXT NOT NULL DEFAULT ''",
    "source_channel": "TEXT NOT NULL DEFAULT 'default'",
    "memory_type": "TEXT NOT NULL DEFAULT 'fact'",
    "memory_hash": "TEXT NOT NULL DEFAULT ''",
    "tokenized_memory": "TEXT NOT NULL DEFAULT ''",
    "score": "REAL NOT NULL DEFAULT 0.5",
    "reinforce_count": "INTEGER NOT NULL DEFAULT 0",
    "attention_score": "REAL NOT NULL DEFAULT 0.5",
    "last_seen_at": "TEXT NOT NULL DEFAULT ''",
    "is_active": "INTEGER NOT NULL DEFAULT 1",
    "is_pinned": "INTEGER NOT NULL DEFAULT 0",
    "event_date": "TEXT NOT NULL DEFAULT ''",
    "valid_until": "TEXT NOT NULL DEFAULT ''",
    "episode_id": "INTEGER NOT NULL DEFAULT 0",
    "derived_from": "TEXT NOT NULL DEFAULT 'direct'",
    "evidence_json": "TEXT NOT NULL DEFAULT ''",
    "semantic_status": "TEXT NOT NULL DEFAULT 'active'",
    "contradiction_of": "INTEGER NOT NULL DEFAULT 0",
    "review_status": "TEXT NOT NULL DEFAULT 'approved'",
}


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS storage_migrations(version INTEGER PRIMARY KEY,report TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS memory_record_ids(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 "canonical_user_id TEXT NOT NULL,kind TEXT NOT NULL,object_id INTEGER NOT NULL,legacy_id INTEGER UNIQUE,UNIQUE(kind,object_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS legacy_record_map(legacy_id INTEGER PRIMARY KEY,canonical_user_id TEXT NOT NULL,kind TEXT NOT NULL,object_id INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS import_object_map(canonical_user_id TEXT NOT NULL,"
                 "table_name TEXT NOT NULL,source_key TEXT NOT NULL,target_id INTEGER NOT NULL,"
                 "PRIMARY KEY(canonical_user_id,table_name,source_key))")
    conn.execute("CREATE TABLE IF NOT EXISTS event_items(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 "canonical_user_id TEXT NOT NULL,content TEXT NOT NULL,normalized_content TEXT NOT NULL,"
                 "source_scope TEXT NOT NULL DEFAULT 'user',persona_id TEXT NOT NULL DEFAULT '',"
                 "status TEXT NOT NULL DEFAULT 'active',task_status TEXT NOT NULL DEFAULT 'pending',"
                 "confidence REAL NOT NULL DEFAULT 0.5,importance REAL NOT NULL DEFAULT 0.5,"
                 "created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
                 "UNIQUE(canonical_user_id,normalized_content,source_scope,persona_id))")
    for table in ("profile_items", "event_items"):
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in EXTRA.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.execute("CREATE TABLE IF NOT EXISTS event_item_evidence(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 "event_item_id INTEGER NOT NULL,conversation_cache_id INTEGER NOT NULL DEFAULT 0,"
                 "canonical_user_id TEXT NOT NULL,source_excerpt TEXT NOT NULL DEFAULT '',"
                 "created_at TEXT NOT NULL,UNIQUE(event_item_id,conversation_cache_id))")


def normalized(text):
    return re.sub(r"\s+", " ", str(text).strip()).lower()


def migrate_legacy(conn, now):
    ensure_schema(conn)
    if conn.execute("SELECT 1 FROM storage_migrations WHERE version=18").fetchone():
        return json.loads(conn.execute("SELECT report FROM storage_migrations WHERE version=18").fetchone()[0])
    report = {"profiles": 0, "events": 0, "quarantined": 0}
    rows = [dict(r) for r in conn.execute("SELECT * FROM memories ORDER BY id")]
    # Reserve old numeric IDs before allocating aliases for pre-existing profiles.
    max_id = max((r["id"] for r in rows), default=0)
    conn.execute("INSERT INTO sqlite_sequence(name,seq) SELECT 'memory_record_ids',? "
                 "WHERE NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='memory_record_ids')", (max_id,))
    conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='memory_record_ids'", (max_id,))
    for table, kind in (("profile_items", "p"), ("event_items", "e")):
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            r = dict(row)
            reason = review_reason(r["content"])
            if r["source_scope"] == "user" or not r.get("platform_id"):
                reason = reason or "ambiguous_provenance"
            if reason:
                quarantine(conn, r["canonical_user_id"], r["content"], reason, now, r["source_scope"], r["persona_id"])
                content = "[credential redacted]" if reason == "credential" else r["content"]
                conn.execute(f"UPDATE {table} SET review_status='pending',status='archived',content=? WHERE id=?", (content, r["id"]))
                report["quarantined"] += 1
            conn.execute("INSERT OR IGNORE INTO memory_record_ids(canonical_user_id,kind,object_id) VALUES(?,?,?)", (r["canonical_user_id"], kind, r["id"]))
    for row in rows:
        uid, text = row["canonical_user_id"], row["memory"]
        scope = row.get("scope", "user")
        adapter = row.get("source_adapter", "")
        reason = review_reason(text)
        if scope == "user" or adapter in ("", "unknown", "system", "webui", "import"):
            reason = reason or "ambiguous_provenance"
        # Legacy group/private scopes do not encode platform instance identity.
        # Keep them pending unless an exact source event establishes the boundary.
        source = conn.execute("SELECT DISTINCT unified_msg_origin FROM conversation_cache WHERE canonical_user_id=? "
                              "AND scope=? AND persona_id=? AND source_adapter=? AND unified_msg_origin!='' LIMIT 2",
                              (uid, scope, row.get("persona_id", ""), adapter)).fetchall()
        origins = {r[0] for r in source}
        if len(origins) == 1:
            platform = next(iter(origins)).split(":", 1)[0]
            scope = f"{platform}|{scope}"
        else:
            platform = ""
            reason = reason or "ambiguous_provenance"
        if reason:
            quarantine(conn, uid, text, reason, now, scope, row.get("persona_id", ""))
            report["quarantined"] += 1
        content = "[credential redacted]" if reason == "credential" else text
        kind = "e" if row.get("memory_type") in ("event", "task") else "p"
        table = "event_items" if kind == "e" else "profile_items"
        record = {key: row.get(key) for key in EXTRA if key in row}
        record.update(canonical_user_id=uid, content=content, normalized_content=normalized(content),
                      source_scope=scope, persona_id=row.get("persona_id", ""), platform_id=platform,
                      status="archived" if reason or not row.get("is_active", 1) else "active",
                      review_status="pending" if reason else "approved", created_at=row["created_at"], updated_at=row["updated_at"],
                      confidence=row.get("confidence", .5), importance=row.get("importance", .5))
        if kind == "p":
            record["facet_type"] = row["memory_type"] if row["memory_type"] in ("fact", "preference", "style", "restriction", "task_pattern") else "fact"
        cols = list(record)
        conn.execute(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", tuple(record.values()))
        object_id = conn.execute(f"SELECT id FROM {table} WHERE canonical_user_id=? AND normalized_content=? AND source_scope=? AND persona_id=?",
                                 (uid, normalized(content), scope, row.get("persona_id", ""))).fetchone()[0]
        previous = conn.execute("SELECT id FROM memory_record_ids WHERE kind=? AND object_id=?", (kind, object_id)).fetchone()
        if not previous:
            conn.execute("INSERT INTO memory_record_ids(id,canonical_user_id,kind,object_id,legacy_id) VALUES(?,?,?,?,?)", (row["id"], uid, kind, object_id, row["id"]))
        conn.execute("INSERT INTO legacy_record_map VALUES(?,?,?,?)", (row["id"], uid, kind, object_id))
        report["events" if kind == "e" else "profiles"] += 1
    conn.execute("INSERT INTO storage_migrations VALUES(18,?)", (json.dumps(report),))
    return report


def resolve_id(conn, value):
    text = str(value)
    if text.isdigit():
        mapped = conn.execute("SELECT r.id FROM legacy_record_map l JOIN memory_record_ids r ON l.kind=r.kind AND l.object_id=r.object_id WHERE l.legacy_id=?", (int(text),)).fetchone()
        return int(mapped[0]) if mapped else int(text)
    if re.fullmatch(r"[pe]:\d+", text):
        row = conn.execute("SELECT id FROM memory_record_ids WHERE kind=? AND object_id=?", (text[0], int(text[2:]))).fetchone()
        return int(row[0]) if row else 0
    return 0
