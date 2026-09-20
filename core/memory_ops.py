"""Memory write/refine operations (MemoryOps facade).

Refactored per ADR-009 (module boundary split): distillation methods live in
``core/distill_ops.DistillOpsMixin``, profile-item operations in
``core/profile_ops.ProfileItemOps`` and the audit-event helper in
``core/memory_events``. This module keeps ``MemoryOps``' public surface intact
via inheritance and re-exports for backward compatibility.
"""

import hashlib
import logging

import jieba

from .distill_ops import DistillOpsMixin
from .memory_events import log_memory_event
from .profile_ops import ProfileItemOps

logger = logging.getLogger("astrbot:db.py")

__all__ = ["MemoryOps", "ProfileItemOps", "log_memory_event", "update_memory_full"]


class MemoryOps(DistillOpsMixin):
    """记忆相关数据库操作与净化逻辑。

    Distillation methods are provided by :class:`~core.distill_ops.DistillOpsMixin`.
    """

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
        summary_channel = "persona" if memory_type_safe == "style" else "canonical"
        tokenized_memory = " ".join(jieba.cut_for_search(memory))
        with self.plugin._db() as conn:
            row = conn.execute(
                "SELECT id, reinforce_count, attention_score FROM memories WHERE canonical_user_id=? AND memory_hash=?",
                (canonical_id, mhash),
            ).fetchone()
            if row:
                existing_attn = float(row["attention_score"] or 0.5)
                conn.execute(
                    """
                    UPDATE memories
                    SET score=?, memory_type=?, importance=MAX(importance, ?), confidence=MAX(confidence, ?),
                        reinforce_count=?, attention_score=?, last_seen_at=?, updated_at=?, tokenized_memory=?
                    WHERE id=?
                    """,
                    (
                        self.plugin._clamp01(score),
                        memory_type_safe,
                        self.plugin._clamp01(importance),
                        self.plugin._clamp01(confidence),
                        int(row["reinforce_count"]) + 1,
                        min(1.0, existing_attn + 0.05),
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
                SELECT id, memory FROM memories
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
                    summary_channel, memory, tokenized_memory, memory_hash, score, importance, confidence, reinforce_count, attention_score, is_active,
                    last_seen_at, created_at, updated_at, persona_id, scope
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    adapter,
                    adapter_user,
                    source_channel,
                    memory_type_safe,
                    summary_channel,
                    memory,
                    tokenized_memory,
                    mhash,
                    self.plugin._clamp01(score),
                    self.plugin._clamp01(importance),
                    self.plugin._clamp01(confidence),
                    1,
                    self.plugin._clamp01(importance),
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
        note = str(operations.get("note", "")) if isinstance(operations, dict) else ""

        pinned_ids = {int(r["id"]) for r in rows if int(r["is_pinned"]) == 1}
        if not include_pinned:
            updates = [u for u in updates if int(u.get("id", 0)) not in pinned_ids]
            deletes = [d for d in deletes if int(d) not in pinned_ids]

        if dry_run:
            return {
                "updates": len(updates),
                "adds": len(adds),
                "deletes": len(deletes),
                "note": f"dry_run preview. {note}",
            }

        applied_updates = applied_adds = applied_deletes = 0

        for upd in updates:
            try:
                mem_id = int(upd.get("id", 0))
                if not mem_id:
                    continue
                memory = self.plugin._sanitize_text(
                    self.plugin._normalize_text(str(upd.get("memory", "")))
                )
                if not memory:
                    continue
                self.plugin._update_memory_full(
                    mem_id,
                    memory=memory,
                    memory_type=self.plugin._safe_memory_type(upd.get("memory_type", "fact")),
                    score=self.plugin._clamp01(upd.get("score", 0.7)),
                    importance=self.plugin._clamp01(upd.get("importance", 0.6)),
                    confidence=self.plugin._clamp01(upd.get("confidence", 0.7)),
                )
                if self.plugin._vec_available:
                    await self.plugin._upsert_vector(mem_id, memory)
                applied_updates += 1
            except Exception as e:
                logger.debug("[tmemory] apply update failed: %s", e)

        for add in adds:
            try:
                memory = self.plugin._sanitize_text(
                    self.plugin._normalize_text(str(add.get("memory", "")))
                )
                if not memory:
                    continue
                new_id = self.plugin._insert_memory(
                    canonical_id=canonical_id,
                    adapter="manual_purify",
                    adapter_user=canonical_id,
                    memory=memory,
                    score=self.plugin._clamp01(add.get("score", 0.7)),
                    memory_type=self.plugin._safe_memory_type(add.get("memory_type", "fact")),
                    importance=self.plugin._clamp01(add.get("importance", 0.6)),
                    confidence=self.plugin._clamp01(add.get("confidence", 0.7)),
                    source_channel="manual_purify",
                )
                if self.plugin._vec_available and new_id:
                    await self.plugin._upsert_vector(new_id, memory)
                applied_adds += 1
            except Exception as e:
                logger.debug("[tmemory] apply add failed: %s", e)

        for d in deletes:
            try:
                mem_id = int(d)
                if self.plugin._delete_memory(mem_id):
                    applied_deletes += 1
            except Exception as e:
                logger.debug("[tmemory] apply delete failed: %s", e)

        return {
            "updates": applied_updates,
            "adds": applied_adds,
            "deletes": applied_deletes,
            "note": note,
        }


def update_memory_full(
    plugin,
    memory_id: int,
    memory: str,
    memory_type: str,
    score: float,
    importance: float,
    confidence: float,
) -> None:
    """更新记忆全字段(memory/tokenized_memory/hash/type/scores)。"""
    now = plugin._now()
    mhash = hashlib.sha256(plugin._normalize_text(memory).encode("utf-8")).hexdigest()
    tokenized_memory = " ".join(jieba.cut_for_search(memory))
    summary_channel = "persona" if plugin._safe_memory_type(memory_type) == "style" else "canonical"
    with plugin._db() as conn:
        conn.execute(
            """
            UPDATE memories
            SET memory=?, tokenized_memory=?, memory_hash=?, memory_type=?, summary_channel=?,
                score=?, importance=?, confidence=?, updated_at=?
            WHERE id=?
            """,
            (memory, tokenized_memory, mhash, memory_type, summary_channel, score, importance, confidence, now, memory_id),
        )
