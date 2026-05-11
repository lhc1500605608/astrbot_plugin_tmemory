"""AdminMemoryMixin — 记忆 CRUD、导图投影和记忆合并/拆分操作。

所有方法通过 ``self._db_mgr`` / ``self._plugin`` 等基类属性访问共享状态。
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from core.db import DatabaseManager
    from core.config import PluginConfig
    from core.identity import IdentityManager

logger = logging.getLogger("astrbot")

# 纯工具函数在 utils_shared.py 模块级定义，mixin 内通过 self._db() 等方式
# 使用基类设施。避免与兄弟 mixin 相互 import。


class AdminMemoryMixin:
    """记忆管理方法组。

    包含记忆 CRUD、导图投影、合并拆分以及内部辅助方法。
    所有数据库访问通过基类的 ``_db()`` 进行。
    """

    # =====================================================================
    # 只读查询
    # =====================================================================

    def get_memories(self, user: str) -> List[Dict[str, Any]]:
        """返回指定用户的活跃记忆列表。"""
        if not user:
            return []
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence,
                       reinforce_count, is_active,
                       COALESCE(is_pinned, 0) AS is_pinned,
                       last_seen_at, created_at, updated_at
                FROM memories WHERE canonical_user_id = ? AND is_active = 1
                ORDER BY importance DESC, score DESC, updated_at DESC LIMIT 200
                """,
                (user,),
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
                "is_active": int(r["is_active"]),
                "is_pinned": int(r["is_pinned"]),
                "last_seen_at": str(r["last_seen_at"]),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def get_mindmap_data(self, user: str) -> Dict[str, Any]:
        """返回三层记忆导图的投影数据。

        Returns
        -------
        dict: {"working": [...], "episodic": [...], "semantic": [...]}
        """
        if not user:
            return {"working": [], "episodic": [], "semantic": []}
        with self._db() as conn:
            working_rows = conn.execute(
                """
                SELECT id, role, content, session_key, turn_index,
                       distilled, episode_id, captured_at, created_at
                FROM conversation_cache
                WHERE canonical_user_id = ?
                ORDER BY id DESC LIMIT 30
                """,
                (user,),
            ).fetchall()
            episodic_rows = conn.execute(
                """
                SELECT id, episode_title, episode_summary, topic_tags,
                       key_entities, status, consolidation_status,
                       importance, confidence, source_count,
                       first_source_at, last_source_at, created_at, updated_at
                FROM memory_episodes
                WHERE canonical_user_id = ?
                ORDER BY updated_at DESC LIMIT 30
                """,
                (user,),
            ).fetchall()
            semantic_rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence,
                       reinforce_count, is_active,
                       COALESCE(is_pinned, 0) AS is_pinned,
                       episode_id, derived_from, semantic_status,
                       last_seen_at, created_at, updated_at
                FROM memories WHERE canonical_user_id = ? AND is_active = 1
                ORDER BY importance DESC, score DESC, updated_at DESC LIMIT 200
                """,
                (user,),
            ).fetchall()

        return {
            "working": [
                {
                    "id": int(r["id"]),
                    "role": str(r["role"]),
                    "content": str(r["content"]),
                    "session_key": str(r["session_key"]),
                    "turn_index": int(r["turn_index"]),
                    "distilled": int(r["distilled"]),
                    "episode_id": int(r["episode_id"]),
                    "captured_at": str(r["captured_at"]),
                    "created_at": str(r["created_at"]),
                }
                for r in working_rows
            ],
            "episodic": [
                {
                    "id": int(r["id"]),
                    "episode_title": str(r["episode_title"]),
                    "episode_summary": str(r["episode_summary"]),
                    "topic_tags": str(r["topic_tags"]),
                    "key_entities": str(r["key_entities"]),
                    "status": str(r["status"]),
                    "consolidation_status": str(r["consolidation_status"]),
                    "importance": float(r["importance"]),
                    "confidence": float(r["confidence"]),
                    "source_count": int(r["source_count"]),
                    "first_source_at": str(r["first_source_at"]),
                    "last_source_at": str(r["last_source_at"]),
                    "created_at": str(r["created_at"]),
                    "updated_at": str(r["updated_at"]),
                }
                for r in episodic_rows
            ],
            "semantic": [
                {
                    "id": int(r["id"]),
                    "memory_type": str(r["memory_type"]),
                    "memory": str(r["memory"]),
                    "score": float(r["score"]),
                    "importance": float(r["importance"]),
                    "confidence": float(r["confidence"]),
                    "reinforce_count": int(r["reinforce_count"]),
                    "is_active": int(r["is_active"]),
                    "is_pinned": int(r["is_pinned"]),
                    "episode_id": int(r["episode_id"]),
                    "derived_from": str(r["derived_from"]),
                    "semantic_status": str(r["semantic_status"]),
                    "last_seen_at": str(r["last_seen_at"]),
                    "created_at": str(r["created_at"]),
                    "updated_at": str(r["updated_at"]),
                }
                for r in semantic_rows
            ],
        }

    # =====================================================================
    # 低风险写操作
    # =====================================================================

    def add_memory(
        self,
        user: str,
        memory: str,
        score: float = 0.7,
        memory_type: str = "fact",
        importance: float = 0.6,
        confidence: float = 0.7,
    ) -> int:
        """通过 WebUI 添加一条记忆。"""
        from .memory_ops import MemoryOps
        return MemoryOps(self._plugin).insert_memory(
            canonical_id=user,
            adapter="webui",
            adapter_user=user,
            memory=memory,
            score=score,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            source_channel="webui",
        )

    def update_memory(self, mem_id: int, data: Dict[str, Any]) -> None:
        """更新记忆字段（memory, memory_type, score, importance, confidence, is_pinned）。"""
        from .utils_shared import _now, _safe_memory_type, _clamp01, _normalize_text

        now = _now()
        fields: list = []
        params: list = []

        for col, key, conv in [
            ("memory", "memory", str),
            ("memory_type", "memory_type", lambda v: _safe_memory_type(v)),
            ("score", "score", lambda v: _clamp01(v)),
            ("importance", "importance", lambda v: _clamp01(v)),
            ("confidence", "confidence", lambda v: _clamp01(v)),
            ("is_pinned", "is_pinned", lambda v: 1 if v else 0),
        ]:
            if key in data:
                fields.append(f"{col} = ?")
                params.append(conv(data[key]))

        if not fields:
            raise ValueError("no fields to update")

        if "memory" in data:
            new_hash = hashlib.sha256(
                _normalize_text(str(data["memory"])).encode()
            ).hexdigest()
            fields.append("memory_hash = ?")
            params.append(new_hash)

        fields.append("updated_at = ?")
        params.append(now)
        params.append(mem_id)

        with self._db() as conn:
            conn.execute(
                f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", params
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin,
            canonical_user_id=str(data.get("user", "")),
            event_type="webui_update",
            payload={"memory_id": mem_id, "updated_fields": list(data.keys())},
        )

    def delete_memory(self, memory_id: int) -> bool:
        """删除一条记忆（含向量索引清理和审计）。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM memories WHERE id=?",
                (memory_id,),
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
                from .vector import delete_vector
                delete_vector(self._plugin, memory_id, conn=conn)
            return deleted

    def set_pinned(self, memory_id: int, pinned: bool) -> bool:
        """设置/取消记忆常驻。"""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE memories SET is_pinned = ? WHERE id = ?",
                (1 if pinned else 0, memory_id),
            )
            return cur.rowcount > 0

    # =====================================================================
    # 复杂写操作：合并/拆分
    # =====================================================================

    async def merge_memories(
        self, user: str, ids: List[int], merged_text: str = ""
    ) -> Dict[str, Any]:
        """合并多条记忆为一条。"""
        safe_ids = [int(i) for i in ids if str(i).isdigit()]
        rows = self._fetch_memories_by_ids(user, safe_ids)
        if len(rows) < 2:
            raise ValueError("not enough valid memory ids")

        if not merged_text:
            merged_text = self._auto_merge_memory_text(
                [str(r["memory"]) for r in rows]
            )

        keep_id = int(rows[0]["id"])
        self._update_memory_text(keep_id, merged_text)

        if getattr(self._plugin, "_vec_available", False):
            from .vector import upsert_vector
            await upsert_vector(self._plugin, keep_id, merged_text)

        deleted = 0
        for r in rows[1:]:
            if self.delete_memory(int(r["id"])):
                deleted += 1

        return {"keep_id": keep_id, "deleted": deleted}

    async def split_memory(
        self,
        user: str,
        memory_id: int,
        segments: Optional[List[str]] = None,
        unified_msg_origin: str = "",
    ) -> Dict[str, Any]:
        """拆分一条记忆为多条。"""
        from .utils_shared import _normalize_text

        row = self._fetch_memory_by_id(user, memory_id)
        if not row:
            raise LookupError("memory not found")

        if isinstance(segments, list):
            segs = [_normalize_text(str(s)) for s in segments if _normalize_text(str(s))]
        else:
            class _Evt:
                pass
            _Evt.unified_msg_origin = unified_msg_origin  # type: ignore[attr-defined]
            from .maintenance import llm_split_memory
            segs = await llm_split_memory(self._plugin, _Evt(), str(row["memory"]))  # type: ignore[arg-type]

        if len(segs) < 2:
            raise ValueError("segments < 2")

        self._update_memory_text(memory_id, segs[0])
        vec_available = getattr(self._plugin, "_vec_available", False)
        if vec_available:
            from .vector import upsert_vector
            await upsert_vector(self._plugin, memory_id, segs[0])

        from .memory_ops import MemoryOps
        ops = MemoryOps(self._plugin)
        added: List[int] = []
        for seg in segs[1:]:
            new_id = ops.insert_memory(
                canonical_id=user,
                adapter=str(row["source_adapter"]),
                adapter_user=str(row["source_user_id"]),
                memory=seg,
                score=float(row["score"]),
                memory_type=str(row["memory_type"]),
                importance=float(row["importance"]),
                confidence=float(row["confidence"]),
                source_channel="manual_split",
            )
            if vec_available and new_id:
                from .vector import upsert_vector
                await upsert_vector(self._plugin, new_id, seg)
            added.append(new_id)

        return {"base_id": memory_id, "added_ids": added}

    # ── 内部数据访问 ─────────────────────────────────────────────────

    def _fetch_memories_by_ids(
        self, canonical_id: str, ids: List[int]
    ) -> List[Dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join(["?"] * len(ids))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT id, memory, memory_type, score, importance, confidence, "
                f"source_adapter, source_user_id "
                f"FROM memories WHERE canonical_user_id=? AND id IN ({placeholders}) ORDER BY id",
                [canonical_id, *ids],
            ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_memory_by_id(
        self, canonical_id: str, memory_id: int
    ) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, canonical_user_id, source_adapter, source_user_id, "
                "memory, memory_type, score, importance, confidence "
                "FROM memories WHERE id=? AND canonical_user_id=?",
                (memory_id, canonical_id),
            ).fetchone()
        return dict(row) if row else None

    def _update_memory_text(self, memory_id: int, memory: str) -> None:
        from .utils_shared import _now, _normalize_text

        now = _now()
        mhash = hashlib.sha256(
            _normalize_text(memory).encode("utf-8")
        ).hexdigest()
        try:
            import jieba
            tokenized_memory = " ".join(jieba.cut_for_search(memory))
        except ImportError:
            tokenized_memory = memory
        with self._db() as conn:
            conn.execute(
                "UPDATE memories SET memory=?, tokenized_memory=?, "
                "memory_hash=?, updated_at=? WHERE id=?",
                (memory, tokenized_memory, mhash, now, memory_id),
            )

    def _auto_merge_memory_text(self, memories: List[str]) -> str:
        """无 LLM 时的简单合并策略：去重后拼接。"""
        from .utils_shared import _normalize_text

        uniq: List[str] = []
        seen: set = set()
        for m in memories:
            n = _normalize_text(m)
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        if not uniq:
            return ""
        if len(uniq) == 1:
            return uniq[0]
        merged = ";".join(uniq)
        if not merged.startswith("用户"):
            merged = f"用户{merged}"
        return merged[:300]
