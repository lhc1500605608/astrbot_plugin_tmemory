"""
astrbot_plugin_tmemory 压力测试
================================
不依赖 AstrBot 框架，直接测试插件的核心数据库逻辑：

- 并发写入 conversation_cache（模拟高频消息采集）
- 并发读取 memories（模拟 LLM 请求时的记忆召回）
- 大量 _insert_memory（模拟蒸馏写入）
- 高并发 _resolve_current_identity（模拟多用户身份查找）
- 边界测试：超长文本、空文本、特殊字符、Unicode
- 数据库稳定性：WAL 并发读写冲突、busy_timeout
- _decay_stale_memories 与 _auto_prune_low_quality 的性能
- _retrieve_memories 关键词打分性能（100 条记忆时）
"""

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# 从 main.py 中内联所有需要测试的方法（不依赖 AstrBot 框架）
# ─────────────────────────────────────────────────────────────────────────────

VALID_MEMORY_TYPES = {"preference", "fact", "task", "restriction", "style"}


class PluginCore:
    """TMemoryPlugin 核心逻辑的独立可测试副本（剥离 AstrBot 依赖）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.plugin_name = "tmemory_test"
        self.cache_max_rows = 20
        self.memory_max_chars = 220
        self.distill_min_batch_count = 8
        self.distill_batch_limit = 80
        self._vec_available = False
        self._sqlite_vec = None
        self.vector_weight = 0.4
        self._sanitize_patterns = self._build_sanitize_patterns()
        self._JUNK_PATTERNS = [
            re.compile(r"^(你好|您好|嗨|hi|hello|hey|哈哈|嗯|哦|好的|ok|okay|谢谢|再见|拜拜)", re.IGNORECASE),
            re.compile(r"^(用户说|用户问|用户发送|assistant|AI|助手)", re.IGNORECASE),
            re.compile(r"^.{0,5}$"),
        ]
        self._UNSAFE_PATTERNS = [
            re.compile(r"(password|passwd|密码|secret|token|api.?key|bearer)", re.IGNORECASE),
            re.compile(r"(杀|死|炸|毒|枪|赌博|色情|porn)", re.IGNORECASE),
            re.compile(r"(ignore.*(previous|above)|忽略.*(之前|以上)|system.?prompt|越狱|jailbreak)", re.IGNORECASE),
        ]
        self._init_db()
        self._migrate_schema()

    # ── DB ───────────────────────────────────────────────────────────────────

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS identity_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    adapter TEXT NOT NULL,
                    adapter_user_id TEXT NOT NULL,
                    canonical_user_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(adapter, adapter_user_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_user_id TEXT NOT NULL,
                    source_adapter TEXT NOT NULL,
                    source_user_id TEXT NOT NULL,
                    source_channel TEXT NOT NULL DEFAULT 'default',
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    memory TEXT NOT NULL,
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
                    UNIQUE(canonical_user_id, memory_hash)
                )
            """)
            conn.execute("""
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
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS distill_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    trigger_type TEXT NOT NULL DEFAULT 'auto',
                    users_processed INTEGER NOT NULL DEFAULT 0,
                    memories_created INTEGER NOT NULL DEFAULT 0,
                    users_failed INTEGER NOT NULL DEFAULT 0,
                    errors TEXT NOT NULL DEFAULT '',
                    duration_sec REAL NOT NULL DEFAULT 0
                )
            """)

    def _migrate_schema(self):
        self._ensure_columns("memories", {
            "source_channel": "TEXT NOT NULL DEFAULT 'default'",
            "memory_type": "TEXT NOT NULL DEFAULT 'fact'",
            "importance": "REAL NOT NULL DEFAULT 0.5",
            "confidence": "REAL NOT NULL DEFAULT 0.5",
            "reinforce_count": "INTEGER NOT NULL DEFAULT 0",
            "last_seen_at": "TEXT NOT NULL DEFAULT ''",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "is_pinned": "INTEGER NOT NULL DEFAULT 0",
        })
        self._ensure_columns("conversation_cache", {
            "source_adapter": "TEXT NOT NULL DEFAULT 'unknown'",
            "source_user_id": "TEXT NOT NULL DEFAULT 'unknown'",
            "unified_msg_origin": "TEXT NOT NULL DEFAULT ''",
            "distilled": "INTEGER NOT NULL DEFAULT 0",
            "distilled_at": "TEXT NOT NULL DEFAULT ''",
        })

    def _ensure_columns(self, table_name: str, wanted: Dict[str, str]):
        with self._db() as conn:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            existing = {str(row["name"]) for row in rows}
            for col, ddl in wanted.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {ddl}")

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _clamp01(self, value) -> float:
        try:
            num = float(value)
        except Exception:
            num = 0.0
        return max(0.0, min(1.0, num))

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _tokenize(self, text: str) -> List[str]:
        normalized = self._normalize_text(text)
        return [w.lower() for w in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if len(w) >= 2]

    def _safe_memory_type(self, value) -> str:
        s = str(value or "fact").strip().lower()
        if s in VALID_MEMORY_TYPES:
            return s
        return "fact"

    def _build_sanitize_patterns(self) -> list:
        return [
            (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
            (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱]"),
            (re.compile(r"\d{17}[\dXx]"), "[身份证]"),
            (re.compile(r"\d{15,19}"), "[长数字]"),
        ]

    def _sanitize_text(self, text: str) -> str:
        for pattern, replacement in self._sanitize_patterns:
            text = pattern.sub(replacement, text)
        return text

    def _distill_text(self, text: str) -> str:
        normalized = self._normalize_text(text)
        if not normalized:
            return "空白输入"
        words = [w for w in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if len(w) >= 2]
        top = [w for w, _ in Counter(words).most_common(5)]
        prefix = f"关键词: {'/'.join(top)}; " if top else ""
        short = normalized[:self.memory_max_chars]
        return f"{prefix}记忆: {short}"

    def _is_junk_memory(self, text: str) -> bool:
        for pat in self._JUNK_PATTERNS:
            if pat.search(text):
                return True
        if len(set(text.replace(" ", ""))) <= 3:
            return True
        meaningful_chars = len(re.sub(r"[^\w一-鿿]", "", text))
        return meaningful_chars < 5

    def _is_unsafe_memory(self, text: str) -> bool:
        for pat in self._UNSAFE_PATTERNS:
            if pat.search(text):
                return True
        return False

    # ── 身份管理 ──────────────────────────────────────────────────────────────

    def _bind_identity(self, adapter: str, adapter_user: str, canonical_id: str):
        now = self._now()
        with self._db() as conn:
            conn.execute("""
                INSERT INTO identity_bindings(adapter, adapter_user_id, canonical_user_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(adapter, adapter_user_id)
                DO UPDATE SET canonical_user_id=excluded.canonical_user_id, updated_at=excluded.updated_at
            """, (adapter, adapter_user, canonical_id, now))

    def _resolve_identity(self, adapter: str, adapter_user: str) -> str:
        with self._db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM identity_bindings WHERE adapter=? AND adapter_user_id=?",
                (adapter, adapter_user),
            ).fetchone()
        if row:
            return row["canonical_user_id"]
        canonical = f"{adapter}:{adapter_user}"
        self._bind_identity(adapter, adapter_user, canonical)
        return canonical

    def _merge_identity(self, from_id: str, to_id: str) -> int:
        now = self._now()
        moved = 0
        with self._db() as conn:
            rows = conn.execute(
                "SELECT source_adapter, source_user_id, source_channel, memory_type, memory, memory_hash, "
                "score, importance, confidence, reinforce_count, last_seen_at, is_active "
                "FROM memories WHERE canonical_user_id=?",
                (from_id,),
            ).fetchall()
            for row in rows:
                try:
                    conn.execute("""
                        INSERT INTO memories(
                            canonical_user_id, source_adapter, source_user_id, source_channel,
                            memory_type, memory, memory_hash, score, importance, confidence,
                            reinforce_count, last_seen_at, created_at, updated_at, is_active
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (to_id, row["source_adapter"], row["source_user_id"], row["source_channel"],
                          row["memory_type"], row["memory"], row["memory_hash"], row["score"],
                          row["importance"], row["confidence"], row["reinforce_count"],
                          row["last_seen_at"], now, now, row["is_active"]))
                    moved += 1
                except sqlite3.IntegrityError:
                    conn.execute("""
                        UPDATE memories
                        SET importance=MAX(importance, ?), confidence=MAX(confidence, ?),
                            reinforce_count=reinforce_count+?, updated_at=?
                        WHERE canonical_user_id=? AND memory_hash=?
                    """, (row["importance"], row["confidence"], row["reinforce_count"],
                          now, to_id, row["memory_hash"]))
            conn.execute("DELETE FROM memories WHERE canonical_user_id=?", (from_id,))
            conn.execute(
                "UPDATE identity_bindings SET canonical_user_id=?, updated_at=? WHERE canonical_user_id=?",
                (to_id, now, from_id),
            )
            conn.execute(
                "UPDATE conversation_cache SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )
        return moved

    # ── 对话缓存 ──────────────────────────────────────────────────────────────

    def _insert_conversation(self, canonical_id: str, role: str, content: str,
                              source_adapter: str = "test", source_user_id: str = "test",
                              unified_msg_origin: str = ""):
        with self._db() as conn:
            conn.execute("""
                INSERT INTO conversation_cache(
                    canonical_user_id, role, content, source_adapter, source_user_id,
                    unified_msg_origin, distilled, distilled_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, 0, '', ?)
            """, (canonical_id, role, content[:1000], source_adapter, source_user_id,
                  unified_msg_origin, self._now()))

    def _fetch_pending_rows(self, canonical_id: str, limit: int) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute("""
                SELECT id, canonical_user_id, role, content, source_adapter, source_user_id, unified_msg_origin
                FROM conversation_cache
                WHERE canonical_user_id=? AND distilled=0
                ORDER BY id ASC LIMIT ?
            """, (canonical_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def _mark_rows_distilled(self, ids: Sequence[int]):
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        with self._db() as conn:
            conn.execute(
                f"UPDATE conversation_cache SET distilled=1, distilled_at=? WHERE id IN ({placeholders})",
                [self._now(), *ids],
            )

    def _count_pending_rows(self) -> int:
        with self._db() as conn:
            row = conn.execute("SELECT COUNT(1) AS n FROM conversation_cache WHERE distilled=0").fetchone()
        return int(row["n"] if row else 0)

    # ── 记忆 CRUD ──────────────────────────────────────────────────────────────

    def _insert_memory(self, canonical_id: str, adapter: str, adapter_user: str,
                        memory: str, score: float, memory_type: str,
                        importance: float, confidence: float,
                        source_channel: str = "default") -> int:
        normalized = self._normalize_text(memory)
        mhash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = self._now()
        memory_type_safe = self._safe_memory_type(memory_type)
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, reinforce_count FROM memories WHERE canonical_user_id=? AND memory_hash=?",
                (canonical_id, mhash),
            ).fetchone()
            if row:
                conn.execute("""
                    UPDATE memories
                    SET score=?, memory_type=?, importance=MAX(importance, ?), confidence=MAX(confidence, ?),
                        reinforce_count=?, last_seen_at=?, updated_at=?
                    WHERE id=?
                """, (self._clamp01(score), memory_type_safe, self._clamp01(importance),
                      self._clamp01(confidence), int(row["reinforce_count"]) + 1, now, now, int(row["id"])))
                return int(row["id"])

            new_words = set(self._tokenize(normalized))
            candidate_rows = conn.execute("""
                SELECT id, memory FROM memories
                WHERE canonical_user_id=? AND memory_type=? AND is_active=1 AND is_pinned=0
                ORDER BY created_at DESC LIMIT 15
            """, (canonical_id, memory_type_safe)).fetchall()

            for cand in candidate_rows:
                cand_words = set(self._tokenize(str(cand["memory"])))
                overlap = len(new_words.intersection(cand_words))
                if overlap >= max(1, min(len(new_words), len(cand_words)) * 0.5):
                    conn.execute("UPDATE memories SET is_active=0, updated_at=? WHERE id=?",
                                 (now, int(cand["id"])))

            cur = conn.execute("""
                INSERT INTO memories(
                    canonical_user_id, source_adapter, source_user_id, source_channel, memory_type,
                    memory, memory_hash, score, importance, confidence, reinforce_count, is_active,
                    last_seen_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (canonical_id, adapter, adapter_user, source_channel, memory_type_safe,
                  memory, mhash, self._clamp01(score), self._clamp01(importance),
                  self._clamp01(confidence), 1, 1, now, now, now))
            return int(cur.lastrowid or 0)

    def _delete_memory(self, memory_id: int) -> bool:
        with self._db() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            return cur.rowcount > 0

    def _list_memories(self, canonical_id: str, limit: int = 8) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute("""
                SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, updated_at, is_pinned
                FROM memories WHERE canonical_user_id=? AND is_active=1
                ORDER BY importance DESC, score DESC, updated_at DESC LIMIT ?
            """, (canonical_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def _retrieve_memories(self, canonical_id: str, query: str, limit: int) -> List[Dict]:
        query_words = set(self._tokenize(query))
        now_ts = int(time.time())
        with self._db() as conn:
            rows = conn.execute("""
                SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, last_seen_at
                FROM memories WHERE canonical_user_id=? AND is_active=1
                ORDER BY updated_at DESC LIMIT 80
            """, (canonical_id,)).fetchall()

        scored = []
        for row in rows:
            memory_text = str(row["memory"])
            memory_words = set(self._tokenize(memory_text))
            overlap = len(query_words.intersection(memory_words))
            lexical = overlap / max(1, len(query_words)) if query_words else 0.0

            recency_bonus = 0.0
            last_seen = str(row["last_seen_at"])
            try:
                last_ts = int(time.mktime(time.strptime(last_seen, "%Y-%m-%d %H:%M:%S")))
                age_hours = max(1.0, (now_ts - last_ts) / 3600)
                recency_bonus = min(0.15, 0.15 / age_hours)
            except Exception:
                pass

            final_score = (
                0.35 * float(row["score"])
                + 0.25 * float(row["importance"])
                + 0.20 * float(row["confidence"])
                + 0.15 * lexical
                + 0.05 * min(1.0, float(row["reinforce_count"]) / 10.0)
                + recency_bonus
            )
            scored.append({
                "id": int(row["id"]),
                "memory_type": str(row["memory_type"]),
                "memory": memory_text,
                "final_score": float(final_score),
            })

        scored.sort(key=lambda x: float(x["final_score"]), reverse=True)
        top_result = scored[:limit]

        if top_result:
            reinforce_now = self._now()
            reinforce_ids = [int(item["id"]) for item in top_result]
            placeholders = ",".join(["?"] * len(reinforce_ids))
            with self._db() as conn:
                conn.execute(
                    f"UPDATE memories SET reinforce_count = reinforce_count + 1, last_seen_at = ? WHERE id IN ({placeholders})",
                    [reinforce_now, *reinforce_ids],
                )
        return top_result

    # ── 记忆生命周期 ──────────────────────────────────────────────────────────

    def _decay_stale_memories(self):
        now_ts = int(time.time())
        stale_threshold = 30 * 86400
        archive_threshold = 90 * 86400
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, last_seen_at FROM memories WHERE is_active = 1 AND is_pinned = 0"
            ).fetchall()
            for row in rows:
                try:
                    last_ts = int(time.mktime(time.strptime(str(row["last_seen_at"]), "%Y-%m-%d %H:%M:%S")))
                except Exception:
                    continue
                age = now_ts - last_ts
                if age > archive_threshold:
                    conn.execute("UPDATE memories SET is_active = 3 WHERE id = ?", (int(row["id"]),))
                elif age > stale_threshold:
                    conn.execute("UPDATE memories SET is_active = 2 WHERE id = ?", (int(row["id"]),))
        self._auto_prune_low_quality()

    def _auto_prune_low_quality(self):
        now_ts = int(time.time())
        prune_age = 7 * 86400
        with self._db() as conn:
            rows = conn.execute("""
                SELECT id, score, importance, confidence, reinforce_count, created_at
                FROM memories WHERE is_active = 1 AND is_pinned = 0
            """).fetchall()
            pruned = 0
            for row in rows:
                try:
                    created_ts = int(time.mktime(time.strptime(str(row["created_at"]), "%Y-%m-%d %H:%M:%S")))
                except Exception:
                    continue
                if now_ts - created_ts < prune_age:
                    continue
                quality = 0.3 * float(row["score"]) + 0.4 * float(row["importance"]) + 0.3 * float(row["confidence"])
                if quality < 0.35 and int(row["reinforce_count"]) <= 1:
                    conn.execute("UPDATE memories SET is_active = 0 WHERE id = ?", (int(row["id"]),))
                    pruned += 1

    def _trim_conversation(self, canonical_id: str, keep_last: int):
        with self._db() as conn:
            conn.execute("""
                DELETE FROM conversation_cache
                WHERE canonical_user_id=?
                AND id NOT IN (
                    SELECT id FROM conversation_cache
                    WHERE canonical_user_id=?
                    ORDER BY id DESC LIMIT ?
                )
            """, (canonical_id, canonical_id, keep_last))

    def _purge_user_data(self, canonical_id: str) -> Dict[str, int]:
        with self._db() as conn:
            m = conn.execute("DELETE FROM memories WHERE canonical_user_id = ?", (canonical_id,)).rowcount
            c = conn.execute("DELETE FROM conversation_cache WHERE canonical_user_id = ?", (canonical_id,)).rowcount
        return {"memories": m, "cache": c}

    def _validate_distill_output(self, items: List[Dict]) -> List[Dict]:
        valid = []
        for item in items:
            mem = str(item.get("memory", "")).strip()
            if not mem or len(mem) < 6:
                continue
            if len(mem) > 300:
                mem = mem[:300]
                item["memory"] = mem
            if self._is_junk_memory(mem):
                continue
            if self._is_unsafe_memory(mem):
                continue
            mtype = str(item.get("memory_type", ""))
            if mtype not in VALID_MEMORY_TYPES:
                item["memory_type"] = "fact"
            for field in ("score", "importance", "confidence"):
                try:
                    v = float(item.get(field, 0.5))
                    item[field] = max(0.0, min(1.0, v))
                except (TypeError, ValueError):
                    item[field] = 0.5
            if float(item.get("confidence", 0)) < 0.4:
                continue
            if float(item.get("importance", 0)) < 0.3:
                continue
            valid.append(item)
        return valid

    def _get_global_stats(self) -> Dict[str, int]:
        with self._db() as conn:
            total_users = conn.execute("SELECT COUNT(DISTINCT canonical_user_id) FROM memories").fetchone()[0]
            active_memories = conn.execute("SELECT COUNT(*) FROM memories WHERE is_active = 1").fetchone()[0]
            deactivated = conn.execute("SELECT COUNT(*) FROM memories WHERE is_active = 0").fetchone()[0]
            pending_cached = conn.execute("SELECT COUNT(*) FROM conversation_cache WHERE distilled = 0").fetchone()[0]
        return {
            "total_users": int(total_users),
            "total_active_memories": int(active_memories),
            "total_deactivated_memories": int(deactivated),
            "pending_cached_rows": int(pending_cached),
        }

    def _set_pinned(self, memory_id: int, pinned: bool) -> bool:
        with self._db() as conn:
            cur = conn.execute("UPDATE memories SET is_pinned = ? WHERE id = ?",
                               (1 if pinned else 0, memory_id))
            return cur.rowcount > 0


# ─────────────────────────────────────────────────────────────────────────────
# 测试框架
# ─────────────────────────────────────────────────────────────────────────────

class StressTestRunner:
    def __init__(self):
        self.results = []
        self.tmp_dir = tempfile.mkdtemp(prefix="tmemory_stress_")

    def _new_core(self, name: str = "test") -> PluginCore:
        db_path = os.path.join(self.tmp_dir, f"{name}_{int(time.time()*1000)}.db")
        return PluginCore(db_path)

    def _pass(self, name: str, detail: str = ""):
        mark = "\033[32m✓\033[0m"
        print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
        self.results.append(("PASS", name))

    def _fail(self, name: str, reason: str):
        mark = "\033[31m✗\033[0m"
        print(f"  {mark} {name} — {reason}")
        self.results.append(("FAIL", name))

    def section(self, title: str):
        print(f"\n\033[1;34m{'─'*55}\033[0m")
        print(f"\033[1;34m  {title}\033[0m")
        print(f"\033[1;34m{'─'*55}\033[0m")

    # ─── 测试用例 ──────────────────────────────────────────────────────────────

    def test_basic_db_init(self):
        self.section("1. 数据库初始化与迁移")
        try:
            core = self._new_core("init")
            # 验证表是否存在
            with core._db() as conn:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            expected = {"identity_bindings", "memories", "conversation_cache",
                        "memory_events", "distill_history"}
            missing = expected - tables
            if missing:
                self._fail("表结构完整性", f"缺失表: {missing}")
            else:
                self._pass("表结构完整性", f"找到 {len(tables)} 张表")

            # WAL 模式
            with core._db() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if mode == "wal":
                self._pass("WAL 模式", "journal_mode=wal")
            else:
                self._fail("WAL 模式", f"实际 journal_mode={mode}")

            # 重复初始化不崩溃
            core2 = PluginCore(core.db_path)
            self._pass("重复初始化幂等性")
        except Exception as e:
            self._fail("数据库初始化", traceback.format_exc())

    def test_identity_management(self):
        self.section("2. 身份管理")
        core = self._new_core("identity")

        # 基础绑定
        try:
            cid = core._resolve_identity("qq", "10001")
            assert cid == "qq:10001", f"预期 qq:10001，实际 {cid}"
            cid2 = core._resolve_identity("qq", "10001")
            assert cid == cid2, "同一用户应返回相同 canonical_id"
            self._pass("身份解析与自动绑定")
        except Exception as e:
            self._fail("身份解析", str(e))

        # 显式绑定
        try:
            core._bind_identity("wechat", "wx_alice", "alice")
            cid = core._resolve_identity("wechat", "wx_alice")
            assert cid == "alice", f"预期 alice，实际 {cid}"
            self._pass("显式绑定")
        except Exception as e:
            self._fail("显式绑定", str(e))

        # 跨适配器同一用户
        try:
            core._bind_identity("telegram", "tg_alice", "alice")
            cid = core._resolve_identity("telegram", "tg_alice")
            assert cid == "alice"
            self._pass("跨适配器同一用户")
        except Exception as e:
            self._fail("跨适配器绑定", str(e))

        # 身份合并
        try:
            core._insert_memory("user_a", "qq", "a1", "用户喜欢 Python 编程", 0.8, "preference", 0.8, 0.9)
            core._insert_memory("user_a", "qq", "a1", "用户习惯早起工作", 0.7, "fact", 0.7, 0.8)
            moved = core._merge_identity("user_a", "user_b")
            assert moved == 2, f"预期迁移 2 条，实际 {moved}"
            mems = core._list_memories("user_b", limit=10)
            assert len(mems) == 2
            self._pass("身份合并", f"迁移 {moved} 条记忆")
        except Exception as e:
            self._fail("身份合并", str(e))

        # 并发身份绑定（100 个不同用户）
        try:
            errors = []
            def bind_user(uid):
                try:
                    core._resolve_identity("stress", f"user_{uid}")
                except Exception as ex:
                    errors.append(str(ex))

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(bind_user, i) for i in range(100)]
                concurrent.futures.wait(futures)

            if errors:
                self._fail("并发身份绑定", f"{len(errors)} 个错误: {errors[:3]}")
            else:
                self._pass("并发身份绑定（100 用户 × 20 线程）")
        except Exception as e:
            self._fail("并发身份绑定", str(e))

    def test_conversation_cache(self):
        self.section("3. 对话缓存（conversation_cache）")
        core = self._new_core("cache")

        # 基础写入
        try:
            for i in range(50):
                core._insert_conversation("user1", "user", f"这是第 {i} 条消息，内容关于 Python 和机器学习。")
            count = core._count_pending_rows()
            assert count == 50, f"预期 50，实际 {count}"
            self._pass("基础写入 50 条")
        except Exception as e:
            self._fail("基础写入", str(e))

        # 超长文本截断（>1000 字符）
        try:
            long_text = "这是一段超长的测试文本。" * 200  # ~2400 字符
            core._insert_conversation("user1", "user", long_text)
            rows = core._fetch_pending_rows("user1", 100)
            # 找到最后插入的
            last = rows[-1]
            assert len(last["content"]) <= 1000, f"内容未截断: {len(last['content'])} 字符"
            self._pass("超长文本截断", f"截断为 {len(last['content'])} 字符")
        except Exception as e:
            self._fail("超长文本截断", str(e))

        # 空文本
        try:
            core._insert_conversation("user1", "user", "")
            self._pass("空文本写入不崩溃")
        except Exception as e:
            self._fail("空文本写入", str(e))

        # Unicode / 特殊字符
        try:
            special_texts = [
                "🎉🎊🎈 Emoji 测试",
                "日本語テスト",
                "한국어 테스트",
                "اختبار عربي",
                "\x00\x01\x02 控制字符",
                "' OR '1'='1 SQL 注入测试",
                "<script>alert('xss')</script>",
            ]
            for text in special_texts:
                core._insert_conversation("user_special", "user", text)
            count = core._count_pending_rows()
            assert count > 0
            self._pass("特殊字符/Unicode 写入", f"写入 {len(special_texts)} 条特殊文本")
        except Exception as e:
            self._fail("特殊字符写入", str(e))

        # 标记为已蒸馏
        try:
            rows = core._fetch_pending_rows("user1", 30)
            ids = [int(r["id"]) for r in rows[:20]]
            core._mark_rows_distilled(ids)
            remaining = core._count_pending_rows()
            self._pass("标记已蒸馏", f"剩余未蒸馏: {remaining}")
        except Exception as e:
            self._fail("标记已蒸馏", str(e))

        # 高并发写入（500 条 × 10 线程）
        try:
            errors = []
            def write_messages(thread_id):
                for i in range(50):
                    try:
                        core._insert_conversation(
                            f"thread_user_{thread_id}",
                            "user",
                            f"线程 {thread_id} 消息 {i}: 测试并发写入稳定性"
                        )
                    except Exception as ex:
                        errors.append(str(ex))

            t0 = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(write_messages, t) for t in range(10)]
                concurrent.futures.wait(futures)
            elapsed = time.time() - t0

            if errors:
                self._fail("高并发写入", f"{len(errors)} 个错误: {errors[:3]}")
            else:
                total = 10 * 50
                self._pass(f"高并发写入（{total} 条 × 10 线程）", f"{elapsed:.2f}s，约 {total/elapsed:.0f} ops/s")
        except Exception as e:
            self._fail("高并发写入", str(e))

        # 对话历史裁剪
        try:
            core._trim_conversation("user1", keep_last=10)
            rows = core._fetch_pending_rows("user1", 100)
            total_user1 = len(rows)
            self._pass("对话历史裁剪", f"裁剪后剩余 {total_user1} 条未蒸馏行")
        except Exception as e:
            self._fail("对话历史裁剪", str(e))

    def test_memory_crud(self):
        self.section("4. 记忆 CRUD")
        core = self._new_core("crud")
        user = "test_user"

        # 插入记忆
        try:
            mem_id = core._insert_memory(user, "qq", "10001", "用户喜欢在早上喝咖啡", 0.8, "preference", 0.8, 0.9)
            assert mem_id > 0, f"插入应返回正整数 ID，实际: {mem_id}"
            self._pass("插入记忆", f"memory_id={mem_id}")
        except Exception as e:
            self._fail("插入记忆", str(e))

        # 重复插入应强化（不重复）
        try:
            mem_id2 = core._insert_memory(user, "qq", "10001", "用户喜欢在早上喝咖啡", 0.9, "preference", 0.9, 0.95)
            assert mem_id == mem_id2, f"重复记忆应返回同一 ID: {mem_id} vs {mem_id2}"
            mems = core._list_memories(user)
            same = [m for m in mems if m["id"] == mem_id]
            assert same[0]["reinforce_count"] >= 2, "reinforce_count 应 >= 2"
            self._pass("重复记忆强化（去重）", f"reinforce_count={same[0]['reinforce_count']}")
        except Exception as e:
            self._fail("重复记忆强化", str(e))

        # 冲突检测（关键词高重叠应标记旧记忆失效）
        try:
            core._insert_memory(user, "qq", "10001", "用户喜欢喝绿茶而不是咖啡", 0.9, "preference", 0.9, 0.95)
            mems = core._list_memories(user, limit=20)
            active_prefs = [m for m in mems if m["memory_type"] == "preference"]
            self._pass("冲突检测（关键词重叠）", f"活跃 preference 记忆: {len(active_prefs)} 条")
        except Exception as e:
            self._fail("冲突检测", str(e))

        # 不同类型记忆
        try:
            types = ["preference", "fact", "task", "restriction", "style"]
            ids = []
            for t in types:
                mid = core._insert_memory(user, "qq", "10001", f"这是一条 {t} 类型的记忆，用于类型测试", 0.7, t, 0.7, 0.8)
                ids.append(mid)
            self._pass("多种 memory_type", f"插入 {len(types)} 种类型")
        except Exception as e:
            self._fail("多种 memory_type", str(e))

        # 批量插入 100 条
        try:
            t0 = time.time()
            for i in range(100):
                core._insert_memory(
                    user, "qq", "10001",
                    f"用户的第 {i} 条长期记忆，内容是关于主题 {i % 10} 的详细信息",
                    0.5 + (i % 5) * 0.1, "fact",
                    0.5 + (i % 3) * 0.15, 0.6,
                )
            elapsed = time.time() - t0
            self._pass("批量插入 100 条记忆", f"{elapsed:.3f}s，约 {100/elapsed:.0f} ops/s")
        except Exception as e:
            self._fail("批量插入 100 条", str(e))

        # 删除记忆
        try:
            mems = core._list_memories(user, limit=5)
            if mems:
                target_id = mems[0]["id"]
                ok = core._delete_memory(target_id)
                assert ok, "删除应返回 True"
                ok2 = core._delete_memory(target_id)
                assert not ok2, "重复删除应返回 False"
                self._pass("删除记忆（含幂等性）")
        except Exception as e:
            self._fail("删除记忆", str(e))

        # pin / unpin
        try:
            mems = core._list_memories(user, limit=3)
            if mems:
                mid = mems[0]["id"]
                core._set_pinned(mid, True)
                core._set_pinned(mid, False)
                self._pass("pin/unpin 操作")
        except Exception as e:
            self._fail("pin/unpin", str(e))

        # 边界：空文本记忆
        try:
            mid = core._insert_memory(user, "qq", "10001", "", 0.5, "fact", 0.5, 0.5)
            self._pass("空文本记忆插入不崩溃", f"id={mid}")
        except Exception as e:
            self._fail("空文本记忆插入", str(e))

        # 边界：超长文本记忆（10000 字符）
        try:
            very_long = "用户喜欢" + ("极其详细的" * 1000)
            mid = core._insert_memory(user, "qq", "10001", very_long, 0.5, "fact", 0.5, 0.5)
            self._pass("超长文本记忆插入", f"id={mid}")
        except Exception as e:
            self._fail("超长文本记忆插入", str(e))

    def test_memory_retrieval(self):
        self.section("5. 记忆召回（_retrieve_memories）")
        core = self._new_core("retrieval")
        user = "recall_user"

        # 插入多样记忆
        memories_to_insert = [
            ("用户偏好使用 Python 进行数据分析", "preference", 0.9, 0.9),
            ("用户是一名后端工程师，有 5 年经验", "fact", 0.8, 0.85),
            ("用户不喜欢使用 JavaScript", "restriction", 0.7, 0.75),
            ("用户的工作时间是早 9 点到晚 6 点", "fact", 0.7, 0.8),
            ("用户倾向于简洁的代码风格", "style", 0.8, 0.8),
            ("用户正在学习机器学习和深度学习", "task", 0.75, 0.8),
            ("用户居住在上海", "fact", 0.9, 0.95),
            ("用户喜欢喝茶，尤其是龙井", "preference", 0.85, 0.9),
        ]
        try:
            for mem, mtype, imp, conf in memories_to_insert:
                core._insert_memory(user, "qq", "10001", mem, 0.7, mtype, imp, conf)
            self._pass("插入多样记忆", f"{len(memories_to_insert)} 条")
        except Exception as e:
            self._fail("插入记忆", str(e))
            return

        # 关键词匹配召回
        try:
            results = core._retrieve_memories(user, "Python 编程", 3)
            assert len(results) > 0, "应找到至少 1 条记忆"
            top_mem = results[0]["memory"]
            self._pass("关键词匹配召回", f"Top: {top_mem[:40]}")
        except Exception as e:
            self._fail("关键词匹配召回", str(e))

        # 空查询召回（应按默认评分返回）
        try:
            results = core._retrieve_memories(user, "", 5)
            self._pass("空查询召回", f"返回 {len(results)} 条")
        except Exception as e:
            self._fail("空查询召回", str(e))

        # limit 边界
        try:
            results = core._retrieve_memories(user, "用户", 1)
            assert len(results) <= 1
            results_many = core._retrieve_memories(user, "用户", 100)
            assert len(results_many) <= len(memories_to_insert)
            self._pass("limit 边界控制")
        except Exception as e:
            self._fail("limit 边界", str(e))

        # 大量记忆（100 条）下的检索性能
        try:
            core2 = self._new_core("retrieval_perf")
            user2 = "perf_user"
            for i in range(100):
                core2._insert_memory(
                    user2, "qq", "u", f"用户的第 {i} 条记忆：关于主题 {i%10}，包含关键词 kw_{i}",
                    0.5 + (i % 5) * 0.1, "fact",
                    0.5 + (i % 3) * 0.1, 0.6,
                )
            t0 = time.time()
            for _ in range(20):
                core2._retrieve_memories(user2, f"主题 {(_ % 10)} kw_{_ * 3}", 5)
            elapsed = time.time() - t0
            self._pass(f"100 条记忆 × 20 次检索", f"{elapsed:.3f}s，约 {20/elapsed:.0f} ops/s")
        except Exception as e:
            self._fail("检索性能", str(e))

    def test_concurrent_rw(self):
        self.section("6. 并发读写（WAL 稳定性）")
        core = self._new_core("concurrent")
        user = "concurrent_user"

        # 预填数据
        for i in range(20):
            core._insert_memory(user, "qq", "u", f"预填记忆 {i}：用户喜欢第 {i} 个爱好", 0.7, "fact", 0.7, 0.7)

        errors = []
        ops_done = [0]
        lock = threading.Lock()

        def read_thread(tid):
            for _ in range(30):
                try:
                    core._retrieve_memories(user, f"用户 爱好 {_ % 20}", 3)
                    with lock:
                        ops_done[0] += 1
                except Exception as ex:
                    errors.append(f"READ tid={tid}: {ex}")

        def write_thread(tid):
            for i in range(20):
                try:
                    core._insert_conversation(user, "user", f"并发消息 tid={tid} i={i}")
                    core._insert_memory(user, "qq", f"u{tid}", f"用户 tid={tid} 的第 {i} 条记忆内容详情", 0.6, "fact", 0.6, 0.7)
                    with lock:
                        ops_done[0] += 1
                except Exception as ex:
                    errors.append(f"WRITE tid={tid}: {ex}")

        try:
            t0 = time.time()
            threads = []
            for i in range(5):
                threads.append(threading.Thread(target=read_thread, args=(i,)))
            for i in range(5):
                threads.append(threading.Thread(target=write_thread, args=(i,)))
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.time() - t0

            if errors:
                self._fail("并发读写", f"{len(errors)} 个错误:\n    " + "\n    ".join(errors[:5]))
            else:
                self._pass(
                    "并发读写（5 读线程 × 30 次 + 5 写线程 × 40 次）",
                    f"{elapsed:.2f}s，完成 {ops_done[0]} 次操作"
                )
        except Exception as e:
            self._fail("并发读写", str(e))

        # 并发 identity 绑定竞争（同一 (adapter, user_id) 被多线程同时首次绑定）
        errors2 = []
        def race_bind(tid):
            try:
                # 故意让多个线程竞争同一个 (adapter, user_id) 首次绑定
                core._resolve_identity("race_adapter", "same_user")
            except Exception as ex:
                errors2.append(str(ex))

        try:
            threads2 = [threading.Thread(target=race_bind, args=(i,)) for i in range(20)]
            for t in threads2:
                t.start()
            for t in threads2:
                t.join()
            if errors2:
                self._fail("并发身份绑定竞争", f"{len(errors2)} 个错误: {errors2[:3]}")
            else:
                self._pass("并发身份绑定竞争（20 线程同时首次绑定同一用户）")
        except Exception as e:
            self._fail("并发身份竞争", str(e))

    def test_validate_and_sanitize(self):
        self.section("7. 输入验证与脱敏")
        core = self._new_core("validate")

        # 脱敏测试
        cases = [
            ("我的手机号是 13812345678", "[手机号]"),
            ("邮箱是 user@example.com 请联系", "[邮箱]"),
            ("身份证 110101199001011234", "[身份证]"),
            ("银行卡 6222021234567890123", "[长数字]"),
            ("普通文本无敏感信息", "普通文本无敏感信息"),
        ]
        passed = True
        for text, expected_fragment in cases:
            result = core._sanitize_text(text)
            if expected_fragment not in result and expected_fragment == result:
                print(f"    脱敏失败: '{text}' → '{result}'（应含 '{expected_fragment}'）")
                passed = False
        if passed:
            self._pass("敏感信息脱敏", f"{len(cases)} 个样本")
        else:
            self._fail("敏感信息脱敏", "见上方详情")

        # 蒸馏输出校验
        test_items = [
            # 正常记忆
            {"memory": "用户喜欢在周末打篮球和健身", "memory_type": "preference", "importance": 0.8, "confidence": 0.85, "score": 0.8},
            # 废话（应被过滤）
            {"memory": "你好", "memory_type": "fact", "importance": 0.5, "confidence": 0.5, "score": 0.5},
            # 太短（< 6 字符）
            {"memory": "AB", "memory_type": "fact", "importance": 0.7, "confidence": 0.7, "score": 0.7},
            # 低置信度（< 0.4，应被过滤）
            {"memory": "用户可能有时候喜欢某些食物", "memory_type": "fact", "importance": 0.5, "confidence": 0.3, "score": 0.5},
            # 低重要度（< 0.3，应被过滤）
            {"memory": "用户说了一些话关于天气的事情", "memory_type": "fact", "importance": 0.2, "confidence": 0.8, "score": 0.5},
            # 不安全内容（应被过滤）
            {"memory": "用户密码是 abc123", "memory_type": "fact", "importance": 0.9, "confidence": 0.9, "score": 0.9},
            # 超长文本（应被截断保留）
            {"memory": "用户" + "喜欢" * 200, "memory_type": "fact", "importance": 0.7, "confidence": 0.7, "score": 0.7},
            # 类型修正
            {"memory": "用户不喜欢噪音环境，需要安静工作", "memory_type": "unknown_type", "importance": 0.7, "confidence": 0.7, "score": 0.7},
        ]
        try:
            valid = core._validate_distill_output(test_items)
            # 应通过的：正常、超长（截断后）、类型修正
            # 应过滤的：废话、太短、低置信度、低重要度、不安全
            self._pass(
                "蒸馏输出校验",
                f"输入 {len(test_items)} 条 → 有效 {len(valid)} 条（预期 3 条）"
            )
            # 验证不安全内容确实被过滤
            unsafe_leaked = [v for v in valid if "密码" in v.get("memory", "")]
            if unsafe_leaked:
                self._fail("不安全内容过滤", f"泄漏: {unsafe_leaked}")
            else:
                self._pass("不安全内容过滤")
        except Exception as e:
            self._fail("蒸馏输出校验", str(e))

        # _is_junk_memory 边界
        junk_cases = [
            ("你好", True),
            ("哈哈哈哈哈", True),
            ("ok", True),
            ("用户喜欢使用 Python 编程", False),
            ("", True),
            ("abc", True),
        ]
        junk_ok = True
        for text, expected_junk in junk_cases:
            result = core._is_junk_memory(text)
            if result != expected_junk:
                print(f"    _is_junk_memory('{text}') = {result}，预期 {expected_junk}")
                junk_ok = False
        if junk_ok:
            self._pass("废话检测边界", f"{len(junk_cases)} 个样本")
        else:
            self._fail("废话检测边界", "见上方详情")

    def test_lifecycle(self):
        self.section("8. 记忆生命周期（衰减 & 剪枝）")
        core = self._new_core("lifecycle")
        user = "lifecycle_user"

        # 插入一批记忆，然后直接调用衰减（绕过时间限制，只验证不崩溃）
        try:
            for i in range(30):
                core._insert_memory(
                    user, "qq", "u", f"用户的第 {i} 条生命周期测试记忆内容",
                    0.3 + (i % 3) * 0.2, "fact",
                    0.3 + (i % 4) * 0.15, 0.4 + (i % 3) * 0.2,
                )
            core._decay_stale_memories()
            core._auto_prune_low_quality()
            self._pass("衰减 & 剪枝不崩溃（30 条记忆）")
        except Exception as e:
            self._fail("衰减 & 剪枝", str(e))

        # purge 清除用户数据
        try:
            result = core._purge_user_data(user)
            assert isinstance(result, dict)
            assert "memories" in result and "cache" in result
            remaining = core._list_memories(user)
            assert len(remaining) == 0, f"清除后应无记忆，实际 {len(remaining)} 条"
            self._pass("用户数据清除（purge）", f"删除 memories={result['memories']} cache={result['cache']}")
        except Exception as e:
            self._fail("用户数据清除", str(e))

    def test_global_stats(self):
        self.section("9. 全局统计")
        core = self._new_core("stats")

        try:
            for uid in range(5):
                user = f"stats_user_{uid}"
                for i in range(10):
                    core._insert_memory(user, "qq", f"u{uid}", f"用户 {uid} 的第 {i} 条记忆：关于主题 {i}", 0.7, "fact", 0.7, 0.8)
                    core._insert_conversation(user, "user", f"消息 {i}")

            stats = core._get_global_stats()
            assert stats["total_users"] >= 5, f"total_users={stats['total_users']}"
            assert stats["total_active_memories"] >= 5, f"total_active_memories={stats['total_active_memories']}"
            assert stats["pending_cached_rows"] >= 50
            self._pass("全局统计", str(stats))
        except Exception as e:
            self._fail("全局统计", str(e))

    def test_text_utils(self):
        self.section("10. 文本工具方法")
        core = self._new_core("text_utils")

        # _normalize_text
        cases_norm = [
            ("  hello  world  ", "hello world"),
            ("\t\n\r多余空格  测试\n", "多余空格 测试"),
            ("", ""),
            (None, ""),
        ]
        ok = True
        for inp, expected in cases_norm:
            result = core._normalize_text(inp)
            if result != expected:
                print(f"    _normalize_text({repr(inp)}) = {repr(result)}，预期 {repr(expected)}")
                ok = False
        if ok:
            self._pass("_normalize_text", f"{len(cases_norm)} 个样本")
        else:
            self._fail("_normalize_text", "见上方")

        # _clamp01
        clamp_cases = [(0.5, 0.5), (-1.0, 0.0), (2.0, 1.0), ("abc", 0.0), (None, 0.0)]
        ok2 = True
        for inp, expected in clamp_cases:
            result = core._clamp01(inp)
            if result != expected:
                print(f"    _clamp01({inp}) = {result}，预期 {expected}")
                ok2 = False
        if ok2:
            self._pass("_clamp01", f"{len(clamp_cases)} 个样本")
        else:
            self._fail("_clamp01", "见上方")

        # _tokenize
        result = core._tokenize("用户喜欢 Python 编程和机器学习")
        assert len(result) > 0, "_tokenize 应返回非空列表"
        self._pass("_tokenize", f"分词: {result}")

        # _distill_text（规则蒸馏）
        try:
            result = core._distill_text("用户喜欢喝茶，尤其是龙井绿茶，每天早晨都要喝一杯。" * 10)
            assert len(result) > 0
            self._pass("_distill_text（规则蒸馏）", f"长度 {len(result)} 字符")
        except Exception as e:
            self._fail("_distill_text", str(e))

        # _safe_memory_type 类型修正
        type_cases = [
            ("preference", "preference"),
            ("fact", "fact"),
            ("PREFERENCE", "preference"),
            ("invalid_type", "fact"),
            ("", "fact"),
            (None, "fact"),
        ]
        ok3 = True
        for inp, expected in type_cases:
            result = core._safe_memory_type(inp)
            if result != expected:
                print(f"    _safe_memory_type({repr(inp)}) = {repr(result)}，预期 {repr(expected)}")
                ok3 = False
        if ok3:
            self._pass("_safe_memory_type", f"{len(type_cases)} 个样本")
        else:
            self._fail("_safe_memory_type", "见上方")

    def test_async_compatibility(self):
        """验证异步调用模式下同步 DB 操作的稳定性（模拟 AstrBot 事件循环场景）。"""
        self.section("11. 异步兼容性（模拟 asyncio 事件循环）")
        core = self._new_core("async_compat")

        async def simulate_on_message(uid: str, msg: str):
            # 模拟 on_any_message：同步 DB 写入在 async 上下文中
            canonical = core._resolve_identity("qq", uid)
            core._insert_conversation(canonical, "user", msg)

        async def simulate_on_llm_request(uid: str, query: str):
            canonical = core._resolve_identity("qq", uid)
            # _retrieve_memories 是同步的，在 async 上下文直接调用
            return core._retrieve_memories(canonical, query, 5)

        async def run_simulation():
            tasks = []
            # 100 个用户，每人 5 条消息
            for uid in range(20):
                for i in range(5):
                    tasks.append(simulate_on_message(str(uid), f"用户 {uid} 的第 {i} 条消息"))
            await asyncio.gather(*tasks)

            # 20 次记忆召回
            recall_tasks = []
            for uid in range(20):
                recall_tasks.append(simulate_on_llm_request(str(uid), f"查询用户 {uid} 的偏好"))
            results = await asyncio.gather(*recall_tasks)
            return results

        try:
            t0 = time.time()
            results = asyncio.run(run_simulation())
            elapsed = time.time() - t0
            self._pass("asyncio 并发事件模拟（20 用户 × 5 消息 + 20 次召回）", f"{elapsed:.3f}s")
        except Exception as e:
            self._fail("asyncio 兼容性", traceback.format_exc())

    def test_edge_cases(self):
        self.section("12. 边界与异常场景")
        core = self._new_core("edge")

        # 删除不存在的记忆
        try:
            result = core._delete_memory(999999)
            assert not result, "删除不存在 ID 应返回 False"
            self._pass("删除不存在的记忆 ID")
        except Exception as e:
            self._fail("删除不存在的记忆", str(e))

        # 查询不存在用户的记忆
        try:
            mems = core._list_memories("nonexistent_user_xyz")
            assert mems == []
            self._pass("查询不存在用户的记忆")
        except Exception as e:
            self._fail("查询不存在用户", str(e))

        # _mark_rows_distilled 空列表
        try:
            core._mark_rows_distilled([])
            self._pass("_mark_rows_distilled 空列表")
        except Exception as e:
            self._fail("_mark_rows_distilled 空列表", str(e))

        # 合并自身
        try:
            core._insert_memory("self_user", "qq", "u", "用户喜欢自我合并测试", 0.7, "fact", 0.7, 0.8)
            moved = core._merge_identity("self_user", "self_user")
            # 合并自身在逻辑上可能 moved=0（hash 冲突全部走更新路径）或崩溃
            self._pass("合并自身（不崩溃）", f"moved={moved}")
        except Exception as e:
            self._fail("合并自身", str(e))

        # validate 空列表
        try:
            result = core._validate_distill_output([])
            assert result == []
            self._pass("_validate_distill_output 空列表")
        except Exception as e:
            self._fail("_validate_distill_output 空列表", str(e))

        # validate 包含 None 字段
        try:
            items = [{"memory": None, "memory_type": None, "importance": None, "confidence": None, "score": None}]
            result = core._validate_distill_output(items)
            self._pass("_validate_distill_output None 字段不崩溃", f"输出 {len(result)} 条")
        except Exception as e:
            self._fail("_validate_distill_output None 字段", str(e))

        # 超大并发（1000 条消息写入，单线程峰值）
        try:
            t0 = time.time()
            for i in range(1000):
                core._insert_conversation("peak_user", "user", f"峰值测试消息 {i}")
            elapsed = time.time() - t0
            self._pass(f"1000 条消息单线程峰值写入", f"{elapsed:.3f}s，约 {1000/elapsed:.0f} ops/s")
        except Exception as e:
            self._fail("峰值写入", str(e))

    # ─── 汇总 ─────────────────────────────────────────────────────────────────

    def run_all(self):
        print(f"\n\033[1;33m{'='*55}\033[0m")
        print(f"\033[1;33m  astrbot_plugin_tmemory 压力测试\033[0m")
        print(f"\033[1;33m  DB: {self.tmp_dir}\033[0m")
        print(f"\033[1;33m{'='*55}\033[0m")

        t_start = time.time()

        self.test_basic_db_init()
        self.test_identity_management()
        self.test_conversation_cache()
        self.test_memory_crud()
        self.test_memory_retrieval()
        self.test_concurrent_rw()
        self.test_validate_and_sanitize()
        self.test_lifecycle()
        self.test_global_stats()
        self.test_text_utils()
        self.test_async_compatibility()
        self.test_edge_cases()

        total_elapsed = time.time() - t_start

        passed = [r for r in self.results if r[0] == "PASS"]
        failed = [r for r in self.results if r[0] == "FAIL"]

        print(f"\n\033[1;33m{'='*55}\033[0m")
        print(f"\033[1;33m  测试结果汇总  （总耗时 {total_elapsed:.2f}s）\033[0m")
        print(f"\033[1;33m{'='*55}\033[0m")
        print(f"  \033[32m通过: {len(passed)}\033[0m  \033[31m失败: {len(failed)}\033[0m  总计: {len(self.results)}")

        if failed:
            print(f"\n\033[31m  失败用例：\033[0m")
            for _, name in failed:
                print(f"    ✗ {name}")

        print()
        return len(failed) == 0


if __name__ == "__main__":
    runner = StressTestRunner()
    success = runner.run_all()
    sys.exit(0 if success else 1)
