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
    summary_channel TEXT NOT NULL DEFAULT 'canonical',
    attention_score REAL NOT NULL DEFAULT 0.5,
    episode_id INTEGER NOT NULL DEFAULT 0,
    derived_from TEXT NOT NULL DEFAULT 'direct',
    evidence_json TEXT NOT NULL DEFAULT '',
    semantic_status TEXT NOT NULL DEFAULT 'active',
    contradiction_of INTEGER NOT NULL DEFAULT 0,
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

_DDL_MEMORY_EPISODES = """
CREATE TABLE IF NOT EXISTS memory_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'user',
    persona_id TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL DEFAULT '',
    episode_title TEXT NOT NULL,
    episode_summary TEXT NOT NULL,
    topic_tags TEXT NOT NULL DEFAULT '[]',
    key_entities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'ongoing',
    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 0.5,
    consolidation_status TEXT NOT NULL DEFAULT 'pending_semantic',
    attention_score REAL NOT NULL DEFAULT 0.5,
    source_count INTEGER NOT NULL DEFAULT 0,
    first_source_at TEXT NOT NULL,
    last_source_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_DDL_EPISODE_SOURCES = """
CREATE TABLE IF NOT EXISTS episode_sources (
    episode_id INTEGER NOT NULL,
    conversation_cache_id INTEGER NOT NULL,
    canonical_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (episode_id, conversation_cache_id),
    FOREIGN KEY (episode_id) REFERENCES memory_episodes(id),
    FOREIGN KEY (conversation_cache_id) REFERENCES conversation_cache(id)
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
    persona_id TEXT NOT NULL DEFAULT '',
    episode_id INTEGER NOT NULL DEFAULT 0,
    session_key TEXT NOT NULL DEFAULT '',
    turn_index INTEGER NOT NULL DEFAULT 0,
    topic_hint TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL DEFAULT ''
)
"""

# ── Profile Tables (ADR user-profile-model) ────────────────────────────────────

_DDL_USER_PROFILES = """
CREATE TABLE IF NOT EXISTS user_profiles (
    canonical_user_id TEXT NOT NULL PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    profile_version INTEGER NOT NULL DEFAULT 1,
    summary_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_DDL_PROFILE_ITEMS = """
CREATE TABLE IF NOT EXISTS profile_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    facet_type TEXT NOT NULL CHECK (facet_type IN ('preference', 'fact', 'style', 'restriction', 'task_pattern')),
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    normalized_content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'contradicted', 'archived')),
    confidence REAL NOT NULL DEFAULT 0.5,
    importance REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.5,
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT NOT NULL DEFAULT '',
    last_confirmed_at TEXT NOT NULL DEFAULT '',
    source_scope TEXT NOT NULL DEFAULT 'user',
    persona_id TEXT NOT NULL DEFAULT '',
    embedding_status TEXT NOT NULL DEFAULT 'pending' CHECK (embedding_status IN ('pending', 'ready', 'disabled', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (canonical_user_id, facet_type, normalized_content, persona_id, source_scope)
)
"""

_DDL_PROFILE_ITEM_EVIDENCE = """
CREATE TABLE IF NOT EXISTS profile_item_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_item_id INTEGER NOT NULL,
    conversation_cache_id INTEGER NOT NULL DEFAULT 0,
    canonical_user_id TEXT NOT NULL,
    source_excerpt TEXT NOT NULL DEFAULT '',
    source_role TEXT NOT NULL DEFAULT 'user' CHECK (source_role IN ('user', 'assistant', 'system', 'manual', 'import')),
    source_timestamp TEXT NOT NULL DEFAULT '',
    evidence_kind TEXT NOT NULL DEFAULT 'conversation' CHECK (evidence_kind IN ('conversation', 'manual', 'import', 'merge')),
    confidence_delta REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_item_id) REFERENCES profile_items(id)
)
"""

_DDL_PROFILE_RELATIONS = """
CREATE TABLE IF NOT EXISTS profile_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    from_item_id INTEGER NOT NULL,
    to_item_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('supports', 'contradicts', 'depends_on', 'context_for', 'supersedes')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    weight REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (canonical_user_id, from_item_id, to_item_id, relation_type),
    FOREIGN KEY (from_item_id) REFERENCES profile_items(id),
    FOREIGN KEY (to_item_id) REFERENCES profile_items(id),
    CHECK (from_item_id != to_item_id)
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

_DDL_MEMORY_EPISODES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_episodes_fts USING fts5(
    episode_title,
    episode_summary,
    topic_tags,
    content='memory_episodes',
    content_rowid='id',
    tokenize='jieba'
)
"""

_DDL_TRIGGER_EP_AI = """
CREATE TRIGGER IF NOT EXISTS t_memory_episodes_ai AFTER INSERT ON memory_episodes BEGIN
  INSERT INTO memory_episodes_fts(rowid, episode_title, episode_summary, topic_tags)
  VALUES (new.id, new.episode_title, new.episode_summary, new.topic_tags);
END;
"""
_DDL_TRIGGER_EP_AD = """
CREATE TRIGGER IF NOT EXISTS t_memory_episodes_ad AFTER DELETE ON memory_episodes BEGIN
  INSERT INTO memory_episodes_fts(memory_episodes_fts, rowid, episode_title, episode_summary, topic_tags)
  VALUES ('delete', old.id, old.episode_title, old.episode_summary, old.topic_tags);
END;
"""
_DDL_TRIGGER_EP_AU = """
CREATE TRIGGER IF NOT EXISTS t_memory_episodes_au AFTER UPDATE ON memory_episodes BEGIN
  INSERT INTO memory_episodes_fts(memory_episodes_fts, rowid, episode_title, episode_summary, topic_tags)
  VALUES ('delete', old.id, old.episode_title, old.episode_summary, old.topic_tags);
  INSERT INTO memory_episodes_fts(rowid, episode_title, episode_summary, topic_tags)
  VALUES (new.id, new.episode_title, new.episode_summary, new.topic_tags);
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
                "summary_channel": "TEXT NOT NULL DEFAULT 'canonical'",
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "confidence": "REAL NOT NULL DEFAULT 0.5",
                "reinforce_count": "INTEGER NOT NULL DEFAULT 0",
                "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                "is_active": "INTEGER NOT NULL DEFAULT 1",
                "is_pinned": "INTEGER NOT NULL DEFAULT 0",
                "persona_id": "TEXT NOT NULL DEFAULT ''",
                "scope": "TEXT NOT NULL DEFAULT 'user'",
                "tokenized_memory": "TEXT NOT NULL DEFAULT ''",
                "attention_score": "REAL NOT NULL DEFAULT 0.5",
                "episode_id": "INTEGER NOT NULL DEFAULT 0",
                "derived_from": "TEXT NOT NULL DEFAULT 'direct'",
                "evidence_json": "TEXT NOT NULL DEFAULT ''",
                "semantic_status": "TEXT NOT NULL DEFAULT 'active'",
                "contradiction_of": "INTEGER NOT NULL DEFAULT 0",
            }
        )
        # Backfill summary_channel for existing rows: style → persona
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'").fetchone():
            conn.execute(
                "UPDATE memories SET summary_channel = 'persona' "
                "WHERE summary_channel = 'canonical' AND memory_type = 'style'"
            )
            # Backfill derived_from for pre-0.8.0 rows
            conn.execute(
                "UPDATE memories SET derived_from = 'legacy' "
                "WHERE derived_from = 'direct' AND episode_id = 0"
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
                "episode_id": "INTEGER NOT NULL DEFAULT 0",
                "session_key": "TEXT NOT NULL DEFAULT ''",
                "turn_index": "INTEGER NOT NULL DEFAULT 0",
                "topic_hint": "TEXT NOT NULL DEFAULT ''",
                "captured_at": "TEXT NOT NULL DEFAULT ''",
            }
        )
        # Backfill captured_at and session_key for existing rows
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_cache'").fetchone():
            conn.execute(
                "UPDATE conversation_cache SET captured_at = created_at "
                "WHERE captured_at = '' AND created_at != ''"
            )
            conn.execute(
                "UPDATE conversation_cache SET session_key = unified_msg_origin "
                "WHERE session_key = '' AND unified_msg_origin != ''"
            )

        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'").fetchone():
            conn.execute("UPDATE memories SET last_seen_at=COALESCE(NULLIF(last_seen_at, ''), updated_at, created_at)")

        self._ensure_columns(
            conn,
            "memory_episodes",
            {
                "attention_score": "REAL NOT NULL DEFAULT 0.5",
            }
        )

        # Migrate episode_sources from pre-0.8.0 schema (auto-increment id → compound PK)
        es_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='episode_sources'"
        ).fetchone()
        if es_exists:
            es_cols = {r["name"] for r in conn.execute("PRAGMA table_info(episode_sources)").fetchall()}
            if "id" in es_cols or "canonical_user_id" not in es_cols:
                logger.info("[tmemory] Migrating episode_sources to 0.8.0 compound-PK schema...")
                conn.execute("DROP TABLE IF EXISTS episode_sources_old")
                conn.execute("ALTER TABLE episode_sources RENAME TO episode_sources_old")
                conn.execute(_DDL_EPISODE_SOURCES)
                logger.info("[tmemory] episode_sources migrated, old data preserved in episode_sources_old")

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
            conn.execute(_DDL_MEMORY_EPISODES)
            conn.execute(_DDL_EPISODE_SOURCES)

            # ── Profile tables ──
            conn.execute(_DDL_USER_PROFILES)
            conn.execute(_DDL_PROFILE_ITEMS)
            conn.execute(_DDL_PROFILE_ITEM_EVIDENCE)
            conn.execute(_DDL_PROFILE_RELATIONS)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_bindings_canonical ON identity_bindings (canonical_user_id)")

            self.migrate_schema(conn)

            # Check FTS5
            try:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS tmemory_fts_test USING fts5(text, tokenize='jieba')")
                conn.execute("DROP TABLE tmemory_fts_test")
                
                conn.execute(_DDL_FTS5)
                conn.execute(_DDL_TRIGGER_AI)
                conn.execute(_DDL_TRIGGER_AD)
                conn.execute(_DDL_TRIGGER_AU)

                conn.execute(_DDL_MEMORY_EPISODES_FTS)
                conn.execute(_DDL_TRIGGER_EP_AI)
                conn.execute(_DDL_TRIGGER_EP_AD)
                conn.execute(_DDL_TRIGGER_EP_AU)

                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS profile_items_fts USING fts5(
                        content,
                        facet_type,
                        content='profile_items',
                        content_rowid='id',
                        tokenize='jieba'
                    )
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS t_profile_items_ai AFTER INSERT ON profile_items BEGIN
                      INSERT INTO profile_items_fts(rowid, content, facet_type)
                      VALUES (new.id, new.content, new.facet_type);
                    END;
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS t_profile_items_ad AFTER DELETE ON profile_items BEGIN
                      INSERT INTO profile_items_fts(profile_items_fts, rowid, content, facet_type)
                      VALUES ('delete', old.id, old.content, old.facet_type);
                    END;
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS t_profile_items_au AFTER UPDATE ON profile_items BEGIN
                      INSERT INTO profile_items_fts(profile_items_fts, rowid, content, facet_type)
                      VALUES ('delete', old.id, old.content, old.facet_type);
                      INSERT INTO profile_items_fts(rowid, content, facet_type)
                      VALUES (new.id, new.content, new.facet_type);
                    END;
                """)

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
                    conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS profile_item_vectors "
                        f"USING vec0(profile_item_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
                    )
                except Exception as _ve:
                    logger.warning("[tmemory] failed to create vector tables: %s", _ve)

            # --- Existing indexes ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (canonical_user_id, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_distilled ON conversations (distilled, timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories (canonical_user_id, is_active, updated_at)")
            
            # --- New indexes for scope/persona performance ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope_persona ON memories (canonical_user_id, scope, persona_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_scope_persona ON conversations (canonical_user_id, scope, persona_id)")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_episodes_user_active ON memory_episodes (canonical_user_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_episodes_session ON memory_episodes (session_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_episodes_status ON memory_episodes (status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_episodes_attention ON memory_episodes (attention_score)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_sources_episode ON episode_sources (episode_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_sources_conversation ON episode_sources (conversation_cache_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episode_sources_user ON episode_sources (canonical_user_id)")

            # --- Consolidation pipeline indexes ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_episodes_consolidation ON memory_episodes (canonical_user_id, consolidation_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_working_user_session ON conversation_cache (canonical_user_id, session_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_episode_pending ON conversation_cache (episode_id, distilled)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_captured_at ON conversation_cache (captured_at)")

            # --- Memories new indexes ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_episode ON memories (episode_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_semantic_status ON memories (semantic_status)")

            # --- Profile table indexes ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_items_user_status ON profile_items (canonical_user_id, status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_items_retrieval ON profile_items (canonical_user_id, facet_type, status, importance, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_items_scope_persona ON profile_items (canonical_user_id, source_scope, persona_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_items_embedding ON profile_items (embedding_status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_evidence_item ON profile_item_evidence (profile_item_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_evidence_conversation ON profile_item_evidence (conversation_cache_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_evidence_user ON profile_item_evidence (canonical_user_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_relations_from ON profile_relations (canonical_user_id, from_item_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_profile_relations_to ON profile_relations (canonical_user_id, to_item_id, status)")
