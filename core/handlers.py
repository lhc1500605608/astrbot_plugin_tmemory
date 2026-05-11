from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

from astrbot.api import logger

from . import vector as _vector
from .commands import CommandHandlersMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import LLMResponse, ProviderRequest


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
            persona_id=self._get_current_persona(event),
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
            return "\u68c0\u7d22\u8bb0\u5fc6\u65f6\u51fa\u73b0\u9519\u8bef\u3002"

        if not rows:
            return "\u672a\u627e\u5230\u76f8\u5173\u8bb0\u5fc6\u3002"

        lines = []
        for row in rows:
            mtype = row["memory_type"]
            mem = row["memory"]
            lines.append(f"- ({mtype}) {mem}")
        return "\n".join(lines)
