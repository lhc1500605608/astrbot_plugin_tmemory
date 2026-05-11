from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import jieba
from astrbot.api import logger

from . import maintenance as _maintenance
from . import memory_ops as _memory_ops
from . import vector as _vector
from .data_access import DataAccessMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import ProviderRequest
    from .db import _LockedConnection


class PluginHelpersMixin(DataAccessMixin):
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
        items = self._retrieval_mgr.retrieve_profile_items(
            canonical_user_id, query, limit,
            scope=scope, persona_id=persona_id, exclude_private=exclude_private,
        )
        if not items:
            return ""

        from .injection import InjectionBuilder
        block = InjectionBuilder._assemble_profile_blocks(items)
        if self._cfg.inject_max_chars > 0 and len(block) > self._cfg.inject_max_chars:
            cutoff = max(self._cfg.inject_max_chars - 3, 1)
            block = block[:cutoff] + "\u2026"
        return block

    def _inject_block_by_position(self, req: ProviderRequest, block: str) -> None:
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
        elif self._cfg.inject_position == "extra_user_temp":
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=block)
            mark_fn = getattr(part, "mark_as_temp", None)
            if callable(mark_fn):
                mark_fn()
                if (
                    not hasattr(req, "extra_user_content_parts")
                    or req.extra_user_content_parts is None
                ):
                    req.extra_user_content_parts = []
                req.extra_user_content_parts.append(part)
            else:
                logger.warning(
                    "[tmemory] mark_as_temp() not available in this AstrBot "
                    "version; falling back to system_prompt injection for "
                    "extra_user_temp position"
                )
                existing = getattr(req, "system_prompt", "") or ""
                req.system_prompt = existing + ("\n\n" if existing else "") + block
        else:  # system_prompt
            existing = getattr(req, "system_prompt", "") or ""
            req.system_prompt = existing + ("\n\n" if existing else "") + block

    async def build_memory_context(
        self, canonical_user_id: str, query: str, limit: int = 6
    ) -> str:
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
            memory_lines = ["- (none) \u6682\u65e0\u5339\u914d\u957f\u671f\u8bb0\u5fc6"]

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
        candidates = []

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            candidates.append(
                os.path.join(get_astrbot_data_path(), "plugin_data", self.plugin_name)
            )
        except Exception:
            pass

        candidates.extend(
            [
                os.path.join(cwd, "data", "plugin_data", self.plugin_name),
                os.path.join(cwd, "plugin_data", self.plugin_name),
            ]
        )

        for path in candidates:
            try:
                os.makedirs(path, exist_ok=True)
                return os.path.join(path, "tmemory.db")
            except OSError:
                continue

        raise RuntimeError(f"[tmemory] \u65e0\u6cd5\u521b\u5efa\u6301\u4e45\u5316\u6570\u636e\u76ee\u5f55\u3002\u5df2\u5c1d\u8bd5: {candidates}")

    def _init_db(self):
        self._db_mgr.init_db(self._vec_available, getattr(self, "embed_dim", 768))

    def _migrate_schema(self, conn: Optional[sqlite3.Connection] = None):
        if conn is None:
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

    async def _upsert_profile_vector(self, profile_item_id: int, text: str) -> bool:
        return await _vector.upsert_profile_vector(self, profile_item_id, text)

    def _delete_vector(self, memory_id: int, conn=None) -> None:
        from . import vector as _vec

        _vec.delete_vector(self, memory_id, conn)

    def _delete_vectors_for_user(self, canonical_id: str, conn=None) -> None:
        from . import vector as _vec

        _vec.delete_vectors_for_user(self, canonical_id, conn)

    async def _rebuild_vector_index(self) -> Tuple[int, int]:
        return await _vector.rebuild_vector_index(self)

    def _log_memory_event(
        self,
        canonical_user_id: str,
        event_type: str,
        payload: Dict[str, object],
        conn: Optional[sqlite3.Connection] = None,
    ):
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
        try:
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
        try:
            from astrbot.core.platform import MessageType  # type: ignore

            return event.get_message_type() != MessageType.FRIEND_MESSAGE
        except Exception:
            gid = event.get_group_id()
            return bool(gid)

    def _build_sanitize_patterns(self) -> list:
        return [
            (re.compile(r"1[3-9]\d{9}"), "[\u624b\u673a\u53f7]"),
            (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[\u90ae\u7bb1]"),
            (re.compile(r"\d{17}[\dXx]"), "[\u8eab\u4efd\u8bc1]"),
            (re.compile(r"\d{15,19}"), "[\u957f\u6570\u5b57]"),
        ]

    def _sanitize_text(self, text: str) -> str:
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
