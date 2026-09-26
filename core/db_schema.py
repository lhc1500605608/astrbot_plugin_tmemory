"""SQLite DDL constants and FTS tokenizer helpers.

Extracted from ``core/db.py`` per ADR-009 (module boundary split). Physical move
only — no behavior change.
"""

import sqlite3
from typing import Optional

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
    event_date TEXT NOT NULL DEFAULT '',
    valid_until TEXT NOT NULL DEFAULT '',
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

# ── Proactive memory tables (Plan TMEAAA-379 B2 / BC-3) ───────────────────────

_DDL_PROACTIVE_USER_POLICY = """
CREATE TABLE IF NOT EXISTS proactive_user_policy (
    canonical_user_id TEXT NOT NULL PRIMARY KEY,
    opt_in INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'user',
    updated_at TEXT NOT NULL
)
"""

_DDL_PROACTIVE_REMINDERS = """
CREATE TABLE IF NOT EXISTS proactive_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    unified_msg_origin TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT ''
)
"""

_DDL_PROACTIVE_SEND_LOG = """
CREATE TABLE IF NOT EXISTS proactive_send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
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
    tokens_total INTEGER NOT NULL DEFAULT -1,
    rule_gated_batches INTEGER NOT NULL DEFAULT -1,
    prompt_cache_hits INTEGER NOT NULL DEFAULT -1
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
    captured_at TEXT NOT NULL DEFAULT '',
    archived_at TEXT NOT NULL DEFAULT ''
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

_DDL_QUERY_EMBEDDING_CACHE = """
CREATE TABLE IF NOT EXISTS query_embedding_cache (
    query_hash TEXT PRIMARY KEY,
    query_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    embed_dim INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_hit_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 1
)
"""

_DDL_DISTILL_PROMPT_CACHE = """
CREATE TABLE IF NOT EXISTS distill_prompt_cache (
    transcript_hash TEXT PRIMARY KEY,
    transcript TEXT NOT NULL,
    memories_json TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_hit_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
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

# ── FTS5 tokenizer 策略（v0.11.1）───────────────────────────────────────────
# 内置 SQLite 不带 jieba tokenizer（需自行编译扩展），此前 tokenize='jieba'
# 必然失败并整体降级。改为：
#   * memories_fts 索引 ``tokenized_memory``（写入时 Python jieba 已分词），
#     使用内置 ``unicode61``；查询端同样 jieba 分词后用 AND 组合。
#   * profile_items_fts / memory_episodes_fts 直接索引原始中文文本，
#     使用内置 ``trigram``（SQLite ≥3.34，支持中文子串），不可用时回退 unicode61。
_FTS_MEMORY_TOKENIZER = "unicode61"
_FTS_TEXT_TOKENIZER_CANDIDATES = ("trigram", "unicode61")

_DDL_MEMORY_FTS = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    tokenized_memory,
    canonical_user_id UNINDEXED,
    content='memories',
    content_rowid='id',
    tokenize='{_FTS_MEMORY_TOKENIZER}'
)
"""

_DDL_TRIGGER_AI = """
CREATE TRIGGER IF NOT EXISTS t_memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, tokenized_memory, canonical_user_id)
  VALUES (new.id, new.tokenized_memory, new.canonical_user_id);
END;
"""
_DDL_TRIGGER_AD = """
CREATE TRIGGER IF NOT EXISTS t_memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, tokenized_memory, canonical_user_id)
  VALUES ('delete', old.id, old.tokenized_memory, old.canonical_user_id);
END;
"""
_DDL_TRIGGER_AU = """
CREATE TRIGGER IF NOT EXISTS t_memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, tokenized_memory, canonical_user_id)
  VALUES ('delete', old.id, old.tokenized_memory, old.canonical_user_id);
  INSERT INTO memories_fts(rowid, tokenized_memory, canonical_user_id)
  VALUES (new.id, new.tokenized_memory, new.canonical_user_id);
