"""Transactional, versioned user graph export/import; embeddings are rebuildable."""
import hashlib
import json

from .data_safety import review_reason

TABLES = (
    "legacy_memories", "user_profiles", "memory_episodes", "conversation_cache", "profile_items", "event_items",
    "memory_record_ids", "legacy_record_map", "profile_item_evidence", "event_item_evidence",
    "profile_relations", "episode_sources", "identity_bindings", "proactive_user_policy",
    "proactive_reminders", "proactive_send_log", "memory_quarantine", "memory_events",
)
REFERENCES = {
    "profile_item_id": "profile_items", "event_item_id": "event_items",
    "conversation_cache_id": "conversation_cache", "episode_id": "memory_episodes",
    "from_item_id": "profile_items", "to_item_id": "profile_items",
}


def export_user(conn, uid):
    tables = {}
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='memories' AND type='table'").fetchone():
        tables["legacy_memories"] = [dict(r) for r in conn.execute("SELECT * FROM memories WHERE canonical_user_id=?", (uid,))]
    for table in TABLES:
        if table not in existing:
            continue
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE canonical_user_id=?", (uid,))]
        for row in rows:
            for key, value in row.items():
                if isinstance(value, str) and review_reason(value) == "credential":
                    row[key] = "[credential redacted]"
        tables[table] = rows
    return tables


def fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def existing_object(conn, uid, table, row, key):
    mapped = conn.execute("SELECT target_id FROM import_object_map WHERE canonical_user_id=? AND table_name=? AND source_key=?", (uid, table, key)).fetchone()
    if mapped:
        return mapped[0]
    actual = "memories" if table == "legacy_memories" else table
    if "id" in row:
        current = conn.execute(f"SELECT * FROM {actual} WHERE canonical_user_id=? AND id=?", (uid, row["id"])).fetchone()
        if current and all(dict(current).get(k) == v for k, v in row.items()):
            return row["id"]
    return None


def plan_import(plugin, payload):
    errors, records, conflicts = [], [], 0
    if payload.get("schema_version") != 2 or not isinstance(payload.get("users"), list):
        errors.append({"reason": "invalid v2 envelope"})
    else:
        for user in payload["users"]:
            uid = user.get("canonical_user_id") if isinstance(user, dict) else None
            if not isinstance(uid, str) or not uid or not isinstance(user.get("tables"), dict):
                errors.append({"reason": "invalid user graph"})
                continue
            unknown = set(user["tables"]) - set(TABLES)
            if unknown:
                errors.append({"reason": "unknown graph tables"})
            for table in TABLES:
                rows = user["tables"].get(table, [])
                if not isinstance(rows, list):
                    errors.append({"reason": f"invalid {table} rows"})
                    continue
                for row in rows:
                    if not isinstance(row, dict) or row.get("canonical_user_id") != uid:
                        errors.append({"reason": f"invalid {table} owner"})
                        continue
                    if table in ("profile_items", "event_items") and row.get("review_status", "approved") == "approved" and review_reason(str(row.get("content", ""))):
                        errors.append({"reason": "unsafe active memory"})
                        continue
                    records.append((uid, table, dict(row), fingerprint(row)))
    seen, unique, duplicates = set(), [], 0
    with plugin._db() as conn:
        for record in records:
            uid, table, row, key = record
            identity = (uid, table, key)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            unique.append(record)
            if existing_object(conn, uid, table, row, key) is not None:
                conflicts += 1
    return dict(schema_version=2, valid=len(unique), invalid=len(errors), to_insert=len(unique)-conflicts,
                conflicts=conflicts, duplicates_in_payload=duplicates, errors=errors[:50], error_count=len(errors),
                preview={"new": [], "conflicts": []}, _records=unique,
                _insert_users=sorted({r[0] for r in unique}))


