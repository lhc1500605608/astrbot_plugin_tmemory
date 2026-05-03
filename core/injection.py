"""Layered injection assembly: Working -> Episodic -> Semantic -> Style context blocks.

Hot-path constraint: on_llm_request path must be zero LLM calls, SQLite reads only.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .config import PluginConfig


class InjectionBuilder:
    """Assembles structured prompt blocks for layered memory injection."""

    def __init__(self, cfg: PluginConfig, retrieval_mgr):
        self._cfg = cfg
        self._retrieval = retrieval_mgr

    async def build_layered_injection(
        self,
        canonical_id: str,
        query: str,
        session_key: str,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> str:
        """Build the full layered injection block.

        Returns empty string if all layers are empty.
        """
        blocks: List[str] = []

        # ── Working + Episodic → 当前对话背景 ──
        context_block = await self._build_context_block(
            canonical_id, query, session_key, scope, persona_id
        )
        if context_block:
            blocks.append(context_block)

        # ── Semantic → 用户记忆 ──
        memory_block = await self._build_memory_block(
            canonical_id, query, scope, persona_id, exclude_private
        )
        if memory_block:
            blocks.append(memory_block)

        # ── Style → 用户风格指导 ──
        style_block = await self._build_style_block(
            canonical_id, scope, persona_id, exclude_private
        )
        if style_block:
            blocks.append(style_block)

        block = "\n\n".join(blocks)
        if self._cfg.inject_max_chars > 0 and len(block) > self._cfg.inject_max_chars:
            cutoff = max(self._cfg.inject_max_chars - 3, 1)
            block = block[:cutoff] + "…"
        return block

    async def _build_context_block(
        self,
        canonical_id: str,
        query: str,
        session_key: str,
        scope: str,
        persona_id: str,
    ) -> str:
        """Build [当前对话背景] from working + episodic layers."""
        parts: List[str] = []

        # Working layer: recent conversation turns
        working_turns = self._retrieval.retrieve_working_context(
            canonical_id, session_key, self._cfg.inject_working_turns
        )
        if working_turns:
            lines = ["[当前对话背景]"]
            for turn in working_turns:
                role = str(turn.get("role", "user"))
                content = str(turn.get("content", ""))
                role_label = "用户" if role == "user" else "助手"
                lines.append(f"- {role_label}: {content}")
            parts.append("\n".join(lines))

        # Episodic layer: episode summaries
        episodes = self._retrieval.retrieve_episodes(
            canonical_id,
            query,
            self._cfg.inject_episode_limit,
            self._cfg.inject_episode_max_chars,
            scope=scope,
            persona_id=persona_id,
        )
        if episodes:
            ep_lines: List[str] = []
            if not working_turns:
                ep_lines.append("[当前对话背景]")
            for ep in episodes:
                title = str(ep.get("episode_title", ""))
                summary = str(ep.get("episode_summary", ""))
                ep_lines.append(f"- [情节] {title}: {summary}")
            parts.append("\n".join(ep_lines))

        return "\n".join(parts) if parts else ""

    async def _build_memory_block(
        self,
        canonical_id: str,
        query: str,
        scope: str,
        persona_id: str,
        exclude_private: bool,
    ) -> str:
        """Build [用户记忆] from semantic memory layer."""
        rows = await self._retrieval.retrieve_memories(
            canonical_id,
            query,
            self._cfg.inject_memory_limit,
            query_vec=None,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
            summary_channel="canonical",
        )
        scored, _ = rows
        if not scored:
            return ""

        deduped = self._retrieval.deduplicate_results(
            scored, self._cfg.inject_memory_limit
        )
        if not deduped:
            return ""

        lines = ["[用户记忆]"]
        for row in deduped:
            lines.append(f"- ({row['memory_type']}) {row['memory']}")
        return "\n".join(lines)

    async def _build_style_block(
        self,
        canonical_id: str,
        scope: str,
        persona_id: str,
        exclude_private: bool,
    ) -> str:
        """Build [用户风格指导] from style/persona memory layer."""
        rows = await self._retrieval.retrieve_memories(
            canonical_id,
            "",
            min(self._cfg.inject_memory_limit, 3),
            query_vec=None,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
            summary_channel="persona",
        )
        scored, _ = rows
        if not scored:
            return ""

        lines = ["[用户风格指导]"]
        char_count = 0
        max_chars = self._cfg.inject_style_max_chars
        for row in scored:
            mem = str(row["memory"])
            if max_chars > 0:
                char_count += len(mem)
                if char_count > max_chars:
                    break
            lines.append(f"- {mem}")
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def inject_block_by_position(
        req, block: str, position: str, slot_marker: str
    ) -> None:
        """Inject a text block into a ProviderRequest at the configured position.

        Static method so it can be used by both layered and flat injection paths.
        """
        if position == "slot":
            existing = getattr(req, "system_prompt", "") or ""
            if slot_marker in existing:
                req.system_prompt = existing.replace(slot_marker, block, 1)
            else:
                req.system_prompt = existing + ("\n\n" if existing else "") + block
        elif position == "user_message_before":
            original_prompt = getattr(req, "prompt", "") or ""
            req.prompt = block + "\n\n" + original_prompt if original_prompt else block
        elif position == "user_message_after":
            original_prompt = getattr(req, "prompt", "") or ""
            req.prompt = original_prompt + ("\n\n" if original_prompt else "") + block
        else:  # system_prompt
            existing = getattr(req, "system_prompt", "") or ""
            req.system_prompt = existing + ("\n\n" if existing else "") + block
