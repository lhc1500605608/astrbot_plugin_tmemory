import sqlite3
import threading
import logging
import jieba
from typing import Optional, Dict

logger = logging.getLogger("astrbot")

_DDL_CONVERSATIONS = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unified_msg_origin TEXT,
    canonical_user_id TEXT,
    role TEXT,
    content TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    source_adapter TEXT,
    source_user_id TEXT,
    distilled INTEGER DEFAULT 0,
    scope TEXT DEFAULT 'user',
    persona_id TEXT DEFAULT ''
)
"""

_DDL_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    source_adapter TEXT NOT NULL,
    source_user_id TEXT NOT NULL,
    source_channel TEXT NOT NULL DEFAULT 'default',
    memory_type TEXT NOT NULL DEFAULT 'fact',
    memory TEXT NOT NULL,
    tokenized_memory TEXT NOT NULL DEFAULT '',
    memory_hash TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0.5,
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.5,
    reinforce_count INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    persona_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'user',
    UNIQUE(canonical_user_id, memory_hash, persona_id, scope)
)
"""

_DDL_MEMORY_EVENTS = """
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_DDL_IDENTITY_MAPPINGS = """
CREATE TABLE IF NOT EXISTS identity_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter TEXT NOT NULL,
    adapter_user_id TEXT NOT NULL,
    canonical_user_id TEXT NOT NULL,
    bound_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(adapter, adapter_user_id)
)
"""

_DDL_DISTILL_HISTORY = """
CREATE TABLE IF NOT EXISTS distill_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    users_processed INTEGER NOT NULL DEFAULT 0,
    memories_created INTEGER NOT NULL DEFAULT 0,
    users_failed INTEGER NOT NULL DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]',
    duration_sec REAL NOT NULL DEFAULT 0,
    tokens_input INTEGER NOT NULL DEFAULT -1,
    tokens_output INTEGER NOT NULL DEFAULT -1,
    tokens_total INTEGER NOT NULL DEFAULT -1
)
"""

_DDL_STYLE_PROFILES = """
CREATE TABLE IF NOT EXISTS style_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT UNIQUE NOT NULL,
    prompt_supplement TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    source_user TEXT NOT NULL DEFAULT '',
    source_adapter TEXT NOT NULL DEFAULT '',
    style_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_DDL_STYLE_BINDINGS = """
CREATE TABLE IF NOT EXISTS style_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter_name TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    profile_id INTEGER DEFAULT NULL REFERENCES style_profiles(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(adapter_name, conversation_id)
)
"""

_DDL_CONVERSATION_CACHE = """
CREATE TABLE IF NOT EXISTS conversation_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    source_adapter TEXT NOT NULL DEFAULT 'unknown',
    source_user_id TEXT NOT NULL DEFAULT 'unknown',
    unified_msg_origin TEXT NOT NULL DEFAULT '',
    distilled INTEGER NOT NULL DEFAULT 0,
    distilled_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'user',
    persona_id TEXT NOT NULL DEFAULT ''
)
"""

_DDL_FTS5 = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory,
    memory_type,
    content='memories',
    content_rowid='id',
    tokenize='jieba'
)
"""

_DDL_TRIGGER_AI = """
CREATE TRIGGER IF NOT EXISTS t_memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, memory, memory_type)
  VALUES (new.id, new.memory, new.memory_type);
END;
"""
_DDL_TRIGGER_AD = """
CREATE TRIGGER IF NOT EXISTS t_memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, memory, memory_type)
  VALUES ('delete', old.id, old.memory, old.memory_type);
END;
"""
_DDL_TRIGGER_AU = """
CREATE TRIGGER IF NOT EXISTS t_memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, memory, memory_type)
  VALUES ('delete', old.id, old.memory, old.memory_type);
  INSERT INTO memories_fts(rowid, memory, memory_type)
  VALUES (new.id, new.memory, new.memory_type);
