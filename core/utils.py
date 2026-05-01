from __future__ import annotations

import time
import json
from typing import Dict, Optional
import sqlite3

class MemoryLogger:
    def __init__(self, db_manager):
        self._db_mgr = db_manager

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def log_memory_event(
        self,
        canonical_user_id: str,
        event_type: str,
        payload: Dict[str, object],
        conn: Optional[sqlite3.Connection] = None,
    ):
        row = (
            canonical_user_id,
            event_type,
            json.dumps(payload, ensure_ascii=False),
            self._now(),
        )
        sql = (
            "INSERT INTO memory_events(canonical_user_id, event_type, payload_json, created_at)"
            " VALUES(?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, row)
        else:
            with self._db_mgr.db() as db_conn:
                db_conn.execute(sql, row)


# =============================================================================
# Plugin helper and handler mixins
# =============================================================================

import asyncio
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from astrbot.api import logger
from . import maintenance as _maintenance
from . import memory_ops as _memory_ops
from . import vector as _vector

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from .db import _LockedConnection


_CMD_FIRST_WORDS = frozenset({
    "tm_distill_now", "tm_worker", "tm_memory", "tm_context", "tm_bind", "tm_merge",
    "tm_forget", "tm_stats", "tm_distill_history", "tm_purify", "tm_quality_refine",
    "tm_vec_rebuild", "tm_refine", "tm_mem_merge", "tm_mem_split", "tm_pin",
    "tm_unpin", "tm_export", "tm_purge",
})


class PluginHelpersMixin:
    _TRANSCRIPT_PREFIX_RE = re.compile(
        r"^(user|assistant|summary)\s*:\s*", re.IGNORECASE | re.MULTILINE
    )

    async def _build_knowledge_injection(
        self,
        canonical_user_id: str,
        query: str,
        limit: int,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> str:
        """构建知识/偏好记忆注入块。"""
        rows = await self._retrieve_memories(
            canonical_user_id,
            query,
            limit,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
        )
        if not rows:
            return ""

        if not rows:
            return ""

        knowledge_rows = [r for r in rows if r["memory_type"] != "style"]
        style_rows = [r for r in rows if r["memory_type"] == "style"]

        blocks: list[str] = []

        if knowledge_rows:
            knowledge_lines = ["[用户记忆]"]
            for row in knowledge_rows:
                knowledge_lines.append(f"- ({row['memory_type']}) {row['memory']}")
            blocks.append("\n".join(knowledge_lines))

        if style_rows:
            style_lines = ["[用户风格指导]"]
            for row in style_rows:
                style_lines.append(f"- {row['memory']}")
            blocks.append("\n".join(style_lines))

        block = "\n\n".join(blocks)
        if self._cfg.inject_max_chars > 0 and len(block) > self._cfg.inject_max_chars:
            cutoff = max(self._cfg.inject_max_chars - 3, 1)
            block = block[:cutoff] + "…"
        return block

    def _inject_block_by_position(self, req: ProviderRequest, block: str) -> None:
        """按 inject_position 配置将知识记忆块注入到正确位置。"""
        if self._cfg.inject_position == "slot":
            existing = getattr(req, "system_prompt", "") or ""
            if self._cfg.inject_slot_marker in existing:
                req.system_prompt = existing.replace(
                    self._cfg.inject_slot_marker, block, 1
                )
            else:
                req.system_prompt = existing + ("\n\n" if existing else "") + block
        elif self._cfg.inject_position == "user_message_before":
            original_prompt = getattr(req, "prompt", "") or ""
            req.prompt = block + "\n\n" + original_prompt if original_prompt else block
        elif self._cfg.inject_position == "user_message_after":
            original_prompt = getattr(req, "prompt", "") or ""
            req.prompt = original_prompt + ("\n\n" if original_prompt else "") + block
        else:  # system_prompt
            existing = getattr(req, "system_prompt", "") or ""
            req.system_prompt = existing + ("\n\n" if existing else "") + block

    async def build_memory_context(
        self, canonical_user_id: str, query: str, limit: int = 6
    ) -> str:
        """构建完整的调试用记忆上下文块(供 /tm_context 指令使用)。"""
        rows = await self._retrieve_memories(canonical_user_id, query, limit)
        recent = self._fetch_recent_conversation(canonical_user_id, limit=6)

        recent_lines = []
        for role, content in recent[-4:]:
            recent_lines.append(f"- {role}: {content}")

        memory_lines = []
        for row in rows:
            display_score = float(
                row.get("final_score", row.get("_retrieval_score", row.get("score", 0.0)))
            )
            memory_lines.append(
                f"- ({row['memory_type']}, score={display_score:.3f}) {row['memory']}"
            )

        if not memory_lines:
            memory_lines = ["- (none) 暂无匹配长期记忆"]

        return "\n".join(
            [
                "[Memory Context]",
                f"canonical_user_id={canonical_user_id}",
                f"query={query}",
                "",
                "Recent Session:",
                *(recent_lines if recent_lines else ["- (none)"]),
                "",
                "Relevant Long-Term Memories:",
                *memory_lines,
            ]
        )

    def _resolve_db_path(self) -> str:
        cwd = os.getcwd()
        plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = []

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            candidates.append(os.path.join(get_astrbot_data_path(), "plugin_data", self.plugin_name))
        except Exception:
            pass

        candidates.extend(
            [
                os.path.join(cwd, "data", "plugin_data", self.plugin_name),
                os.path.join(cwd, "plugin_data", self.plugin_name),
                os.path.join(plugin_root, "data"),
            ]
        )

        for path in candidates:
            try:
                os.makedirs(path, exist_ok=True)
                return os.path.join(path, "tmemory.db")
            except OSError:
                continue

        return os.path.join(plugin_root, "tmemory.db")

    def _init_db(self):
        self._db_mgr.init_db(self._vec_available, getattr(self, "embed_dim", 768))

    def _migrate_schema(self, conn: Optional[sqlite3.Connection] = None):
        if conn is None:
            # For backward compatibility with tests calling this manually
            with self._db() as _conn:
                self._db_mgr.migrate_schema(_conn)
        else:
            self._db_mgr.migrate_schema(conn)

    def _db(self) -> _LockedConnection:
        return self._db_mgr.db()

    def _close_db(self) -> None:
        self._db_mgr.close()

    async def _get_http_session(self):
        return await _vector.get_http_session(self)

    async def _embed_text(self, text: str) -> Optional[List[float]]:
        return await _vector.embed_text(self, text)

    async def _upsert_vector(self, memory_id: int, text: str) -> bool:
        return await _vector.upsert_vector(self, memory_id, text)

    def _delete_vector(self, memory_id: int, conn=None) -> None:
        from astrbot_plugin_tmemory.core import vector as _vec
        _vec.delete_vector(self, memory_id, conn)

    def _delete_vectors_for_user(self, canonical_id: str, conn=None) -> None:
        from astrbot_plugin_tmemory.core import vector as _vec
        _vec.delete_vectors_for_user(self, canonical_id, conn)

    async def _rebuild_vector_index(self) -> Tuple[int, int]:
        return await _vector.rebuild_vector_index(self)

    def _log_memory_event(
        self, canonical_user_id: str, event_type: str,
        payload: Dict[str, object], conn: Optional[sqlite3.Connection] = None,
    ):
        """记录记忆相关事件到审计日志 memory_events。"""
        _memory_ops.log_memory_event(self, canonical_user_id, event_type, payload, conn)

    def _safe_get_unified_msg_origin(self, event: AstrMessageEvent) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _platform_str(val):
        try:
            from astrbot.core.platform.platform_metadata import PlatformMetadata  # type: ignore
            if isinstance(val, PlatformMetadata):
                return val.id or val.name
        except ImportError:
            pass
        return str(val)

    def _get_adapter_name(self, event: AstrMessageEvent) -> str:
        for name in ("get_platform_name", "get_adapter_name", "get_client_name"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    val = fn()
                    if val:
                        return str(val)
                except Exception:
                    pass

        for attr in ("platform_name", "adapter_name", "adapter", "platform"):
            val = getattr(event, attr, None)
            if val:
                return self._platform_str(val)

        return "unknown_adapter"

    def _get_adapter_user_id(self, event: AstrMessageEvent) -> str:
        for name in ("get_sender_id", "get_user_id"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    val = fn()
                    if val:
                        return str(val)
                except Exception:
                    pass

        sender_name = getattr(event, "get_sender_name", None)
        if callable(sender_name):
            try:
                val = sender_name()
                if val:
                    return str(val)
            except Exception:
                pass

        return "unknown_user"

    def _get_memory_scope(self, event: AstrMessageEvent) -> str:
        """根据 memory_scope 配置和消息类型确定本次的 scope 标签。"""
        if self._cfg.memory_scope == "session":
            try:
                from astrbot.core.platform import MessageType  # type: ignore

                if event.get_message_type() == MessageType.FRIEND_MESSAGE:
                    return "private"
                gid = event.get_group_id()
                return f"group:{gid}" if gid else "private"
            except Exception:
                return "private"
        return "user"

    async def _get_current_persona_async(self, event: AstrMessageEvent) -> str:
        """使用 AstrBot conversation_manager 异步获取当前人格 ID。"""
        try:
            umo = self._safe_get_unified_msg_origin(event)
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr and umo:
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid:
                    conv = await conv_mgr.get_conversation(umo, cid)
                    if conv and getattr(conv, "persona_id", None):
                        return str(conv.persona_id)
        except Exception:
            pass
        return self._get_current_persona(event)

    def _get_current_persona(self, event: AstrMessageEvent) -> str:
        """同步获取人格 ID(fallback)。优先从 event extras 获取，否则返回空。"""
        try:
            # AstrBot 在某些版本中将 conversation 挂到 event extras
            extras = getattr(event, "_extras", {}) or {}
            conv = extras.get("conversation") or getattr(event, "conversation", None)
            if conv:
                persona = getattr(conv, "persona_id", None)
                if persona:
                    return str(persona)
        except Exception:
            pass
        return ""

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        """是否为群聊事件。"""
        try:
            from astrbot.core.platform import MessageType  # type: ignore

            return event.get_message_type() != MessageType.FRIEND_MESSAGE
        except Exception:
            gid = event.get_group_id()
            return bool(gid)

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
                SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, updated_at, is_pinned
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
    ) -> List[Dict[str, object]]:
        """从 memories 表中检索最相关的记忆，按综合评分排序。只返回 is_active=1 的有效记忆。"""
        # 步骤 1:获取查询向量
        query_vec: Optional[List[float]] = None
        if self._vec_available and query:
            query_vec = await self._embed_text(query)

        # 步骤 2:底层 DB 检索
        scored, _ = await self._retrieval_mgr.retrieve_memories(
            canonical_id=canonical_id,
            query=query,
            limit=limit,
            query_vec=query_vec,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private
        )

        # 去重:高语义重叠的记忆只保留分数最高的那条
        deduped = self._retrieval_mgr.deduplicate_results(scored, limit * 2)

        # 可选 Reranker:对候选结果做精排
        if self._cfg.enable_reranker and self._cfg.rerank_base_url and query and len(deduped) > 1:
            top_result = await self._rerank_results(query, deduped, limit)
        else:
            top_result = deduped[:limit]

        # 对命中的 top 结果进行强化:reinforce_count += 1，批量更新减少 DB 开销
        if top_result:
            reinforce_now = self._now()
            reinforce_ids = [int(item["id"]) for item in top_result]
            placeholders = ",".join(["?"] * len(reinforce_ids))
            with self._db() as conn:
                conn.execute(
                    f"UPDATE memories SET reinforce_count = reinforce_count + 1,"
                    f" last_seen_at = ? WHERE id IN ({placeholders})",
                    [reinforce_now, *reinforce_ids],
                )

        return top_result

    async def _manual_purify_memories(
        self,
        event: AstrMessageEvent,
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
        event: AstrMessageEvent,
        rows: List[Dict[str, object]],
        mode: str,
        extra_instruction: str,
    ) -> Dict[str, object]:
        return await _maintenance.llm_purify_operations(self, event, rows, mode, extra_instruction)

    async def _manual_refine_memories(
        self,
        event: AstrMessageEvent,
        canonical_id: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
    ) -> Dict[str, object]:
        """兼容旧方法名，等价 _manual_purify_memories。"""
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
        self, event: AstrMessageEvent, memory_text: str
    ) -> List[str]:
        return await _maintenance.llm_split_memory(self, event, memory_text)

    def _parse_json_object(self, text: str) -> Optional[Dict[str, object]]:
        """从文本中提取 JSON 对象。"""
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
        self, memory_id: int, memory: str, memory_type: str,
        score: float, importance: float, confidence: float,
    ) -> None:
        from astrbot_plugin_tmemory.core import memory_ops as _mo
        _mo.update_memory_full(self, memory_id, memory, memory_type, score, importance, confidence)

    def _auto_merge_memory_text(self, memories: List[str]) -> str:
        """无 LLM 时的简单合并策略:去重后拼接。"""
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
        if not merged.startswith("用户"):
            merged = f"用户{merged}"
        return merged[:300]

    async def _rerank_results(
        self, query: str, candidates: List[Dict[str, object]], top_n: int
    ) -> List[Dict[str, object]]:
        return await _vector.rerank_results(self, query, candidates, top_n)

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
        self, canonical_id: str, role: str, content: str, source_adapter: str,
        source_user_id: str, unified_msg_origin: str, scope: str = "user", persona_id: str = "",
    ):
        from astrbot_plugin_tmemory.core import maintenance as _m
        _m.insert_conversation_sync(
            self, canonical_id, role, content, source_adapter,
            source_user_id, unified_msg_origin, scope, persona_id,
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
                WHERE distilled=0
                GROUP BY canonical_user_id
                HAVING cnt >= ?
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (min_required, limit),
            ).fetchall()
        return [str(r["canonical_user_id"]) for r in rows]

    def _fetch_pending_rows(self, canonical_id: str, limit: int) -> List[Dict]:
        """获取待蒸馏的对话行，返回 dict 列表(避免 sqlite3.Row 在 async 上下文中的问题)。"""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, canonical_user_id, role, content, source_adapter, source_user_id, unified_msg_origin, scope, persona_id
                FROM conversation_cache
                WHERE canonical_user_id=? AND distilled=0
                ORDER BY id ASC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()
            # 立即转换为普通 dict，防止 Connection 关闭后访问失效
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
                "SELECT COUNT(1) AS n FROM (SELECT canonical_user_id FROM conversation_cache WHERE distilled=0 GROUP BY canonical_user_id)"
            ).fetchone()
        return int(row["n"] if row else 0)

    def _count_pending_rows(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS n FROM conversation_cache WHERE distilled=0"
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
        """对超出阈值的历史做轻量规则摘要压缩，不触发 LLM，以节省 token。"""
        _maintenance.optimize_context(self, canonical_id)

    def _set_pinned(self, memory_id: int, pinned: bool) -> bool:
        """设置/取消常驻标记。常驻记忆不会被衰减、剪枝、冲突覆盖。"""
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

    def _build_sanitize_patterns(self) -> list:
        """构建脱敏正则列表。"""
        return [
            (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
            (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱]"),
            (re.compile(r"\d{17}[\dXx]"), "[身份证]"),
            (re.compile(r"\d{15,19}"), "[长数字]"),
        ]

    def _sanitize_text(self, text: str) -> str:
        """对文本进行敏感信息脱敏。"""
        for pattern, replacement in self._sanitize_patterns:
            text = pattern.sub(replacement, text)
        return text

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _safe_memory_type(self, value: object) -> str:
        s = str(value or "fact").strip().lower()
        if s in {"preference", "fact", "task", "restriction", "style"}:
            return s
        return "fact"

    def _clamp01(self, value: object) -> float:  # type: ignore[arg-type]
        try:
            num = float(value)  # type: ignore[arg-type]
        except Exception:
            num = 0.0
        return max(0.0, min(1.0, num))

    def _get_global_stats(self) -> Dict[str, int]:
        """获取全局统计信息。"""
        return _maintenance.get_global_stats(self)


# =============================================================================
# Plugin decorated-handler implementation mixin
# =============================================================================


class PluginHandlersMixin:
    async def _handle_on_any_message(self, event: AstrMessageEvent):
        """自动采集每条用户消息。"""
        if not self._cfg.enable_auto_capture:
            return

        text = self._normalize_text(getattr(event, "message_str", "") or "")
        if not text:
            return

        command_text = text[1:] if text.startswith("/") else text
        first_word = command_text.split(maxsplit=1)[0] if command_text else ""
        if first_word in _CMD_FIRST_WORDS:
            return
        if text.startswith("/"):
            return

        if self._capture_filter.should_skip_capture(text):
            return

        canonical_id, adapter, adapter_user = self._identity_mgr.resolve_current_identity(event)
        umo = self._safe_get_unified_msg_origin(event)

        await self._insert_conversation(
            canonical_id=canonical_id,
            role="user",
            content=self._sanitize_text(text),
            source_adapter=adapter,
            source_user_id=adapter_user,
            unified_msg_origin=umo,
            scope=self._get_memory_scope(event),
            persona_id=self._get_current_persona(event),
        )

    async def _handle_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """可选采集模型回复，作为后续批量蒸馏素材。"""
        if not (self._cfg.enable_auto_capture and self._cfg.capture_assistant_reply):
            return

        text = self._normalize_text(getattr(resp, "completion_text", "") or "")
        if not text:
            return

        if self._capture_filter.should_skip_capture(text):
            return

        try:
            canonical_id, adapter, adapter_user = self._identity_mgr.resolve_current_identity(event)
            umo = self._safe_get_unified_msg_origin(event)
            await self._insert_conversation(
                canonical_id=canonical_id,
                role="assistant",
                content=text,
                source_adapter=adapter,
                source_user_id=adapter_user,
                unified_msg_origin=umo,
            )
        except Exception as e:
            logger.warning("[tmemory] on_llm_response capture failed: %s", e)

    async def _handle_on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 调用前注入记忆。"""
        if not self._cfg.enable_memory_injection:
            return

        try:
            canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
            query = self._normalize_text(getattr(req, "prompt", "") or "")
            scope = self._get_memory_scope(event)
            persona_id = await self._get_current_persona_async(event)
            is_group = self._is_group_event(event)
            exclude_private = (is_group and not self._cfg.private_memory_in_group)

            # ── 知识记忆注入：遵循 inject_position ──
            knowledge_block = await self._build_knowledge_injection(
                canonical_id, query, self._cfg.inject_memory_limit,
                scope=scope, persona_id=persona_id, exclude_private=exclude_private,
            )
            if knowledge_block:
                self._inject_block_by_position(req, knowledge_block)
        except Exception as e:
            logger.warning("[tmemory] on_llm_request inject failed: %s", e)

    async def _handle_tool_remember(
        self, event: AstrMessageEvent, content: str, memory_type: str
    ):
        """记住用户的重要信息。当对话中出现值得长期记住的用户偏好、事实、任务、限制或风格时，主动调用此工具保存。

        Args:
            content(string): 要记住的内容，用简洁的陈述句描述
            memory_type(string): 记忆类型，可选值：preference（偏好）、fact（事实）、task（任务）、restriction（限制）、style（风格）
        """
        if self._cfg.memory_mode == "distill_only":
            return "记忆工具当前已禁用（模式为 distill_only）。"

        content = self._normalize_text(content or "")
        if not content or len(content) < 4:
            return "内容过短，未保存。"

        memory_type = self._safe_memory_type(memory_type)

        # 安全审计
        if self._is_unsafe_memory(content):
            return "内容未通过安全审计，未保存。"
        if self._is_junk_memory(content):
            return "内容信息量过低，未保存。"

        canonical_id, adapter, adapter_user = self._identity_mgr.resolve_current_identity(event)
        scope = self._get_memory_scope(event)
        persona_id = self._get_current_persona(event)

        new_id = self._insert_memory(
            canonical_id=canonical_id,
            adapter=adapter,
            adapter_user=adapter_user,
            memory=self._sanitize_text(content),
            score=0.80,
            memory_type=memory_type,
            importance=0.70,
            confidence=0.85,
            source_channel="active_tool",
            persona_id=persona_id,
            scope=scope,
        )

        if self._vec_available and new_id:
            try:
                await self._upsert_vector(new_id, content)
            except Exception:
                pass  # 向量写入失败不影响核心流程

        return f"已记住（id={new_id}, type={memory_type}）。"

    async def _handle_tool_recall(self, event: AstrMessageEvent, query: str):
        """检索与查询相关的用户记忆。当需要回忆用户的偏好、历史信息或之前提到的内容时调用。

        Args:
            query(string): 查询文本，描述想要回忆的内容
        """
        if self._cfg.memory_mode == "distill_only":
            return "记忆工具当前已禁用（模式为 distill_only）。"

        query = self._normalize_text(query or "")
        if not query:
            return "查询内容为空。"

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        scope = self._get_memory_scope(event)
        persona_id = self._get_current_persona(event)
        is_group = self._is_group_event(event)

        try:
            rows = await self._retrieve_memories(
                canonical_id,
                query,
                limit=self._cfg.inject_memory_limit,
                scope=scope,
                persona_id=persona_id,
                exclude_private=(is_group and not self._cfg.private_memory_in_group),
            )
        except Exception as e:
            logger.warning("[tmemory] recall tool retrieval failed: %s", e)
            return "检索记忆时出现错误。"

        if not rows:
            return "未找到相关记忆。"

        lines = []
        for row in rows:
            mtype = row["memory_type"]
            mem = row["memory"]
            lines.append(f"- ({mtype}) {mem}")
        return "\n".join(lines)

    async def _handle_tm_distill_now(self, event: AstrMessageEvent):
        """手动触发一次批量蒸馏:/tm_distill_now"""
        processed_users, total_memories = await self._run_distill_cycle(
            force=True, trigger="manual_cmd"
        )
        yield event.plain_result(
            f"批量蒸馏完成:处理用户 {processed_users} 个，新增/更新记忆 {total_memories} 条。"
        )

    async def _handle_tm_worker(self, event: AstrMessageEvent):
        """查看蒸馏 worker 状态:/tm_worker"""
        pending_users = self._count_pending_users()
        pending_rows = self._count_pending_rows()
        lines = [
            f"worker_running={self._worker_running}",
            f"distill_interval_sec={self._cfg.distill_interval_sec}",
            f"distill_min_batch_count={self._cfg.distill_min_batch_count}",
            f"distill_batch_limit={self._cfg.distill_batch_limit}",
            f"--- 记忆蒸馏 (memory distill) ---",
            f"enable_auto_capture={self._cfg.enable_auto_capture}",
            f"distill_pause={self._cfg.distill_pause}",
            f"memory_mode={self._cfg.memory_mode}",
            f"pending_users={pending_users}",
            f"pending_rows={pending_rows}",
            f"--- gate stats ---",
            f"capture_min_content_len={self._cfg.capture_min_content_len}",
            f"capture_dedup_window={self._cfg.capture_dedup_window}",
            f"distill_user_throttle_sec={self._cfg.distill_user_throttle_sec}",
            f"distill_skipped_rows(lifetime)={self._distill_skipped_rows}",
            f"throttled_users={sum(1 for ts in self._user_last_distilled_ts.values() if time.time() - ts < self._cfg.distill_user_throttle_sec) if self._cfg.distill_user_throttle_sec > 0 else 'N/A'}",
        ]
        yield event.plain_result("\n".join(lines))

    async def _handle_tm_memory(self, event: AstrMessageEvent):
        """查看当前用户的记忆:/tm_memory"""
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        memories = self._list_memories(canonical_id, limit=8)
        if not memories:
            yield event.plain_result("当前还没有已保存记忆。")
            return

        lines = [f"canonical_id={canonical_id}"]
        for row in memories:
            pin = "📌 " if row.get("is_pinned") else ""
            lines.append(
                f"[{row['id']}] {pin}[{row['memory_type']}] s={row['score']:.2f} i={row['importance']:.2f} c={row['confidence']:.2f} r={row['reinforce_count']} | {row['memory']}"
            )
        yield event.plain_result("\n".join(lines))

    async def _handle_tm_context(self, event: AstrMessageEvent):
        """预览记忆召回上下文:/tm_context 今天吃什么"""
        raw = (event.message_str or "").strip()
        query = re.sub(r"^/tm_context\s*", "", raw, flags=re.IGNORECASE).strip()
        if not query:
            yield event.plain_result("用法: /tm_context <当前问题>")
            return

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        context_block = await self.build_memory_context(canonical_id, query, limit=6)
        yield event.plain_result(context_block)

    async def _handle_tm_bind(self, event: AstrMessageEvent):
        """绑定当前账号到统一用户 ID:/tm_bind alice"""
        raw = (event.message_str or "").strip()
        canonical_id = re.sub(r"^/tm_bind\s*", "", raw, flags=re.IGNORECASE).strip()
        if not canonical_id:
            yield event.plain_result("用法: /tm_bind <统一用户ID>")
            return

        adapter = self._get_adapter_name(event)
        adapter_user = self._get_adapter_user_id(event)
        self._identity_mgr.bind_identity(adapter, adapter_user, canonical_id)
        yield event.plain_result(
            f"绑定成功:{adapter}:{adapter_user} -> {canonical_id}"
        )

    async def _handle_tm_merge(self, event: AstrMessageEvent):
        """合并两个统一用户 ID 的记忆:/tm_merge old_id new_id"""
        raw = (event.message_str or "").strip()
        args = re.sub(r"^/tm_merge\s*", "", raw, flags=re.IGNORECASE).strip().split()
        if len(args) != 2:
            yield event.plain_result(
                "用法: /tm_merge <from_canonical_id> <to_canonical_id>"
            )
            return

        from_id, to_id = args[0], args[1]
        if from_id == to_id:
            yield event.plain_result("两个 ID 相同，无需合并。")
            return

        moved = self._identity_mgr.merge_identity(from_id, to_id)
        self._delete_vectors_for_user(from_id)
        self._merge_needs_vector_rebuild = True
        yield event.plain_result(
            f"合并完成:{from_id} -> {to_id}，迁移记忆 {moved} 条。"
        )

    async def _handle_tm_forget(self, event: AstrMessageEvent):
        """删除一条记忆:/tm_forget 12"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_forget\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("用法: /tm_forget <记忆ID>")
            return

        deleted = self._delete_memory(int(arg))
        if deleted:
            yield event.plain_result(f"已删除记忆 {arg}")
            return
        yield event.plain_result(f"未找到记忆 {arg}")

    async def _handle_tm_stats(self, event: AstrMessageEvent):
        """查看全局统计信息:/tm_stats"""
        stats = self._get_global_stats()
        lines = [
            f"total_users: {stats['total_users']}",
            f"total_active_memories: {stats['total_active_memories']}",
            f"total_deactivated_memories: {stats['total_deactivated_memories']}",
            f"pending_cached_rows: {stats['pending_cached_rows']}",
            f"total_events: {stats['total_events']}",
        ]
        if self._vec_available:
            lines.append(f"vector_index_rows: {stats.get('vector_index_rows', 0)}")
            lines.append(
                f"embed_ok/fail: {self._embed_ok_count}/{self._embed_fail_count}"
            )
            hit_rate = (
                f"{self._vec_hit_count}/{self._vec_query_count}"
                f" ({self._vec_hit_count * 100 // max(1, self._vec_query_count)}%)"
                if self._vec_query_count > 0
                else "N/A"
            )
            lines.append(f"vector_hit_rate: {hit_rate}")
            if self._embed_last_error:
                lines.append(f"embed_last_error: {self._embed_last_error[:80]}")
        elif self._cfg.enable_vector_search:
            lines.append("vector_search: enabled but sqlite-vec not installed")

        # 最近 10 轮蒸馏 token 累计
        distill_cost = self._get_distill_cost_summary(last_n=10)
        lines.append("--- distill cost (last 10 runs) ---")
        if distill_cost["has_usage"]:
            lines.append(f"distill_runs: {distill_cost['runs']}")
            lines.append(f"distill_tokens_input: {distill_cost['tokens_input']}")
            lines.append(f"distill_tokens_output: {distill_cost['tokens_output']}")
            lines.append(f"distill_tokens_total: {distill_cost['tokens_total']}")
        else:
            lines.append(
                f"distill_runs: {distill_cost['runs']} (no usage data from provider)"
            )

        yield event.plain_result("\n".join(lines))

    async def _handle_tm_distill_history(self, event: AstrMessageEvent):
        """查看最近蒸馏历史（含 token 成本）:/tm_distill_history"""
        rows = self._get_distill_history(limit=10)
        if not rows:
            yield event.plain_result("暂无蒸馏历史记录。")
            return

        lines = [f"最近 {len(rows)} 轮蒸馏历史（最新优先）:"]
        for r in rows:
            tok_in = r.get("tokens_input", -1)
            tok_out = r.get("tokens_output", -1)
            tok_total = r.get("tokens_total", -1)
            tok_str = (
                f"in={tok_in} out={tok_out} total={tok_total}"
                if tok_total >= 0
                else "tokens=N/A"
            )
            lines.append(
                f"[{r['id']}] {r['started_at'][:16]} trigger={r['trigger_type']}"
                f" users={r['users_processed']} mems={r['memories_created']}"
                f" failed={r['users_failed']} dur={r['duration_sec']:.1f}s"
                f" {tok_str}"
            )
        yield event.plain_result("\n".join(lines))

    async def _handle_tm_purify(self, event: AstrMessageEvent):
        """手动触发一次记忆提纯:/tm_purify"""
        yield event.plain_result("开始记忆提纯，请稍候…")
        pruned, kept = await self._run_memory_purify()
        yield event.plain_result(
            f"记忆提纯完成:失活低质量记忆 {pruned} 条，保留 {kept} 条。"
        )

    async def _handle_tm_quality_refine(self, event: AstrMessageEvent):
        """兼容旧命令:/tm_quality_refine(等价 /tm_purify)"""
        async for msg in self._handle_tm_purify(event):
            yield msg

    async def _handle_tm_vec_rebuild(self, event: AstrMessageEvent):
        """重建向量索引:/tm_vec_rebuild 或 /tm_vec_rebuild force=true"""
        if not self._vec_available:
            yield event.plain_result(
                "向量检索未启用或 sqlite-vec 未安装。\n"
                "请先安装:pip install sqlite-vec，并在配置中开启 enable_vector_search。"
            )
            return
        if not self._cfg.embed_provider_id:
            yield event.plain_result("未配置 embed_provider_id，无法生成向量。")
            return

        raw = (event.message_str or "").strip()
        force = "force=true" in raw.lower() or "force" in raw.lower()

        if force:
            yield event.plain_result("全量重建模式:清空现有向量后重建，请稍候...")
            with self._db() as conn:
                try:
                    conn.execute("DELETE FROM memory_vectors")
                except Exception:
                    pass
        else:
            yield event.plain_result("增量补全模式:只补缺失向量，请稍候...")

        ok, fail = await self._rebuild_vector_index()
        yield event.plain_result(
            f"向量索引重建完成:成功 {ok} 条，跳过/失败 {fail} 条。"
        )

    async def _handle_tm_refine(self, event: AstrMessageEvent):
        """手动提纯已产生记忆。

        用法:
        /tm_refine mode=both limit=20 dry_run=false include_pinned=false <附加要求>

        参数:
        - mode: merge | split | both
        - limit: 处理记忆条数上限
        - dry_run: true/false 仅预览不落库
        - include_pinned: 是否允许处理常驻记忆
        """
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_refine\s*", "", raw, flags=re.IGNORECASE).strip()

        opts = {
            "mode": self._cfg.manual_purify_default_mode,
            "limit": str(self._cfg.manual_purify_default_limit),
            "dry_run": "false",
            "include_pinned": "false",
        }
        for m in re.finditer(
            r"(mode|limit|dry_run|include_pinned)=([^\s]+)",
            body,
            flags=re.IGNORECASE,
        ):
            opts[m.group(1).lower()] = m.group(2)
        extra = re.sub(
            r"(mode|limit|dry_run|include_pinned)=([^\s]+)",
            "",
            body,
            flags=re.IGNORECASE,
        ).strip()

        mode = str(opts["mode"]).lower()
        if mode not in {"merge", "split", "both"}:
            yield event.plain_result("mode 仅支持 merge|split|both")
            return

        try:
            limit = max(1, min(200, int(opts["limit"])))
        except Exception:
            limit = 20
        dry_run = str(opts["dry_run"]).lower() in {"1", "true", "yes", "y", "on"}
        include_pinned = str(opts["include_pinned"]).lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        result = await self._manual_purify_memories(
            event=event,
            canonical_id=canonical_id,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra,
        )

        yield event.plain_result(
            "\n".join(
                [
                    f"manual_purify done (dry_run={dry_run})",
                    f"user={canonical_id}",
                    f"mode={mode}, limit={limit}, include_pinned={include_pinned}",
                    f"updates={result['updates']}, adds={result['adds']}, deletes={result['deletes']}",
                    f"note={result.get('note', '')}",
                ]
            )
        )

    async def _handle_tm_mem_merge(self, event: AstrMessageEvent):
        """手动合并多条记忆。

        用法:
        /tm_mem_merge 12,18,33 用户偏好吃火锅但关注体重管理
        """
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_mem_merge\s*", "", raw, flags=re.IGNORECASE).strip()
        if not body:
            yield event.plain_result(
                "用法: /tm_mem_merge <id1,id2,...> <合并后的记忆文本>"
            )
            return

        parts = body.split(None, 1)
        ids_part = parts[0]
        merged_text = parts[1].strip() if len(parts) > 1 else ""
        ids = [int(x) for x in re.split(r"[,，]", ids_part) if x.strip().isdigit()]
        if len(ids) < 2:
            yield event.plain_result(
                "请至少提供两个记忆ID，例如 /tm_mem_merge 12,18 新记忆内容"
            )
            return

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        rs = self._fetch_memories_by_ids(canonical_id, ids)
        if len(rs) < 2:
            yield event.plain_result("这些ID中可用记忆不足两条(可能不属于当前用户)")
            return

        if not merged_text:
            merged_text = self._auto_merge_memory_text([str(r["memory"]) for r in rs])

        keep_id = int(rs[0]["id"])
        self._update_memory_text(keep_id, merged_text)
        if self._vec_available:
            await self._upsert_vector(keep_id, merged_text)

        for r in rs[1:]:
            self._delete_memory(int(r["id"]))

        yield event.plain_result(f"合并完成:保留 #{keep_id}，删除 {len(rs) - 1} 条")

    async def _handle_tm_mem_split(self, event: AstrMessageEvent):
        """手动拆分一条记忆。

        用法:
        /tm_mem_split 12 片段A|片段B|片段C
        /tm_mem_split 12   # 不给片段时自动调用 LLM 拆分
        """
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_mem_split\s*", "", raw, flags=re.IGNORECASE).strip()
        if not body:
            yield event.plain_result("用法: /tm_mem_split <id> [片段1|片段2|...]")
            return

        parts = body.split(None, 1)
        if not parts[0].isdigit():
            yield event.plain_result("第一个参数必须是记忆ID")
            return
        mem_id = int(parts[0])
        custom = parts[1].strip() if len(parts) > 1 else ""

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        row = self._fetch_memory_by_id(canonical_id, mem_id)
        if not row:
            yield event.plain_result(f"未找到记忆 {mem_id}")
            return

        if custom:
            segments = [
                self._normalize_text(x)
                for x in custom.split("|")
                if self._normalize_text(x)
            ]
        else:
            segments = await self._llm_split_memory(event, str(row["memory"]))

        if len(segments) < 2:
            yield event.plain_result("拆分结果不足两段，未执行写入")
            return

        self._update_memory_text(mem_id, segments[0])
        if self._vec_available:
            await self._upsert_vector(mem_id, segments[0])

        added = 0
        for seg in segments[1:]:
            new_id = self._insert_memory(
                canonical_id=canonical_id,
                adapter=str(row["source_adapter"]),
                adapter_user=str(row["source_user_id"]),
                memory=seg,
                score=float(row["score"]),
                memory_type=str(row["memory_type"]),
                importance=float(row["importance"]),
                confidence=float(row["confidence"]),
                source_channel="manual_split",
            )
            if self._vec_available and new_id:
                await self._upsert_vector(new_id, seg)
            added += 1

        yield event.plain_result(f"拆分完成:原记忆#{mem_id} + 新增 {added} 条")

    async def _handle_tm_pin(self, event: AstrMessageEvent):
        """常驻一条记忆(不会被衰减/剪枝/冲突覆盖):/tm_pin 12"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_pin\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("用法: /tm_pin <记忆ID>")
            return
        ok = self._set_pinned(int(arg), True)
        yield event.plain_result(
            f"记忆 {arg} 已设为常驻" if ok else f"未找到记忆 {arg}"
        )

    async def _handle_tm_unpin(self, event: AstrMessageEvent):
        """取消常驻一条记忆:/tm_unpin 12"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_unpin\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("用法: /tm_unpin <记忆ID>")
            return
        ok = self._set_pinned(int(arg), False)
        yield event.plain_result(
            f"记忆 {arg} 已取消常驻" if ok else f"未找到记忆 {arg}"
        )

    async def _handle_tm_export(self, event: AstrMessageEvent):
        """导出当前用户的所有记忆(JSON):/tm_export"""
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        data = self._export_user_data(canonical_id)
        yield event.plain_result(json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    async def _handle_tm_purge(self, event: AstrMessageEvent):
        """删除当前用户的所有记忆和缓存:/tm_purge"""
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        deleted = self._purge_user_data(canonical_id)
        yield event.plain_result(
            f"已清除 {canonical_id} 的所有数据:{deleted['memories']} 条记忆，{deleted['cache']} 条缓存。"
        )



