"""AdminService — WebUI 管理用例的应用服务边界。

所有 WebUI 管理操作通过此类暴露，web_server.py 只做 HTTP 适配（request
parsing + response wrapping），不再直接执行 SQL 或调用 plugin 私有方法。

设计约束（ADR-005）：
- 输入输出使用普通 dict / list，不依赖 aiohttp。
- 不持有 HTTP / 浏览器相关状态。
- 通过构造函数注入依赖（db_manager, identity_mgr, memory_logger, cfg）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from core.db import DatabaseManager
    from core.config import PluginConfig
    from core.identity import IdentityManager
    from core.utils import MemoryLogger

logger = logging.getLogger("astrbot")

# ── 纯工具函数（无状态） ─────────────────────────────────────────────

_VALID_MEMORY_TYPES = frozenset({"preference", "fact", "task", "restriction", "style"})
_VALID_FACET_TYPES = frozenset({"preference", "fact", "style", "restriction", "task_pattern"})
_VALID_ITEM_STATUSES = frozenset({"active", "superseded", "contradicted", "archived"})


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _safe_memory_type(value: object) -> str:
    s = str(value or "fact").strip().lower()
    return s if s in _VALID_MEMORY_TYPES else "fact"


def _clamp01(value: object) -> float:
    try:
        num = float(value)  # type: ignore[arg-type]
    except Exception:
        num = 0.0
    return max(0.0, min(1.0, num))


class AdminService:
    """WebUI 管理用例的应用服务。

    Parameters
    ----------
    plugin : TMemoryPlugin
        插件实例——仅用于透传给仍然需要 ``plugin`` 作为首参的 core/ 模块
        函数（如 ``maintenance.get_global_stats(plugin)``）。
        AdminService 本身只通过显式注入的依赖工作。
    """

    def __init__(self, plugin: Any) -> None:
        # 显式依赖——AdminService 只通过这些引用工作
        self._plugin = plugin
        self._db_mgr = plugin._db_mgr
        self._identity_mgr: IdentityManager = plugin._identity_mgr
        self._memory_logger: MemoryLogger = plugin._memory_logger
        self._cfg: PluginConfig = plugin._cfg

    # ── helpers ───────────────────────────────────────────────────────

    def _db(self):
        return self._db_mgr.db()

    # =====================================================================
    # Batch 1.1 — 只读查询
    # =====================================================================

    def get_users(self) -> List[Dict[str, Any]]:
        """返回所有用户：合并 memories 和 conversation_cache 两张表。"""
        with self._db() as conn:
            mem_rows = conn.execute(
                "SELECT canonical_user_id, COUNT(*) as cnt "
                "FROM memories WHERE is_active = 1 "
                "GROUP BY canonical_user_id"
            ).fetchall()
            cache_rows = conn.execute(
                "SELECT canonical_user_id, COUNT(*) as cnt "
                "FROM conversation_cache WHERE distilled = 0 "
                "GROUP BY canonical_user_id"
            ).fetchall()

        merged: Dict[str, Dict[str, Any]] = {}
        for r in mem_rows:
            uid = str(r["canonical_user_id"])
            merged[uid] = {"id": uid, "memory_count": int(r["cnt"]), "pending_count": 0}
        for r in cache_rows:
            uid = str(r["canonical_user_id"])
            if uid in merged:
                merged[uid]["pending_count"] = int(r["cnt"])
            else:
                merged[uid] = {"id": uid, "memory_count": 0, "pending_count": int(r["cnt"])}

        return sorted(
            merged.values(),
            key=lambda u: u["memory_count"] + u["pending_count"],
            reverse=True,
        )

    def get_global_stats(self) -> Dict[str, Any]:
        """获取全局统计信息。"""
        from .maintenance import get_global_stats
        stats = get_global_stats(self._plugin)
        stats["pending_users"] = self.count_pending_users()
        return stats

    def count_pending_users(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS n FROM "
                "(SELECT canonical_user_id FROM conversation_cache "
                "WHERE distilled=0 GROUP BY canonical_user_id)"
            ).fetchone()
        return int(row["n"] if row else 0)

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
            # Working layer: 最近 conversation_cache turns
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
            # Episodic layer: memory_episodes with source_count
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
            # Semantic layer: same as get_memories but with episode provenance
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

    def get_events(self, user: str) -> List[Dict[str, Any]]:
        """返回指定用户的审计事件列表。"""
        if not user:
            return []
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, event_type, payload_json, created_at "
                "FROM memory_events WHERE canonical_user_id = ? "
                "ORDER BY id DESC LIMIT 100",
                (user,),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "event_type": str(r["event_type"]),
                "payload_json": str(r["payload_json"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def get_pending(self) -> List[Dict[str, Any]]:
        """返回待蒸馏队列详情。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT canonical_user_id, COUNT(*) as cnt, "
                "MIN(created_at) as oldest, MAX(created_at) as newest "
                "FROM conversation_cache WHERE distilled = 0 "
                "GROUP BY canonical_user_id ORDER BY cnt DESC LIMIT 100"
            ).fetchall()
        return [
            {
                "user": str(r["canonical_user_id"]),
                "count": int(r["cnt"]),
                "oldest": str(r["oldest"]),
                "newest": str(r["newest"]),
            }
            for r in rows
        ]

    def get_identities(self) -> List[Dict[str, Any]]:
        """返回所有身份绑定关系。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, adapter, adapter_user_id, canonical_user_id, updated_at "
                "FROM identity_bindings ORDER BY canonical_user_id, adapter"
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "adapter": str(r["adapter"]),
                "adapter_user_id": str(r["adapter_user_id"]),
                "canonical_user_id": str(r["canonical_user_id"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def get_distill_history(self, limit: int = 30) -> List[Dict]:
        """返回蒸馏历史记录。"""
        from .distill_validator import get_distill_history
        return get_distill_history(self._plugin, limit=limit)

    # =====================================================================
    # Batch 1.1b — 用户画像只读查询
    # =====================================================================

    def get_profile_summary(self, user: str) -> Dict[str, Any]:
        """返回用户画像概览：profile 摘要 + 按 facet/status 统计。"""
        if not user:
            return {"user_profile": None, "facet_counts": {}, "status_counts": {}, "total_items": 0}
        with self._db() as conn:
            up = conn.execute(
                "SELECT * FROM user_profiles WHERE canonical_user_id=?",
                (user,),
            ).fetchone()
            facet_counts = {}
            for r in conn.execute(
                "SELECT facet_type, COUNT(*) as cnt FROM profile_items "
                "WHERE canonical_user_id=? AND status='active' GROUP BY facet_type",
                (user,),
            ).fetchall():
                facet_counts[str(r["facet_type"])] = int(r["cnt"])
            status_counts = {}
            for r in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM profile_items "
                "WHERE canonical_user_id=? GROUP BY status",
                (user,),
            ).fetchall():
                status_counts[str(r["status"])] = int(r["cnt"])
            total = conn.execute(
                "SELECT COUNT(*) FROM profile_items WHERE canonical_user_id=?",
                (user,),
            ).fetchone()
        return {
            "user_profile": {
                "canonical_user_id": str(up["canonical_user_id"]),
                "display_name": str(up["display_name"] or ""),
                "profile_version": int(up["profile_version"]),
                "summary_text": str(up["summary_text"] or ""),
                "created_at": str(up["created_at"]),
                "updated_at": str(up["updated_at"]),
            } if up else None,
            "facet_counts": facet_counts,
            "status_counts": status_counts,
            "total_items": int(total[0]) if total else 0,
        }

    def get_profile_items(
        self, user: str, facet_type: str = "", status: str = "active"
    ) -> List[Dict[str, Any]]:
        """返回用户画像条目列表，可筛选 facet_type 和 status。"""
        if not user:
            return []
        where = ["canonical_user_id = ?"]
        params: list = [user]
        if facet_type and facet_type in _VALID_FACET_TYPES:
            where.append("facet_type = ?")
            params.append(facet_type)
        if status and status in _VALID_ITEM_STATUSES:
            where.append("status = ?")
            params.append(status)
        with self._db() as conn:
            rows = conn.execute(
                f"""
                SELECT pi.*,
                       (SELECT COUNT(*) FROM profile_item_evidence e WHERE e.profile_item_id=pi.id) AS evidence_count
                FROM profile_items pi
                WHERE {' AND '.join(where)}
                ORDER BY importance DESC, confidence DESC, updated_at DESC LIMIT 200
                """,
                params,
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "facet_type": str(r["facet_type"]),
                "title": str(r["title"] or ""),
                "content": str(r["content"]),
                "status": str(r["status"]),
                "confidence": float(r["confidence"]),
                "importance": float(r["importance"]),
                "stability": float(r["stability"] or 0.5),
                "usage_count": int(r["usage_count"] or 0),
                "last_used_at": str(r["last_used_at"] or ""),
                "last_confirmed_at": str(r["last_confirmed_at"] or ""),
                "source_scope": str(r["source_scope"] or "user"),
                "persona_id": str(r["persona_id"] or ""),
                "evidence_count": int(r["evidence_count"] or 0),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def get_profile_item_evidence(self, item_id: int) -> List[Dict[str, Any]]:
        """返回指定画像条目的证据链。"""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT e.*, cc.role, cc.content AS excerpt
                FROM profile_item_evidence e
                LEFT JOIN conversation_cache cc ON cc.id = e.conversation_cache_id
                WHERE e.profile_item_id = ?
                ORDER BY e.id LIMIT 50
                """,
                (item_id,),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "conversation_cache_id": int(r["conversation_cache_id"] or 0),
                "source_role": str(r["source_role"]),
                "source_excerpt": str(r["source_excerpt"] or ""),
                "evidence_kind": str(r["evidence_kind"]),
                "confidence_delta": float(r["confidence_delta"] or 0),
                "excerpt": str(r["excerpt"] or ""),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    # =====================================================================
    # Batch 1.2 — 低风险写操作
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

    # ── 用户画像写操作 ──────────────────────────────────────────────────

    def update_profile_item(self, item_id: int, data: Dict[str, Any]) -> None:
        """更新画像条目字段（title, content, facet_type, confidence, importance, status）。"""
        now = _now()
        fields: list = []
        params: list = []

        for col, key, conv in [
            ("title", "title", str),
            ("content", "content", str),
            ("facet_type", "facet_type", lambda v: v if v in _VALID_FACET_TYPES else None),
            ("confidence", "confidence", lambda v: _clamp01(v)),
            ("importance", "importance", lambda v: _clamp01(v)),
            ("status", "status", lambda v: v if v in _VALID_ITEM_STATUSES else None),
        ]:
            if key in data:
                val = conv(data[key])
                if val is not None:
                    fields.append(f"{col} = ?")
                    params.append(val)

        if not fields:
            raise ValueError("no fields to update")

        if "content" in data:
            import re
            normalized = re.sub(r"\s+", " ", str(data["content"]).strip()).lower()
            fields.append("normalized_content = ?")
            params.append(normalized)

        fields.append("updated_at = ?")
        params.append(now)
        params.append(item_id)

        with self._db() as conn:
            conn.execute(
                f"UPDATE profile_items SET {', '.join(fields)} WHERE id = ?", params
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin,
            canonical_user_id=str(data.get("user", "")),
            event_type="profile_item_update_webui",
            payload={"profile_item_id": item_id, "updated_fields": list(data.keys())},
        )

    def archive_profile_item(self, item_id: int) -> bool:
        """归档一条画像条目。"""
        from .memory_ops import ProfileItemOps
        return ProfileItemOps(self._plugin).archive_item(item_id)

    def merge_profile_items(self, user: str, ids: List[int]) -> Dict[str, Any]:
        """合并多条画像条目（同 facet_type + 同 profile item 语义合并）。

        保留 ids[0]，将其余条目的证据迁移过去，然后标记被合并条目为 superseded。
        """
        safe_ids = [int(i) for i in ids if str(i).isdigit()]
        if len(safe_ids) < 2:
            raise ValueError("need at least 2 profile item ids to merge")

        placeholders = ",".join(["?"] * len(safe_ids))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT id, content, facet_type "
                f"FROM profile_items WHERE id IN ({placeholders}) AND canonical_user_id=?",
                [*safe_ids, user],
            ).fetchall()

        if len(rows) < 2:
            raise ValueError("not enough valid profile items")

        rows_by_id = {int(r["id"]): r for r in rows}

        # Guard: all items must share the same facet_type
        facet_types = {r["facet_type"] for r in rows}
        if len(facet_types) > 1:
            raise ValueError(
                f"cannot merge items with different facet types: {', '.join(sorted(facet_types))}"
            )

        # Deterministic keeper: use ids[0] from the request
        keep_id = safe_ids[0]
        if keep_id not in rows_by_id:
            raise ValueError(f"keeper item {keep_id} not found")
        keep = rows_by_id[keep_id]
        merged_parts = [str(keep["content"])]

        now = _now()
        with self._db() as conn:
            for row in rows:
                merged_id = int(row["id"])
                if merged_id == keep_id:
                    continue
                merged_parts.append(str(row["content"]))
                # Move evidence to keeper
                conn.execute(
                    "UPDATE profile_item_evidence SET profile_item_id=? WHERE profile_item_id=?",
                    (keep_id, merged_id),
                )
                # Mark merged item as superseded (consistent with ProfileItemOps.supersede_item)
                conn.execute(
                    "UPDATE profile_items SET status='superseded', updated_at=? WHERE id=? AND status='active'",
                    (now, merged_id),
                )
                # Archive relations for merged item
                conn.execute(
                    "UPDATE profile_relations SET status='archived', updated_at=? WHERE (from_item_id=? OR to_item_id=?)",
                    (now, merged_id, merged_id),
                )
                # Create supersedes relation: keeper supersedes merged
                conn.execute(
                    """
                    INSERT OR IGNORE INTO profile_relations(canonical_user_id, from_item_id, to_item_id, relation_type, status, weight, created_at, updated_at)
                    VALUES(?, ?, ?, 'supersedes', 'active', 1.0, ?, ?)
                    """,
                    (user, keep_id, merged_id, now, now),
                )

            merged_text = "; ".join(merged_parts)
            import re
            normalized = re.sub(r"\s+", " ", merged_text).lower()
            conn.execute(
                "UPDATE profile_items SET content=?, normalized_content=?, updated_at=? WHERE id=?",
                (merged_text[:500], normalized, now, keep_id),
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin, user,
            "profile_items_merged_webui",
            {"keep_id": keep_id, "archived_ids": [int(r["id"]) for r in rows if int(r["id"]) != keep_id]},
        )

        return {"keep_id": keep_id, "archived_count": len(rows) - 1}

    # =====================================================================
    # Batch 1.3 — 高风险写操作
    # =====================================================================

    async def insert_test_conversation(
        self,
        user_id: str,
        role: str,
        content: str,
        source_adapter: str = "webui_test",
        source_user_id: str = "",
        unified_msg_origin: str = "",
        scope: str = "user",
        persona_id: str = "",
    ) -> Dict[str, Any]:
        """插入一条测试对话到 conversation_cache，用于 WebUI 模拟链路。

        复用 plugin._insert_conversation 路径，
        仅写入缓存，不触发蒸馏、不注入记忆。
        """
        if not user_id or not content or not role:
            return {"ok": False, "error": "user_id, role, content are required"}
        if role not in ("user", "assistant"):
            return {"ok": False, "error": "role must be user or assistant"}

        await self._plugin._insert_conversation(
            canonical_id=user_id,
            role=role,
            content=content,
            source_adapter=source_adapter or "webui_test",
            source_user_id=source_user_id or user_id,
            unified_msg_origin=unified_msg_origin or f"webui_test:{user_id}",
            scope=scope or "user",
            persona_id=persona_id or "",
        )
        return {"ok": True}

    async def trigger_distill(self) -> Dict[str, Any]:
        """手动触发蒸馏。"""
        pending_before = self.count_pending_users()
        from .memory_ops import MemoryOps
        processed_users, total_memories = await MemoryOps(self._plugin).run_distill_cycle(
            force=True, trigger="manual_web"
        )
        return {
            "processed_users": processed_users,
            "total_memories": total_memories,
            "pending_users_before": pending_before,
        }

    def set_distill_pause(self, pause: bool) -> None:
        """暂停或恢复自动蒸馏。

        写入 ``_cfg.distill_pause``（distill worker 实际检查的位置）。
        """
        self._cfg.distill_pause = pause

    def merge_users(self, from_id: str, to_id: str) -> int:
        """合并两个用户：将 from_user 的所有记忆和绑定迁移到 to_user。"""
        return self._identity_mgr.merge_identity(from_id, to_id)

    def rebind_user(self, binding_id: int, new_canonical: str) -> Dict[str, str]:
        """将一个适配器账号改绑到新的统一用户 ID。

        Returns
        -------
        dict with ``old_canonical``, ``adapter``, ``adapter_user_id``
        """
        now = _now()
        with self._db() as conn:
            row = conn.execute(
                "SELECT adapter, adapter_user_id, canonical_user_id "
                "FROM identity_bindings WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if not row:
                raise LookupError("binding not found")
            old_canonical = str(row["canonical_user_id"])
            conn.execute(
                "UPDATE identity_bindings SET canonical_user_id = ?, updated_at = ? WHERE id = ?",
                (new_canonical, now, binding_id),
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin,
            canonical_user_id=new_canonical,
            event_type="rebind",
            payload={
                "binding_id": binding_id,
                "adapter": str(row["adapter"]),
                "adapter_user_id": str(row["adapter_user_id"]),
                "old_canonical": old_canonical,
                "new_canonical": new_canonical,
            },
        )
        return {
            "old_canonical": old_canonical,
            "adapter": str(row["adapter"]),
            "adapter_user_id": str(row["adapter_user_id"]),
        }

    def export_user(self, user: str) -> Dict:
        """导出用户数据。"""
        from .maintenance import export_user_data
        return export_user_data(self._plugin, user)

    def purge_user(self, user: str) -> Dict[str, int]:
        """清除用户全部数据。"""
        from .maintenance import purge_user_data
        return purge_user_data(self._plugin, user)

    async def refine_memories(
        self,
        user: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
        unified_msg_origin: str = "",
    ) -> Dict[str, Any]:
        """LLM 记忆精馏/提纯。"""
        mode = mode or self._cfg.manual_purify_default_mode or "both"
        limit = max(1, min(200, limit or self._cfg.manual_purify_default_limit or 20))

        class _Evt:
            pass
        _Evt.unified_msg_origin = unified_msg_origin  # type: ignore[attr-defined]

        from .memory_ops import MemoryOps
        return await MemoryOps(self._plugin).manual_purify_memories(
            event=_Evt(),  # type: ignore[arg-type]
            canonical_id=user,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra_instruction,
        )

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