END;
"""


class _LockedConnection:
    def __init__(self, lock: threading.Lock, conn: sqlite3.Connection):
        self.lock = lock
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.lock.acquire()
        self.conn.__enter__()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return self.conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.lock.release()

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn_lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._fts5_needs_rebuild = False

    def db(self) -> _LockedConnection:
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    conn = sqlite3.connect(self.db_path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    self._conn = conn
        return _LockedConnection(self._conn_lock, self._conn)

    def close(self) -> None:
        with self._conn_lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _ensure_columns(self, conn: sqlite3.Connection, table_name: str, wanted: Dict[str, str]) -> None:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        if not exists:
            return
        existing = {
            r["name"]
            for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for col, ddl in wanted.items():
            if col in existing:
                continue
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {ddl}")

    def _migrate_fts5_to_content_sync(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone()
        if not row:
            return

        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone()
        if row:
            create_sql = str(row[0] or row["sql"] if isinstance(row, sqlite3.Row) else row[0])
            if "content=" in create_sql or "content =" in create_sql:
                return

        logger.info("[tmemory] 正在将 FTS5 表迁移到 content-sync 模式...")
        conn.execute("DROP TRIGGER IF EXISTS t_memories_ai")
        conn.execute("DROP TRIGGER IF EXISTS t_memories_ad")
        conn.execute("DROP TRIGGER IF EXISTS t_memories_au")
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        self._fts5_needs_rebuild = True

    def migrate_schema(self, conn: sqlite3.Connection) -> None:
        self._ensure_columns(
            conn,
            "memories",
            {
                "source_channel": "TEXT NOT NULL DEFAULT 'default'",
                "memory_type": "TEXT NOT NULL DEFAULT 'fact'",
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "confidence": "REAL NOT NULL DEFAULT 0.5",
                "reinforce_count": "INTEGER NOT NULL DEFAULT 0",
                "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                "is_active": "INTEGER NOT NULL DEFAULT 1",
                "is_pinned": "INTEGER NOT NULL DEFAULT 0",
                "persona_id": "TEXT NOT NULL DEFAULT ''",
                "scope": "TEXT NOT NULL DEFAULT 'user'",
                "tokenized_memory": "TEXT NOT NULL DEFAULT ''",
            }
        )
        self._ensure_columns(
            conn,
            "conversation_cache",
            {
                "source_adapter": "TEXT NOT NULL DEFAULT 'unknown'",
                "source_user_id": "TEXT NOT NULL DEFAULT 'unknown'",
                "unified_msg_origin": "TEXT NOT NULL DEFAULT ''",
                "distilled": "INTEGER NOT NULL DEFAULT 0",
                "distilled_at": "TEXT NOT NULL DEFAULT ''",
                "scope": "TEXT NOT NULL DEFAULT 'user'",
                "persona_id": "TEXT NOT NULL DEFAULT ''",
            }
        )

        conn.execute("UPDATE memories SET last_seen_at=COALESCE(NULLIF(last_seen_at, ''), updated_at, created_at)")

        self._ensure_columns(
            conn,
            "style_profiles",
            {
                "source_user": "TEXT NOT NULL DEFAULT ''",
                "source_adapter": "TEXT NOT NULL DEFAULT ''",
                "style_summary": "TEXT NOT NULL DEFAULT ''",
            }
        )

        try:
            existing_dh = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(distill_history)").fetchall()
            }
            if existing_dh:
                # Detect old schema by checking for old column names
                old_markers = {
                    "status", "run_at", "canonical_user_id", "persona_id",
                    "scope", "messages_processed", "memories_generated", "error_msg",
                }
                if existing_dh & old_markers:
                    logger.info("[tmemory] Migrating distill_history from old schema to new schema...")
                    conn.execute("DROP TABLE IF EXISTS distill_history_old")
                    conn.execute("ALTER TABLE distill_history RENAME TO distill_history_old")
                    conn.execute(_DDL_DISTILL_HISTORY)
                    logger.info("[tmemory] distill_history migrated, old data preserved in distill_history_old")
                else:
                    for col, ddl in {
                        "tokens_input": "INTEGER NOT NULL DEFAULT -1",
                        "tokens_output": "INTEGER NOT NULL DEFAULT -1",
                        "tokens_total": "INTEGER NOT NULL DEFAULT -1",
                    }.items():
                        if col not in existing_dh:
                            conn.execute(f"ALTER TABLE distill_history ADD COLUMN {col} {ddl}")
        except Exception as e:
            logger.warning("[tmemory] Failed to migrate distill_history schema: %s", e)

        self._migrate_fts5_to_content_sync(conn)

    def init_db(self, vec_available: bool, embed_dim: int) -> None:
        with self.db() as conn:
            conn.execute(_DDL_CONVERSATIONS)
            conn.execute(_DDL_MEMORIES)
            conn.execute(_DDL_MEMORY_EVENTS)
            conn.execute(_DDL_IDENTITY_MAPPINGS)
            conn.execute(_DDL_DISTILL_HISTORY)
            conn.execute(_DDL_CONVERSATION_CACHE)
            conn.execute(_DDL_STYLE_PROFILES)
            conn.execute(_DDL_STYLE_BINDINGS)
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_bindings_canonical ON identity_bindings (canonical_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_style_bindings_lookup ON style_bindings (adapter_name, conversation_id)")

            self.migrate_schema(conn)

            # Check FTS5
            try:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS tmemory_fts_test USING fts5(text, tokenize='jieba')")
                conn.execute("DROP TABLE tmemory_fts_test")
                
                conn.execute(_DDL_FTS5)
                conn.execute(_DDL_TRIGGER_AI)
                conn.execute(_DDL_TRIGGER_AD)
                conn.execute(_DDL_TRIGGER_AU)

                # Data migration for empty tokenized_memory
                untokenized = conn.execute("SELECT id, memory FROM memories WHERE tokenized_memory = ''").fetchall()
                if untokenized:
                    logger.info(f"[tmemory] 正在为 {len(untokenized)} 条历史记忆生成全文检索词元...")
                    if self._fts5_needs_rebuild:
                        conn.execute("DROP TRIGGER IF EXISTS t_memories_ai")
                        conn.execute("DROP TRIGGER IF EXISTS t_memories_ad")
                        conn.execute("DROP TRIGGER IF EXISTS t_memories_au")
                    for row in untokenized:
                        mem_text = str(row["memory"])
                        tokens = " ".join(jieba.cut_for_search(mem_text))
                        conn.execute("UPDATE memories SET tokenized_memory = ? WHERE id = ?", (tokens, int(row["id"])))
                    if self._fts5_needs_rebuild:
                        conn.execute(_DDL_TRIGGER_AI)
                        conn.execute(_DDL_TRIGGER_AD)
                        conn.execute(_DDL_TRIGGER_AU)
                    logger.info("[tmemory] 历史记忆分词完成。")

                if self._fts5_needs_rebuild:
                    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                    self._fts5_needs_rebuild = False
                    logger.info("[tmemory] FTS5 索引重建完成。")
            except sqlite3.OperationalError as e:
                logger.warning(f"[tmemory] FTS5 w/ jieba tokenizer is NOT available: {e}. Falling back to plain LIKE.")

            if vec_available:
                try:
                    conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors "
                        f"USING vec0(memory_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
                    )
                except Exception as _ve:
                    logger.warning("[tmemory] failed to create memory_vectors: %s", _ve)

            # --- Existing indexes ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (canonical_user_id, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_distilled ON conversations (distilled, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories (canonical_user_id, is_active, updated_at)")
            
            # --- New indexes for scope/persona performance ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope_persona ON memories (canonical_user_id, scope, persona_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_scope_persona ON conversations (canonical_user_id, scope, persona_id)")
