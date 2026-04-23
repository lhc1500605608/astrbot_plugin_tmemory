import json
import logging
import sqlite3
import hashlib
from typing import Dict, Any, List, Optional
import jieba

logger = logging.getLogger("astrbot:db.py")

class MemoryOps:
    """记忆相关数据库操作与净化逻辑。"""
    
    def __init__(self, plugin):
        self.plugin = plugin

    def insert_memory(
        self,
        canonical_id: str,
        adapter: str,
        adapter_user: str,
        memory: str,
        score: float,
        memory_type: str,
        importance: float,
        confidence: float,
        source_channel: str = "default",
        persona_id: str = "",
        scope: str = "user",
    ) -> int:
        normalized = self.plugin._normalize_text(memory)
        mhash = hashlib.sha256(
            f"{persona_id}:{scope}:{normalized}".encode("utf-8")
        ).hexdigest()
        now = self.plugin._now()
        memory_type_safe = self.plugin._safe_memory_type(memory_type)
        tokenized_memory = " ".join(jieba.cut_for_search(memory))
        with self.plugin._db() as conn:
            row = conn.execute(
                "SELECT id, reinforce_count FROM memories WHERE canonical_user_id=? AND memory_hash=?",
                (canonical_id, mhash),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE memories
                    SET score=?, memory_type=?, importance=MAX(importance, ?), confidence=MAX(confidence, ?),
                        reinforce_count=?, last_seen_at=?, updated_at=?, tokenized_memory=?
                    WHERE id=?
                    """,
                    (
                        self.plugin._clamp01(score),
                        memory_type_safe,
                        self.plugin._clamp01(importance),
                        self.plugin._clamp01(confidence),
                        int(row["reinforce_count"]) + 1,
                        now,
                        now,
                        tokenized_memory,
                        int(row["id"]),
                    ),
                )
                return int(row["id"])

            new_words = set(self.plugin._retrieval_mgr.tokenize(normalized))
            candidate_rows = conn.execute(
                """
                SELECT id, memory, confidence FROM memories
                WHERE canonical_user_id=? AND memory_type=? AND is_active=1 AND is_pinned=0
                ORDER BY created_at DESC
                LIMIT 15
                """,
                (canonical_id, memory_type_safe),
            ).fetchall()

            deactivated = 0
            for cand in candidate_rows:
                cand_words = set(self.plugin._retrieval_mgr.tokenize(str(cand["memory"])))
                overlap = len(new_words.intersection(cand_words))
                if overlap >= max(1, min(len(new_words), len(cand_words)) * 0.5):
                    cand_conf = float(cand.get("confidence", 0.0) if hasattr(cand, "get") else cand["confidence"])
                    if confidence >= cand_conf:
                        conn.execute(
                            """
                            UPDATE memories SET is_active=0, updated_at=? WHERE id=?
                            """,
                            (now, int(cand["id"])),
                        )
                        deactivated += 1

            cur = conn.execute(
                """
                INSERT INTO memories(
                    canonical_user_id, source_adapter, source_user_id, source_channel, memory_type,
                    memory, tokenized_memory, memory_hash, score, importance, confidence, reinforce_count, is_active,
                    last_seen_at, created_at, updated_at, persona_id, scope
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    adapter,
                    adapter_user,
                    source_channel,
                    memory_type_safe,
                    memory,
                    tokenized_memory,
                    mhash,
                    self.plugin._clamp01(score),
                    self.plugin._clamp01(importance),
                    self.plugin._clamp01(confidence),
                    1,
                    1,
                    now,
                    now,
                    now,
                    persona_id,
                    scope,
                ),
            )
            new_id = int(cur.lastrowid or 0)

            if deactivated > 0:
                self.plugin._memory_logger.log_memory_event(
                    canonical_user_id=canonical_id,
                    event_type="create_with_conflict",
                    payload={
                        "new_memory_id": new_id,
                        "memory_type": memory_type_safe,
                        "deactivated_count": deactivated,
                    },
                    conn=conn,
                )
            else:
                self.plugin._memory_logger.log_memory_event(
                    canonical_user_id=canonical_id,
                    event_type="create",
                    payload={
                        "memory_id": new_id,
                        "memory_type": memory_type_safe,
                    },
                    conn=conn,
                )

            return new_id

    async def manual_purify_memories(
        self,
        event,
        canonical_id: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
    ) -> dict:
        rows = self.plugin._list_memories_for_purify(
            canonical_id, limit=limit, include_pinned=include_pinned
        )
        if not rows:
            return {"updates": 0, "adds": 0, "deletes": 0, "note": "no memories"}

        operations = await self.plugin._llm_purify_operations(
            event, rows, mode, extra_instruction
        )
        updates = operations.get("updates", []) if isinstance(operations, dict) else []
        adds = operations.get("adds", []) if isinstance(operations, dict) else []
        deletes = operations.get("deletes", []) if isinstance(operations, dict) else []

        valid_ids = {int(r["id"]) for r in rows}
        updates = [u for u in updates if int(u.get("id", -1)) in valid_ids]
        deletes = [d for d in deletes if int(d) in valid_ids]

        if dry_run:
            return {
                "updates": len(updates),
                "adds": len(adds),
                "deletes": len(deletes),
                "note": "dry-run: no db changes",
                "ops": {"updates": updates, "adds": adds, "deletes": deletes},
            }

        now = self.plugin._now()
        up_count = 0
        add_count = 0
        del_count = 0

        with self.plugin._db() as conn:
            for u in updates:
                mem_id = int(u["id"])
                new_mem = str(u.get("memory", ""))
                if not new_mem:
                    continue
                score = self.plugin._clamp01(u.get("score", 0.5))
                mtype = self.plugin._safe_memory_type(u.get("memory_type", "fact"))
                imp = self.plugin._clamp01(u.get("importance", 0.5))
                conf = self.plugin._clamp01(u.get("confidence", 0.5))
                mhash = hashlib.sha256(self.plugin._normalize_text(new_mem).encode("utf-8")).hexdigest()
                tokenized = " ".join(jieba.cut_for_search(new_mem))
                conn.execute(
                    """
                    UPDATE memories 
                    SET memory=?, tokenized_memory=?, memory_hash=?, memory_type=?, score=?, importance=?, confidence=?, updated_at=?
                    WHERE id=?
                    """,
                    (new_mem, tokenized, mhash, mtype, score, imp, conf, now, mem_id),
                )
                up_count += 1

            for a in adds:
                new_mem = str(a.get("memory", ""))
                if not new_mem:
                    continue
                score = self.plugin._clamp01(a.get("score", 0.5))
                mtype = self.plugin._safe_memory_type(a.get("memory_type", "fact"))
                imp = self.plugin._clamp01(a.get("importance", 0.5))
                conf = self.plugin._clamp01(a.get("confidence", 0.5))
                mhash = hashlib.sha256(self.plugin._normalize_text(new_mem).encode("utf-8")).hexdigest()
                tokenized = " ".join(jieba.cut_for_search(new_mem))
                conn.execute(
                    """
                    INSERT INTO memories(
                        canonical_user_id, source_adapter, source_user_id, source_channel, memory_type,
                        memory, tokenized_memory, memory_hash, score, importance, confidence, reinforce_count, is_active,
                        last_seen_at, created_at, updated_at, persona_id, scope
                    ) VALUES(?, 'system', 'purify', 'purify', ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, '', 'user')
                    """,
                    (canonical_id, mtype, new_mem, tokenized, mhash, score, imp, conf, now, now, now),
                )
                add_count += 1

            if deletes:
                placeholders = ",".join("?" * len(deletes))
                conn.execute(
                    f"UPDATE memories SET is_active=0, updated_at=? WHERE id IN ({placeholders})",
                    [now] + deletes,
                )
                del_count = len(deletes)

            conn.execute(
                "DELETE FROM conversation_cache WHERE canonical_user_id=?",
                (canonical_id,),
            )

        if up_count or add_count or del_count:
            self.plugin._memory_logger.log_memory_event(
                canonical_user_id=canonical_id,
                event_type="purify",
                payload={"updates": up_count, "adds": add_count, "deletes": del_count},
            )

        return {
            "updates": up_count,
            "adds": add_count,
            "deletes": del_count,
            "note": "success",
        }
