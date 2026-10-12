"""Shared write boundaries and deletion barriers for all memory pipelines."""
from contextlib import contextmanager
from contextvars import ContextVar
import re

from .distill_validator import is_unsafe_memory
from .distill_validator import CREDENTIAL_PATTERNS

_generation = ContextVar("tmemory_user_generation", default=None)
_credentials = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY-----|"
    r"(?:api[_ -]?key|access[_ -]?token|password|密码|密钥)\s*[:=：]\s*\S+",
    re.I,
)


def review_reason(text):
    if _credentials.search(str(text)) or any(p.search(str(text)) for p in CREDENTIAL_PATTERNS):
        return "credential"
    return "unsafe_content" if is_unsafe_memory(str(text)) else ""


def require_safe(text):
    if review_reason(text):
        raise ValueError("unsafe memory content: review required")


def quarantine(conn, uid, text, reason, now, scope="user", persona=""):
    # Credentials must not be retained as reviewable plaintext.
    safe_text = "[credential redacted]" if reason == "credential" else str(text)
    conn.execute(
        "INSERT INTO memory_quarantine(canonical_user_id,content,reason,scope,persona_id,created_at) "
        "VALUES(?,?,?,?,?,?)", (uid, safe_text, reason, scope, persona, now),
    )


def batch_key(row):
    return tuple(str(row.get(k, "")) for k in (
        "canonical_user_id", "source_adapter", "scope", "persona_id", "unified_msg_origin"
    ))


def first_isolated_batch(rows):
    if not rows:
        return rows
    key = batch_key(rows[0])
    return [r for r in rows if batch_key(r) == key]


@contextmanager
def generation_guard(plugin, uid):
    with plugin._db() as conn:
        row = conn.execute("SELECT generation FROM user_generations WHERE canonical_user_id=?", (uid,)).fetchone()
    token = _generation.set((plugin._db_mgr.db_path, uid, int(row[0]) if row else 0))
    try:
        yield
    finally:
        _generation.reset(token)


def check_generation(conn, db_path):
    guard = _generation.get()
    if guard is None or guard[0] != db_path:
        return
    row = conn.execute("SELECT generation FROM user_generations WHERE canonical_user_id=?", (guard[1],)).fetchone()
    if (int(row[0]) if row else 0) != guard[2]:
        raise RuntimeError("user data was purged; stale task cancelled")


def user_task(parameter="user"):
    """Protect user-scoped administrative model operations across awaits."""
    import inspect
    from functools import wraps
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            owner = args[0]
            plugin = getattr(owner, "_plugin", getattr(owner, "plugin", owner))
            uid = bound.arguments[parameter]
            with generation_guard(plugin, uid):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def vector_task(table):
    from functools import wraps
    def decorate(function):
        @wraps(function)
        async def wrapped(plugin, item_id, text):
            with plugin._db() as conn:
                row = conn.execute(f"SELECT canonical_user_id FROM {table} WHERE id=?", (item_id,)).fetchone()
            if not row:
                return False
            with generation_guard(plugin, row[0]):
                return await function(plugin, item_id, text)
        return wrapped
    return decorate


DDL = (
    "CREATE TABLE IF NOT EXISTS user_generations(canonical_user_id TEXT PRIMARY KEY,generation INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS memory_quarantine(id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "canonical_user_id TEXT NOT NULL,content TEXT NOT NULL,reason TEXT NOT NULL,scope TEXT NOT NULL,"
    "persona_id TEXT NOT NULL,created_at TEXT NOT NULL)",
)


def purge_user(plugin, uid):
    """Delete all user-owned rows atomically; retain only a deletion generation."""
    counts = {}
    with plugin._db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for vector, column, source in (
            ("memory_vectors", "memory_id", "memories"),
            ("profile_item_vectors", "profile_item_id", "profile_items"),
        ):
            if vector in tables:
                conn.execute(f"DELETE FROM {vector} WHERE {column} IN "
                             f"(SELECT id FROM {source} WHERE canonical_user_id=?)", (uid,))
        # Discover legacy archival tables too; table names come exclusively from SQLite.
        preferred = ["profile_relations", "profile_item_evidence", "event_item_evidence", "episode_sources", "profile_items", "event_items"]
        for table in preferred + sorted(tables - set(preferred)):
            if table not in tables or table == "user_generations" or table.startswith("sqlite_"):
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({quoted})")}
            if "canonical_user_id" in cols:
                counts[table] = conn.execute(f"DELETE FROM {quoted} WHERE canonical_user_id=?", (uid,)).rowcount
        for cache in ("query_embedding_cache", "distill_prompt_cache", "distill_history"):
            if cache in tables:
                conn.execute(f"DELETE FROM {cache}")
        conn.execute("INSERT INTO user_generations VALUES(?,1) ON CONFLICT(canonical_user_id) "
                     "DO UPDATE SET generation=generation+1", (uid,))
    for name in ("_user_last_distilled_ts", "_last_conversation_ids"):
        values = getattr(plugin, name, None)
        if isinstance(values, dict):
            values.pop(uid, None)
    return {**counts, "memories": counts.get("memories", 0), "cache": counts.get("conversation_cache", 0)}
