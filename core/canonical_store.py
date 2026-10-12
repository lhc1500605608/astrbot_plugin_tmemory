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
    from .runtime_reliability import ensure_schema as runtime_schema
    runtime_schema(conn)
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


def is_active(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='memories'").fetchone())


def install_functions(conn):
    import jieba
    from .utils_shared import _normalize_text
    conn.create_function("tm_normalize", 1, normalized)
    conn.create_function("tm_tokens", 1, lambda t: " ".join(jieba.cut_for_search(str(t or "")) ))
    conn.create_function("tm_hash", 3, lambda p, s, t: hashlib.sha256(f"{p or ''}:{s or 'user'}:{_normalize_text(str(t or ''))}".encode()).hexdigest())
    conn.create_function("tm_unsafe", 1, review_reason)
    conn.create_function("tm_platform", 1, lambda s: str(s).split("|", 1)[0] if "|" in str(s) else "")


def init_search(conn):
    """FTS external content can read a view; triggers live on canonical tables."""
    from .db_schema import (_DDL_MEMORY_FTS, _profile_items_fts_ddl, _memory_episodes_fts_ddl,
                            _PROFILE_ITEMS_FTS_TRIGGERS, _MEMORY_EPISODES_FTS_TRIGGERS,
                            _select_text_fts_tokenizer)
    tokenizer = _select_text_fts_tokenizer(conn)
    if tokenizer is None:
        return False
    conn.execute(_DDL_MEMORY_FTS)
    for table, kind in (("profile_items", "p"), ("event_items", "e")):
        for suffix in ("insert", "update", "delete"):
            trigger = f"canonical_fts_{kind}_{suffix}"
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            body = []
            if suffix != "insert":
                body.append(f"INSERT INTO memories_fts(memories_fts,rowid,tokenized_memory,canonical_user_id) SELECT 'delete',id,tm_tokens(old.content),old.canonical_user_id FROM memory_record_ids WHERE kind='{kind}' AND object_id=old.id;")
            if suffix != "delete":
                body.append(f"INSERT INTO memories_fts(rowid,tokenized_memory,canonical_user_id) SELECT id,tm_tokens(new.content),new.canonical_user_id FROM memory_record_ids WHERE kind='{kind}' AND object_id=new.id;")
            # Mapping is created by the BEFORE-installed alias trigger. For insert,
            # that trigger performs indexing itself, avoiding trigger ordering reliance.
            if suffix == "insert":
                continue
            conn.execute(f"CREATE TRIGGER {trigger} AFTER {suffix.upper()} ON {table} BEGIN {' '.join(body)} END")
        conn.execute(f"DROP TRIGGER IF EXISTS canonical_alias_{kind}")
        conn.execute(f"CREATE TRIGGER canonical_alias_{kind} AFTER INSERT ON {table} BEGIN "
                     f"INSERT OR IGNORE INTO memory_record_ids(canonical_user_id,kind,object_id) VALUES(new.canonical_user_id,'{kind}',new.id);"
                     f"INSERT INTO memories_fts(rowid,tokenized_memory,canonical_user_id) SELECT id,tm_tokens(new.content),new.canonical_user_id FROM memory_record_ids WHERE kind='{kind}' AND object_id=new.id; END")
    for ddl, triggers, table in (
        (_profile_items_fts_ddl(tokenizer), _PROFILE_ITEMS_FTS_TRIGGERS, "profile_items_fts"),
        (_memory_episodes_fts_ddl(tokenizer), _MEMORY_EPISODES_FTS_TRIGGERS, "memory_episodes_fts"),
    ):
        conn.execute(ddl)
        for trigger in triggers:
            conn.execute(trigger)
        conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    return True


def activate(conn, now):
    ensure_schema(conn)
    install_functions(conn)
    if is_active(conn):
        return
    migrate_legacy(conn, now)
    for name in ("t_memories_ai", "t_memories_ad", "t_memories_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DROP TABLE IF EXISTS memories_fts")
    conn.execute("DROP TABLE memories")
    selects = []
    for table, kind in (("profile_items", "p"), ("event_items", "e")):
        memory_type = "t.facet_type" if kind == "p" else "t.memory_type"
        cols = ["r.id", "t.canonical_user_id", "t.source_adapter", "t.source_user_id", "t.source_channel",
                f"{memory_type} AS memory_type", "t.content AS memory", "tm_tokens(t.content) AS tokenized_memory",
                "tm_hash(t.persona_id,t.source_scope,t.content) AS memory_hash", "t.score", "t.importance", "t.confidence",
                "t.reinforce_count", "t.last_seen_at", "t.created_at", "t.updated_at",
                "CASE WHEN t.status='active' AND t.is_active=1 AND t.review_status='approved' THEN 1 ELSE 0 END AS is_active",
                "t.is_pinned", "t.persona_id", "t.source_scope AS scope",
                f"CASE WHEN {memory_type}='style' THEN 'persona' ELSE 'canonical' END AS summary_channel",
                "t.attention_score", "t.episode_id", "t.derived_from", "t.evidence_json", "t.semantic_status",
                "t.contradiction_of", "t.event_date", "t.valid_until", f"'{kind}:' || t.id AS public_id", "t.platform_id", "t.review_status"]
        selects.append(f"SELECT {','.join(cols)} FROM {table} t JOIN memory_record_ids r ON r.kind='{kind}' AND r.object_id=t.id")
    conn.execute("CREATE VIEW memories AS " + " UNION ALL ".join(selects))
    # INSERT/UPDATE/DELETE compatibility is a projection onto the two canonical stores.
    parts = ["INSERT OR IGNORE INTO user_profiles(canonical_user_id,created_at,updated_at) VALUES(new.canonical_user_id,COALESCE(new.created_at,datetime('now')),COALESCE(new.updated_at,datetime('now')));", "SELECT CASE WHEN tm_unsafe(new.memory)!='' THEN RAISE(ABORT,'unsafe memory content') END;"]
    for table, kind in (("profile_items", "p"), ("event_items", "e")):
        condition = "new.memory_type IN ('event','task')" if kind == "e" else "COALESCE(new.memory_type,'fact') NOT IN ('event','task')"
        expressions = {k: f"COALESCE(new.{k},{ddl.split('DEFAULT ',1)[1]})" for k, ddl in EXTRA.items() if k not in ("platform_id", "review_status")}
        expressions.update(canonical_user_id="new.canonical_user_id", content="new.memory", normalized_content="tm_normalize(new.memory)",
                           source_scope="COALESCE(new.scope,'user')", persona_id="COALESCE(new.persona_id,'')",
                           platform_id="tm_platform(new.scope)", status="CASE WHEN COALESCE(new.is_active,1)=1 THEN 'active' ELSE 'archived' END",
                           review_status="CASE WHEN tm_platform(new.scope)='' AND COALESCE(new.scope,'user')!='global' THEN 'pending' ELSE 'approved' END",
                           created_at="COALESCE(new.created_at,datetime('now'))", updated_at="COALESCE(new.updated_at,datetime('now'))",
                           confidence="COALESCE(new.confidence,0.5)", importance="COALESCE(new.importance,0.5)")
        if kind == "p":
            expressions["facet_type"] = "CASE WHEN new.memory_type IN ('preference','fact','style','restriction','task_pattern') THEN new.memory_type ELSE 'fact' END"
        parts.append(f"INSERT INTO {table}({','.join(expressions)}) SELECT {','.join(expressions.values())} WHERE {condition};")
    conn.execute("CREATE TRIGGER canonical_insert INSTEAD OF INSERT ON memories BEGIN " + " ".join(parts) + " END")
    updates, deletes = [], []
    for table, kind in (("profile_items", "p"), ("event_items", "e")):
        assignments = {k: f"new.{k}" for k in EXTRA if k not in ("platform_id", "review_status")}
        assignments.update(content="new.memory", normalized_content="tm_normalize(new.memory)", canonical_user_id="new.canonical_user_id",
                           source_scope="new.scope", persona_id="new.persona_id", confidence="new.confidence", importance="new.importance",
                           updated_at="new.updated_at", status="CASE WHEN new.is_active=0 THEN 'archived' ELSE 'active' END")
        if kind == "p":
            assignments["facet_type"] = "CASE WHEN new.memory_type IN ('preference','fact','style','restriction','task_pattern') THEN new.memory_type ELSE facet_type END"
        where = f"id IN (SELECT object_id FROM memory_record_ids WHERE id=old.id AND kind='{kind}')"
        updates.append(f"UPDATE {table} SET {','.join(f'{k}={v}' for k,v in assignments.items())} WHERE {where};")
        for evidence, column in (("profile_item_evidence", "profile_item_id"),) if kind == "p" else (("event_item_evidence", "event_item_id"),):
            deletes.append(f"DELETE FROM {evidence} WHERE {column} IN (SELECT object_id FROM memory_record_ids WHERE id=old.id AND kind='{kind}');")
        if kind == "p":
            deletes.append("DELETE FROM profile_relations WHERE from_item_id IN (SELECT object_id FROM memory_record_ids WHERE id=old.id AND kind='p') OR to_item_id IN (SELECT object_id FROM memory_record_ids WHERE id=old.id AND kind='p');")
        deletes.append(f"DELETE FROM {table} WHERE {where};")
    updates.append("UPDATE memory_record_ids SET canonical_user_id=new.canonical_user_id WHERE id=old.id;")
    deletes.append("DELETE FROM memory_record_ids WHERE id=old.id;")
    conn.execute("CREATE TRIGGER canonical_update INSTEAD OF UPDATE ON memories BEGIN SELECT CASE WHEN tm_unsafe(new.memory)!='' AND new.memory!=old.memory THEN RAISE(ABORT,'unsafe memory content') END; " + " ".join(updates) + " END")
    conn.execute("CREATE TRIGGER canonical_delete INSTEAD OF DELETE ON memories BEGIN " + " ".join(deletes) + " END")
    # Alias registration works even without FTS5.
    for table, kind in (("profile_items", "p"), ("event_items", "e")):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS canonical_alias_{kind} AFTER INSERT ON {table} BEGIN INSERT OR IGNORE INTO memory_record_ids(canonical_user_id,kind,object_id) VALUES(new.canonical_user_id,'{kind}',new.id); END")
    init_search(conn)