END;
"""


def _fts_tokenizer_supported(conn: sqlite3.Connection, tokenizer: str) -> bool:
    """探测给定 FTS5 tokenizer 是否由当前 SQLite 提供。"""
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE temp._tmem_fts_probe USING fts5(x, tokenize='{tokenizer}')"
        )
        conn.execute("DROP TABLE temp._tmem_fts_probe")
        return True
    except sqlite3.OperationalError:
        try:
            conn.execute("DROP TABLE IF EXISTS temp._tmem_fts_probe")
        except sqlite3.Error:
            pass
        return False


def _select_text_fts_tokenizer(conn: sqlite3.Connection) -> Optional[str]:
    for tok in _FTS_TEXT_TOKENIZER_CANDIDATES:
        if _fts_tokenizer_supported(conn, tok):
            return tok
    return None


def _memory_episodes_fts_ddl(tokenizer: str) -> str:
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_episodes_fts USING fts5(
    episode_title,
    episode_summary,
    topic_tags,
    canonical_user_id UNINDEXED,
    content='memory_episodes',
    content_rowid='id',
    tokenize='{tokenizer}'
)
"""


_MEMORY_EPISODES_FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS t_memory_episodes_ai AFTER INSERT ON memory_episodes BEGIN
  INSERT INTO memory_episodes_fts(rowid, episode_title, episode_summary, topic_tags, canonical_user_id)
  VALUES (new.id, new.episode_title, new.episode_summary, new.topic_tags, new.canonical_user_id);
END;""",
    """CREATE TRIGGER IF NOT EXISTS t_memory_episodes_ad AFTER DELETE ON memory_episodes BEGIN
  INSERT INTO memory_episodes_fts(memory_episodes_fts, rowid, episode_title, episode_summary, topic_tags, canonical_user_id)
  VALUES ('delete', old.id, old.episode_title, old.episode_summary, old.topic_tags, old.canonical_user_id);
END;""",
    """CREATE TRIGGER IF NOT EXISTS t_memory_episodes_au AFTER UPDATE ON memory_episodes BEGIN
  INSERT INTO memory_episodes_fts(memory_episodes_fts, rowid, episode_title, episode_summary, topic_tags, canonical_user_id)
  VALUES ('delete', old.id, old.episode_title, old.episode_summary, old.topic_tags, old.canonical_user_id);
  INSERT INTO memory_episodes_fts(rowid, episode_title, episode_summary, topic_tags, canonical_user_id)
  VALUES (new.id, new.episode_title, new.episode_summary, new.topic_tags, new.canonical_user_id);
END;""",
)


def _profile_items_fts_ddl(tokenizer: str) -> str:
    return f"""
CREATE VIRTUAL TABLE IF NOT EXISTS profile_items_fts USING fts5(
    content,
    facet_type,
    canonical_user_id UNINDEXED,
    content='profile_items',
    content_rowid='id',
    tokenize='{tokenizer}'
)
"""


_PROFILE_ITEMS_FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS t_profile_items_ai AFTER INSERT ON profile_items BEGIN
  INSERT INTO profile_items_fts(rowid, content, facet_type, canonical_user_id)
  VALUES (new.id, new.content, new.facet_type, new.canonical_user_id);
END;""",
    """CREATE TRIGGER IF NOT EXISTS t_profile_items_ad AFTER DELETE ON profile_items BEGIN
  INSERT INTO profile_items_fts(profile_items_fts, rowid, content, facet_type, canonical_user_id)
  VALUES ('delete', old.id, old.content, old.facet_type, old.canonical_user_id);
END;""",
    """CREATE TRIGGER IF NOT EXISTS t_profile_items_au AFTER UPDATE ON profile_items BEGIN
  INSERT INTO profile_items_fts(profile_items_fts, rowid, content, facet_type, canonical_user_id)
  VALUES ('delete', old.id, old.content, old.facet_type, old.canonical_user_id);
  INSERT INTO profile_items_fts(rowid, content, facet_type, canonical_user_id)
  VALUES (new.id, new.content, new.facet_type, new.canonical_user_id);
END;""",
)
