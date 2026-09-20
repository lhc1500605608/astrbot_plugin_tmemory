import os
import time
import sqlite3
import threading
import logging
from typing import Optional, Dict

from .db_schema import (
    _DDL_MEMORIES,
    _DDL_MEMORY_EVENTS,
    _DDL_PROACTIVE_USER_POLICY,
    _DDL_PROACTIVE_REMINDERS,
    _DDL_PROACTIVE_SEND_LOG,
    _DDL_IDENTITY_MAPPINGS,
    _DDL_DISTILL_HISTORY,
    _DDL_CONVERSATION_CACHE,
    _DDL_MEMORY_EPISODES,
    _DDL_EPISODE_SOURCES,
    _DDL_USER_PROFILES,
    _DDL_PROFILE_ITEMS,
    _DDL_PROFILE_ITEM_EVIDENCE,
    _DDL_PROFILE_RELATIONS,
    _DDL_QUERY_EMBEDDING_CACHE,
    _DDL_DISTILL_PROMPT_CACHE,
)
# Re-exported for backward compatibility (previously defined in db.py).
from .db_schema import (  # noqa: F401
    _DDL_MEMORY_FTS,
    _DDL_TRIGGER_AI,
    _DDL_TRIGGER_AD,
    _DDL_TRIGGER_AU,
)
from .db_fts import DbFtsMixin

logger = logging.getLogger("astrbot")


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

class DatabaseManager(DbFtsMixin):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn_lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._fts5_needs_rebuild = False
        # sqlite-vec 扩展：模块导入 ≠ 连接可用。必须在每个连接上显式 load，
        # 否则会出现 "sqlite-vec loaded" 但 vec0 缺失（no such module: vec0）。
        self._vec_module = None
        self.vec_enabled = False

    def set_vec_extension(self, module) -> None:
        """注册 sqlite_vec 模块；连接建立时自动加载。"""
        self._vec_module = module
        self.vec_enabled = False
        if self._conn is not None:
            self._load_vec_extension(self._conn)
            self.vec_enabled = self.vec0_available(self._conn)

    def _load_vec_extension(self, conn: sqlite3.Connection) -> None:
        if self._vec_module is None:
            return
        try:
            conn.enable_load_extension(True)
        except (AttributeError, sqlite3.Error):
            pass
        try:
            self._vec_module.load(conn)
        except Exception as e:  # noqa: BLE001 - 加载失败需清晰降级
            logger.warning("[tmemory] sqlite-vec extension load failed: %s", e)

    @staticmethod
    def vec0_available(conn: sqlite3.Connection) -> bool:
        try:
            row = conn.execute(
                "SELECT 1 FROM pragma_module_list WHERE name = 'vec0'"
            ).fetchone()
            return bool(row)
        except sqlite3.Error:
            return False

    def db(self) -> _LockedConnection:
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    conn = sqlite3.connect(self.db_path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    if self._is_malformed(conn):
                        # SQLite 文件损坏（database disk image is malformed）会中断
                        # 插件加载。备份损坏文件并重建空库，保证插件可启动；
                        # 旧数据保留在 <db>.corrupt-<ts> 供人工修复。
                        try:
                            conn.close()
                        except sqlite3.Error:
                            pass
                        backup_path = f"{self.db_path}.corrupt-{int(time.time())}"
                        try:
                            os.replace(self.db_path, backup_path)
                            logger.warning(
                                "[tmemory] SQLite database malformed; backed up to %s and recreated a fresh database.",
                                backup_path,
                            )
                        except OSError as e:  # noqa: BLE001
                            logger.warning(
                                "[tmemory] SQLite database malformed and backup failed (%s); recreating in place.",
                                e,
                            )
                        conn = sqlite3.connect(self.db_path, check_same_thread=False)
                        conn.row_factory = sqlite3.Row
                    self._load_vec_extension(conn)
                    self._conn = conn
        return _LockedConnection(self._conn_lock, self._conn)

    @staticmethod
    def _is_malformed(conn: sqlite3.Connection) -> bool:
        """检测 SQLite 文件是否损坏（quick_check）。"""
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as e:  # noqa: BLE001
            return "malformed" in str(e).lower()
        except sqlite3.Error:
            return False
        if not row:
            return False
        return str(row[0]).strip().lower() != "ok"

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

    def migrate_schema(self, conn: sqlite3.Connection) -> None:
        # 先修复半初始化的 FTS 索引：任何内容表 UPDATE 都会经触发器写 FTS，
        # 空索引会抛 malformed 并中断加载。
        self._repair_inconsistent_fts(conn)
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
                "archived_at": "TEXT NOT NULL DEFAULT ''",
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
                        "rule_gated_batches": "INTEGER NOT NULL DEFAULT -1",
                        "prompt_cache_hits": "INTEGER NOT NULL DEFAULT -1",
                    }.items():
                        if col not in existing_dh:
                            conn.execute(f"ALTER TABLE distill_history ADD COLUMN {col} {ddl}")
        except Exception as e:
            logger.warning("[tmemory] Failed to migrate distill_history schema: %s", e)

        self._migrate_fts5_to_content_sync(conn)

    def _init_vector_tables(
        self, conn: sqlite3.Connection, embed_dim: int, requested: bool
    ) -> bool:
        """建表前校验 vec0 是否真在连接上可用；不可用则清晰降级。"""
        self.vec_enabled = False
        if not requested:
            return False
        if not self.vec0_available(conn):
            logger.warning(
                "[tmemory] sqlite-vec vec0 module 在连接上不可用；跳过向量表创建，"
                "向量检索降级（不会出现 no such table）。"
            )
            return False
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors "
                f"USING vec0(memory_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
            )
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS profile_item_vectors "
                f"USING vec0(profile_item_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
            )
            self.vec_enabled = True
            return True
        except sqlite3.Error as e:
            logger.warning("[tmemory] failed to create vector tables: %s", e)
            self.vec_enabled = False
            return False

    def init_db(self, vec_available: bool, embed_dim: int) -> None:
        with self.db() as conn:
            conn.execute(_DDL_MEMORIES)
            conn.execute(_DDL_MEMORY_EVENTS)
            conn.execute(_DDL_PROACTIVE_USER_POLICY)
            conn.execute(_DDL_PROACTIVE_REMINDERS)
            conn.execute(_DDL_PROACTIVE_SEND_LOG)
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
            conn.execute(_DDL_QUERY_EMBEDDING_CACHE)
            conn.execute(_DDL_DISTILL_PROMPT_CACHE)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_identity_bindings_canonical ON identity_bindings (canonical_user_id)")

            self.migrate_schema(conn)

            self._init_fts(conn)
            self._init_vector_tables(conn, embed_dim, vec_available)

            # --- Existing indexes ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories (canonical_user_id, is_active, updated_at)")
            
            # --- New indexes for scope/persona performance ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope_persona ON memories (canonical_user_id, scope, persona_id)")
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

            # --- Proactive memory indexes ---
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proactive_send_log_user ON proactive_send_log (canonical_user_id, status, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proactive_send_log_status ON proactive_send_log (status, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proactive_reminders_due ON proactive_reminders (status, due_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proactive_reminders_user ON proactive_reminders (canonical_user_id, status)")