def import_dataset(plugin, payload, dry_run=True, on_conflict="skip", backup=True):
    from .portability import write_backup
    if on_conflict not in ("skip", "error"):
        raise ValueError("on_conflict must be skip or error")
    plan = plan_import(plugin, payload)
    result = {k: v for k, v in plan.items() if not k.startswith("_")}
    result.update(ok=False, applied=False, dry_run=dry_run, inserted=0, backup_path=None, rolled_back=False)
    if plan["invalid"]:
        return result
    if on_conflict == "error" and (plan["conflicts"] or plan["duplicates_in_payload"]):
        result["error"] = "conflict detected"
        return result
    if dry_run:
        result["ok"] = True
        return result
    if backup:
        result["backup_path"] = write_backup(plugin, plan["_insert_users"])
    maps = {}
    try:
        with plugin._db() as conn:
            for uid, table, original, key in plan["_records"]:
                previous = existing_object(conn, uid, table, original, key)
                source_id = original.get("id", original.get("legacy_id", 0))
                if previous is not None:
                    maps[(uid, table, source_id)] = previous
                    continue
                row = dict(original)
                row.pop("id", None)
                if table == "legacy_memories":
                    from .portability import _validate_memory, _insert_record, _memory_hash
                    validated, error = _validate_memory(original, uid, 0)
                    if error:
                        raise ValueError(error)
                    _insert_record(conn, validated, plugin._now())
                    target_id = conn.execute("SELECT id FROM memories WHERE canonical_user_id=? AND memory_hash=?", (uid, _memory_hash(validated["persona_id"],validated["scope"],validated["memory"]))).fetchone()[0]
                elif table == "memory_record_ids":
                    kind = row["kind"]
                    parent = "profile_items" if kind == "p" else "event_items" if kind == "e" else None
                    target = maps.get((uid, parent, row["object_id"]))
                    if target is None:
                        raise ValueError("missing memory object reference")
                    conn.execute("INSERT OR IGNORE INTO memory_record_ids(canonical_user_id,kind,object_id) VALUES(?,?,?)", (uid, kind, target))
                    target_id = conn.execute("SELECT id FROM memory_record_ids WHERE kind=? AND object_id=?", (kind, target)).fetchone()[0]
                elif table == "legacy_record_map":
                    # Imported numeric aliases are source-local; do not steal existing IDs.
                    continue
                else:
                    for column, parent in REFERENCES.items():
                        if column in row and row[column]:
                            target = maps.get((uid, parent, row[column]))
                            if target is None:
                                raise ValueError(f"missing {column} reference")
                            row[column] = target
                    if row.get("contradiction_of"):
                        row["contradiction_of"] = maps.get((uid, "memory_record_ids", row["contradiction_of"]), 0)
                    if table == "proactive_reminders":
                        row["status"], row["sent_at"] = "paused", ""
                    if table == "proactive_user_policy":
                        row["opt_in"] = 0
                    if table == "conversation_cache":
                        # Imported history is evidence, never an automatic extraction queue.
                        row["distilled"] = 1
                        if "processing_status" in row:
                            row["processing_status"] = "success"
                    if table in ("profile_items", "event_items"):
                        row["normalized_content"] = " ".join(row["content"].split()).lower()
                        if "embedding_status" in row:
                            row["embedding_status"] = "pending"
                    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if set(row) - columns:
                        raise ValueError(f"unknown {table} fields")
                    names = list(row)
                    cur = conn.execute(f"INSERT OR IGNORE INTO {table}({','.join(names)}) VALUES({','.join('?' for _ in names)})", tuple(row.values()))
                    target_id = cur.lastrowid
                    if cur.rowcount == 0:
                        if table in ("profile_items", "event_items"):
                            target_id = conn.execute(f"SELECT id FROM {table} WHERE canonical_user_id=? AND normalized_content=? AND source_scope=? AND persona_id=?", (uid, row["normalized_content"], row["source_scope"], row["persona_id"])).fetchone()[0]
                        elif "id" in columns:
                            # Existing identity bindings must not be reassigned across users.
                            if table == "identity_bindings":
                                existing = conn.execute("SELECT id,canonical_user_id FROM identity_bindings WHERE adapter=? AND adapter_user_id=?", (row["adapter"], row["adapter_user_id"])).fetchone()
                                if not existing or existing["canonical_user_id"] != uid:
                                    raise ValueError("identity binding belongs to another user")
                                target_id = existing["id"]
                            else:
                                raise ValueError(f"conflicting {table} record")
                        else:
                            target_id = 0
                maps[(uid, table, source_id)] = target_id
                conn.execute("INSERT INTO import_object_map VALUES(?,?,?,?)", (uid, table, key, target_id or 0))
                result["inserted"] += 1
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}", rolled_back=True, inserted=0)
        return result
    result.update(ok=True, applied=True)
    return result
