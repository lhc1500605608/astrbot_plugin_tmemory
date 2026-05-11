"""DataAccessMixin — memory CRUD and conversation cache operations.

Extracted from helpers.py to keep each module under 500 lines (ADR-009 / TMEAAA-350).
Methods use ``self._db()``, ``self._now()``, ``self._cfg`` etc. from the host class (TMemoryPlugin).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Dict, List, Optional, Sequence, Tuple

import jieba

from . import maintenance as _maintenance
from . import memory_ops as _memory_ops
from . import vector as _vector

class DataAccessMixin:
    """Memory CRUD and conversation cache operations."""

    def _insert_memory(
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
        from .memory_ops import MemoryOps

        return MemoryOps(self).insert_memory(
            canonical_id=canonical_id,
            adapter=adapter,
            adapter_user=adapter_user,
            memory=memory,
            score=score,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            source_channel=source_channel,
            persona_id=persona_id,
            scope=scope,
        )

    def _delete_memory(self, memory_id: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            deleted = cur.rowcount > 0
            if deleted and row:
                self._memory_logger.log_memory_event(
                    canonical_user_id=str(row["canonical_user_id"]),
                    event_type="delete",
                    payload={"memory_id": memory_id},
                    conn=conn,
                )
                if self._vec_available:
                    self._delete_vector(memory_id, conn=conn)
            return deleted

    def _list_memories(
        self, canonical_id: str, limit: int = 8
    ) -> List[Dict[str, object]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, attention_score, updated_at, is_pinned
                FROM memories
                WHERE canonical_user_id=? AND is_active=1
                ORDER BY importance DESC, score DESC, updated_at DESC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()

        return [
            {
                "id": int(r["id"]),
                "memory_type": str(r["memory_type"]),
                "memory": str(r["memory"]),
                "score": float(r["score"]),
                "importance": float(r["importance"]),
                "confidence": float(r["confidence"]),
                "reinforce_count": int(r["reinforce_count"]),
                "attention_score": float(r["attention_score"] or 0.5),
                "updated_at": str(r["updated_at"]),
                "is_pinned": int(r["is_pinned"]),
            }
            for r in rows
        ]

    async def _retrieve_memories(
        self,
        canonical_id: str,
        query: str,
        limit: int,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
        summary_channel: str = "canonical",
    ) -> List[Dict[str, object]]:
        from astrbot.api.event import AstrMessageEvent

        query_vec: Optional[List[float]] = None
        if self._vec_available and query:
            query_vec = await _vector.get_or_generate_query_embedding(self, query)

        scored, _ = await self._retrieval_mgr.retrieve_memories(
            canonical_id=canonical_id,
            query=query,
            limit=limit,
            query_vec=query_vec,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
            summary_channel=summary_channel,
        )

        deduped = self._retrieval_mgr.deduplicate_results(scored, limit * 2)

        if (
            self._cfg.enable_reranker
            and self._cfg.rerank_base_url
            and query
            and len(deduped) > 1
        ):
            top_result = await self._rerank_results(query, deduped, limit)
        else:
            top_result = deduped[:limit]

        if top_result:
            reinforce_now = self._now()
            reinforce_ids = [int(item["id"]) for item in top_result]
            placeholders = ",".join(["?"] * len(reinforce_ids))
            with self._db() as conn:
                conn.execute(
                    f"UPDATE memories SET reinforce_count = reinforce_count + 1,"
                    f" attention_score = MIN(1.0, attention_score + 0.05),"
                    f" last_seen_at = ? WHERE id IN ({placeholders})",
                    [reinforce_now, *reinforce_ids],
                )

        return top_result

    async def _manual_purify_memories(
        self,
        event,
        canonical_id: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
    ) -> Dict[str, object]:
        from .memory_ops import MemoryOps

        return await MemoryOps(self).manual_purify_memories(
            event=event,
            canonical_id=canonical_id,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra_instruction,
        )

    async def _llm_purify_operations(
        self,
        event,
        rows: List[Dict[str, object]],
        mode: str,
        extra_instruction: str,
    ) -> Dict[str, object]:
        return await _maintenance.llm_purify_operations(
            self, event, rows, mode, extra_instruction
        )

    async def _manual_refine_memories(
        self,
        event,
        canonical_id: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
    ) -> Dict[str, object]:
        return await self._manual_purify_memories(
            event=event,
            canonical_id=canonical_id,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra_instruction,
        )

    async def _llm_split_memory(
        self, event, memory_text: str
    ) -> List[str]:
        return await _maintenance.llm_split_memory(self, event, memory_text)

    def _parse_json_object(self, text: str) -> Optional[Dict[str, object]]:
        if not text:
            return None
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                    return data if isinstance(data, dict) else None
                except Exception:
                    return None
        return None

    def _list_memories_for_purify(
        self, canonical_id: str, limit: int, include_pinned: bool
    ) -> List[Dict[str, object]]:
        with self._db() as conn:
            sql = (
                "SELECT id, memory, memory_type, score, importance, confidence, reinforce_count, is_pinned "
                "FROM memories WHERE canonical_user_id=? AND is_active=1 "
                + ("" if include_pinned else "AND is_pinned=0 ")
                + "ORDER BY importance DESC, score DESC, updated_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, (canonical_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def _fetch_memory_by_id(
        self, canonical_id: str, memory_id: int
    ) -> Optional[Dict[str, object]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, canonical_user_id, source_adapter, source_user_id, memory, memory_type, score, importance, confidence "
                "FROM memories WHERE id=? AND canonical_user_id=?",
                (memory_id, canonical_id),
            ).fetchone()
        return dict(row) if row else None

    def _fetch_memories_by_ids(
        self, canonical_id: str, ids: List[int]
    ) -> List[Dict[str, object]]:
        if not ids:
            return []
        placeholders = ",".join(["?"] * len(ids))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT id, memory, memory_type, score, importance, confidence, source_adapter, source_user_id "
                f"FROM memories WHERE canonical_user_id=? AND id IN ({placeholders}) ORDER BY id",
                [canonical_id, *ids],
            ).fetchall()
        return [dict(r) for r in rows]

    def _update_memory_text(self, memory_id: int, memory: str) -> None:
        now = self._now()
        mhash = hashlib.sha256(self._normalize_text(memory).encode("utf-8")).hexdigest()
        tokenized_memory = " ".join(jieba.cut_for_search(memory))
        with self._db() as conn:
            conn.execute(
                "UPDATE memories SET memory=?, tokenized_memory=?, memory_hash=?, updated_at=? WHERE id=?",
                (memory, tokenized_memory, mhash, now, memory_id),
            )

    def _update_memory_full(
        self,
        memory_id: int,
        memory: str,
        memory_type: str,
        score: float,
        importance: float,
        confidence: float,
    ) -> None:
        from . import memory_ops as _mo

        _mo.update_memory_full(
            self, memory_id, memory, memory_type, score, importance, confidence
        )

    def _auto_merge_memory_text(self, memories: List[str]) -> str:
        uniq: List[str] = []
        seen = set()
        for m in memories:
            n = self._normalize_text(m)
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        if not uniq:
            return ""
        if len(uniq) == 1:
            return uniq[0]
        merged = ";".join(uniq)
        if not merged.startswith("\u7528\u6237"):
            merged = f"\u7528\u6237{merged}"
        return merged[:300]

    async def _rerank_results(
        self, query: str, candidates: List[Dict[str, object]], top_n: int
    ) -> List[Dict[str, object]]:
        return await _vector.rerank_results(self, query, candidates, top_n)

    # ── Conversation cache operations ──────────────────────────────────────

    async def _insert_conversation(
        self,
        canonical_id: str,
        role: str,
        content: str,
        source_adapter: str,
        source_user_id: str,
        unified_msg_origin: str,
        scope: str = "user",
        persona_id: str = "",
    ):
        await asyncio.to_thread(
            self._insert_conversation_sync,
            canonical_id,
            role,
            content,
            source_adapter,
            source_user_id,
            unified_msg_origin,
            scope,
            persona_id,
        )

    def _insert_conversation_sync(
        self,
        canonical_id: str,
        role: str,
        content: str,
        source_adapter: str,
        source_user_id: str,
        unified_msg_origin: str,
        scope: str = "user",
        persona_id: str = "",
    ):
        from . import maintenance as _m_

        _m_.insert_conversation_sync(
            self,
            canonical_id,
            role,
            content,
            source_adapter,
            source_user_id,
            unified_msg_origin,
            scope,
            persona_id,
        )

    def _fetch_recent_conversation(
        self, canonical_id: str, limit: int = 20
    ) -> List[Tuple[str, str]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM conversation_cache
                WHERE canonical_user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()
        return [(str(r["role"]), str(r["content"])) for r in reversed(rows)]

    def _pending_distill_users(
        self, limit: int, min_batch_count: Optional[int] = None
    ) -> List[str]:
        min_required = (
            self._cfg.distill_min_batch_count
            if min_batch_count is None
            else max(1, int(min_batch_count))
        )
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT canonical_user_id, COUNT(*) as cnt
                FROM conversation_cache
                WHERE distilled=0 AND episode_id=0
                GROUP BY canonical_user_id
                HAVING cnt >= ?
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (min_required, limit),
            ).fetchall()
        return [str(r["canonical_user_id"]) for r in rows]

    def _fetch_pending_rows(self, canonical_id: str, limit: int) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, canonical_user_id, role, content, source_adapter, source_user_id, unified_msg_origin, scope, persona_id
                FROM conversation_cache
                WHERE canonical_user_id=? AND distilled=0 AND episode_id=0
                ORDER BY id ASC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def _mark_rows_distilled(self, ids: Sequence[int]):
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        params = [self._now(), *ids]
        with self._db() as conn:
            conn.execute(
                f"UPDATE conversation_cache SET distilled=1, distilled_at=? WHERE id IN ({placeholders})",
                params,
            )

    def _count_pending_users(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS n FROM (SELECT canonical_user_id FROM conversation_cache WHERE distilled=0 AND episode_id=0 GROUP BY canonical_user_id)"
            ).fetchone()
        return int(row["n"] if row else 0)

    def _count_pending_rows(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS n FROM conversation_cache WHERE distilled=0 AND episode_id=0"
            ).fetchone()
        return int(row["n"] if row else 0)

    def _trim_conversation(self, canonical_id: str, keep_last: int):
        with self._db() as conn:
            conn.execute(
                """
                DELETE FROM conversation_cache
                WHERE canonical_user_id=?
                AND id NOT IN (
                    SELECT id FROM conversation_cache
                    WHERE canonical_user_id=?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (canonical_id, canonical_id, keep_last),
            )

    def _optimize_context(self, canonical_id: str):
        _maintenance.optimize_context(self, canonical_id)

    def _set_pinned(self, memory_id: int, pinned: bool) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE memories SET is_pinned = ? WHERE id = ?",
                (1 if pinned else 0, memory_id),
            )
            return cur.rowcount > 0

    def _export_user_data(self, canonical_id: str) -> Dict:
        return _maintenance.export_user_data(self, canonical_id)

    def _purge_user_data(self, canonical_id: str) -> Dict[str, int]:
        return _maintenance.purge_user_data(self, canonical_id)

    def _get_global_stats(self) -> Dict[str, int]:
        return _maintenance.get_global_stats(self)
