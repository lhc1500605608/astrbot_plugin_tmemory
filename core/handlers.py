from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, List, Optional

from . import vector as _vector
from .commands import CommandHandlersMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest

logger = logging.getLogger("astrbot")

# recall_for_prompt 公共 API 约束（TMEAAA-396）
_RECALL_FOR_PROMPT_TIMEOUT_SEC = 2.0
_RECALL_FOR_PROMPT_MAX_CHARS = 200
_RECALL_FOR_PROMPT_MAX_LIMIT = 50


class PluginHandlersMixin(CommandHandlersMixin):

    # ── 事件钩子 ───────────────────────────────────────────────────────────

    async def _handle_on_any_message(self, event: AstrMessageEvent):
        if not self._cfg.enable_auto_capture:
            return

        text = self._normalize_text(getattr(event, "message_str", "") or "")
        if not text:
            return

        command_text = text[1:] if text.startswith("/") else text
        first_word = command_text.split(maxsplit=1)[0] if command_text else ""
        if first_word in self._cmd_first_words:
            return
        if text.startswith("/"):
            return

        if self._capture_filter.should_skip_capture(text):
            return

        canonical_id, adapter, adapter_user = (
            self._identity_mgr.resolve_current_identity(event)
        )
        umo = self._safe_get_unified_msg_origin(event)

        await self._insert_conversation(
            canonical_id=canonical_id,
            role="user",
            content=self._sanitize_text(text),
            source_adapter=adapter,
            source_user_id=adapter_user,
            unified_msg_origin=umo,
            scope=self._get_memory_scope(event),
            persona_id=await self._get_current_persona_async(event),
        )

    async def _handle_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not (self._cfg.enable_auto_capture and self._cfg.capture_assistant_reply):
            return

        text = self._normalize_text(getattr(resp, "completion_text", "") or "")
        if not text:
            return

        if self._capture_filter.should_skip_capture(text):
            return

        try:
            canonical_id, adapter, adapter_user = (
                self._identity_mgr.resolve_current_identity(event)
            )
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

    async def _handle_on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        if not self._cfg.enable_memory_injection:
            return

        try:
            canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
            query = self._normalize_text(getattr(req, "prompt", "") or "")
            scope = self._get_memory_scope(event)
            persona_id = await self._get_current_persona_async(event)
            is_group = self._is_group_event(event)
            exclude_private = is_group and not self._cfg.private_memory_in_group
            session_key = self._safe_get_unified_msg_origin(event)

            query_vec: Optional[List[float]] = None
            if (
                self._cfg.inject_enable_vector_search
                and self._vec_available
                and query
            ):
                self._vec_query_count += 1
                query_vec = await _vector.get_or_generate_query_embedding(
                    self, query
                )
                if query_vec is not None:
                    self._vec_hit_count += 1

            block = await self._injection_builder.build_profile_injection(
                canonical_id,
                query,
                session_key,
                query_vec=query_vec,
                scope=scope,
                persona_id=persona_id,
                exclude_private=exclude_private,
            )
            if block:
                self._inject_block_by_position(req, block)
        except Exception as e:
            logger.warning("[tmemory] on_llm_request inject failed: %s", e)

    # ── LLM Tool 处理器 ─────────────────────────────────────────────────────

    async def _handle_tool_remember(
        self, event: AstrMessageEvent, content: str, memory_type: str
    ):
        if self._cfg.memory_mode == "distill_only":
            return "\u8bb0\u5fc6\u5de5\u5177\u5f53\u524d\u5df2\u7981\u7528\uff08\u6a21\u5f0f\u4e3a distill_only\uff09\u3002"

        content = self._normalize_text(content or "")
        if not content or len(content) < 4:
            return "\u5185\u5bb9\u8fc7\u77ed\uff0c\u672a\u4fdd\u5b58\u3002"

        memory_type = self._safe_memory_type(memory_type)

        if self._is_unsafe_memory(content):
            return "\u5185\u5bb9\u672a\u901a\u8fc7\u5b89\u5168\u5ba1\u8ba1\uff0c\u672a\u4fdd\u5b58\u3002"
        if self._is_junk_memory(content):
            return "\u5185\u5bb9\u4fe1\u606f\u91cf\u8fc7\u4f4e\uff0c\u672a\u4fdd\u5b58\u3002"

        canonical_id, adapter, adapter_user = (
            self._identity_mgr.resolve_current_identity(event)
        )
        scope = self._get_memory_scope(event)
        persona_id = await self._get_current_persona_async(event)

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
                pass

        return f"\u5df2\u8bb0\u4f4f\uff08id={new_id}, type={memory_type}\uff09\u3002"

    async def _handle_tool_recall(self, event: AstrMessageEvent, query: str):
        if self._cfg.memory_mode == "distill_only":
            return "\u8bb0\u5fc6\u5de5\u5177\u5f53\u524d\u5df2\u7981\u7528\uff08\u6a21\u5f0f\u4e3a distill_only\uff09\u3002"

        query = self._normalize_text(query or "")
        if not query:
            return "\u67e5\u8be2\u5185\u5bb9\u4e3a\u7a7a\u3002"

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        scope = self._get_memory_scope(event)
        persona_id = await self._get_current_persona_async(event)
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
            return "\u68c0\u7d22\u8bb0\u5fc6\u65f6\u51fa\u73b0\u9519\u8bef\u3002"

        if not rows:
            return "\u672a\u627e\u5230\u76f8\u5173\u8bb0\u5fc6\u3002"

        lines = []
        for row in rows:
            mtype = row["memory_type"]
            mem = row["memory"]
            lines.append(f"- ({mtype}) {mem}")
        return "\n".join(lines)

    # ── 公共只读召回 API（供关联插件 kanjyou 注入主动消息）────────────────────

    async def recall_for_prompt(
        self,
        umo: str,
        query: str,
        session_type: str = "private",
        limit: int | None = None,
    ) -> list[str]:
        """只读、无副作用的记忆召回公共 API（TMEAAA-396）。

        复用 recall 工具的检索链路（``_retrieve_memories``，以 ``reinforce=False``
        调用避免写库），供 kanjyou 等在主动消息生成时注入真实记忆。

        - persona 隔离：按该会话 persona_id 维度检索（``''`` 视为通用）。
        - 隐私边界：``session_type == "group"`` 强制 ``exclude_private=True``，
          除非配置 ``private_memory_in_group``；私聊可含 private。
        - 不依赖 event：从 umo 优先经 ``conversation_cache`` / ``identity_bindings``
          解析 canonical_user_id。
        - 任何异常 / 未初始化 / 禁用（memory_mode=distill_only）返回 ``[]``，绝不抛出。
        - 内部超时 ≤2s；返回纯文本条目列表（每条 ≤200 字）。
        """
        try:
            if not self._recall_api_ready():
                return []
            norm_query = self._normalize_text(query or "")
            if not norm_query:
                return []
            return await asyncio.wait_for(
                self._recall_for_prompt_impl(umo, norm_query, session_type, limit),
                timeout=_RECALL_FOR_PROMPT_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.debug("[tmemory] recall_for_prompt failed: %s", e)
            return []

    def _recall_api_ready(self) -> bool:
        """未初始化 / 禁用（distill_only）时不可用。"""
        cfg = getattr(self, "_cfg", None)
        if cfg is None or getattr(self, "_db_mgr", None) is None:
            return False
        return getattr(cfg, "memory_mode", "hybrid") != "distill_only"

    async def _recall_for_prompt_impl(
        self, umo: str, query: str, session_type: str, limit: int | None
    ) -> list[str]:
        canonical_id = self._resolve_canonical_from_umo(umo)
        if not canonical_id:
            return []

        is_group = str(session_type or "").strip().lower() == "group"
        exclude_private = is_group and not bool(
            getattr(self._cfg, "private_memory_in_group", False)
        )
        scope = self._recall_scope(session_type, umo)
        persona_id = await self._resolve_persona_from_umo(umo)

        rows = await self._retrieve_memories(
            canonical_id,
            query,
            limit=self._recall_limit(limit),
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
            reinforce=False,
        )

        out: list[str] = []
        for row in rows or []:
            text = self._normalize_text(str(row.get("memory", "") or ""))
            if not text:
                continue
            if len(text) > _RECALL_FOR_PROMPT_MAX_CHARS:
                text = text[: _RECALL_FOR_PROMPT_MAX_CHARS - 1] + "…"
            out.append(text)
        return out

    def _recall_limit(self, limit: int | None) -> int:
        default = int(getattr(self._cfg, "inject_memory_limit", 5) or 5)
        try:
            value = default if limit is None else int(limit)
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, _RECALL_FOR_PROMPT_MAX_LIMIT))

    def _recall_scope(self, session_type: str, umo: str) -> str:
        if getattr(self._cfg, "memory_scope", "user") != "session":
            return "user"
        if str(session_type or "").strip().lower() == "group":
            _, session_id = self._parse_umo(umo)
            return f"group:{session_id}" if session_id else "private"
        return "private"

    @staticmethod
    def _parse_umo(umo: str) -> tuple[str, str]:
        """解析 ``platform:message_type:session_id``，返回 (adapter, session_id)。"""
        parts = str(umo or "").split(":")
        if len(parts) >= 3:
            return parts[0], parts[-1]
        if len(parts) == 1 and parts[0]:
            return "", parts[0]
        return "", ""

    def _resolve_canonical_from_umo(self, umo: str) -> str:
        """从 umo 解析 canonical_user_id，优先 conversation_cache，再 identity_bindings。"""
        umo = str(umo or "").strip()
        if not umo:
            return ""
        adapter, session_id = self._parse_umo(umo)

        try:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT canonical_user_id FROM conversation_cache"
                    " WHERE unified_msg_origin=? AND canonical_user_id != ''"
                    " ORDER BY id DESC LIMIT 1",
                    (umo,),
                ).fetchone()
            if row and row["canonical_user_id"]:
                return str(row["canonical_user_id"])
        except Exception as e:
            logger.debug("[tmemory] recall_for_prompt cache resolve failed: %s", e)

        if session_id:
            try:
                with self._db() as conn:
                    row = conn.execute(
                        "SELECT canonical_user_id FROM identity_bindings"
                        " WHERE adapter=? AND adapter_user_id=?",
                        (adapter, session_id),
                    ).fetchone()
                if row and row["canonical_user_id"]:
                    return str(row["canonical_user_id"])
            except Exception as e:
                logger.debug("[tmemory] recall_for_prompt binding resolve failed: %s", e)

        if adapter and session_id:
            return f"{adapter}:{session_id}"
        return ""

    async def _resolve_persona_from_umo(self, umo: str) -> str:
        """解析会话 persona_id：优先 conversation_manager，再 conversation_cache。"""
        try:
            from ..adapters import conversation as _conversation_adapter

            persona = await _conversation_adapter.get_current_persona_id(
                getattr(self, "context", None), umo
            )
            if persona:
                return persona
        except Exception as e:
            logger.debug("[tmemory] recall_for_prompt persona manager failed: %s", e)

        try:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT persona_id FROM conversation_cache"
                    " WHERE unified_msg_origin=? AND persona_id != ''"
                    " ORDER BY id DESC LIMIT 1",
                    (umo,),
                ).fetchone()
            if row and row["persona_id"]:
                return str(row["persona_id"])
        except Exception as e:
            logger.debug("[tmemory] recall_for_prompt persona cache failed: %s", e)
        return ""
