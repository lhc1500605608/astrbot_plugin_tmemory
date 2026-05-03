"""Profile-aware injection assembly: facet-grouped user profile blocks.

Hot-path constraint: on_llm_request path must be zero LLM calls, SQLite reads only.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .config import PluginConfig

# Facet → block heading mapping
_FACET_HEADINGS: Dict[str, str] = {
    "fact": "[用户画像·事实]",
    "preference": "[用户画像·偏好]",
    "restriction": "[用户画像·限制]",
    "task_pattern": "[用户画像·任务模式]",
    "style": "[用户画像·风格指导]",
}


class InjectionBuilder:
    """Assembles structured prompt blocks for profile-aware injection."""

    def __init__(self, cfg: PluginConfig, retrieval_mgr):
        self._cfg = cfg
        self._retrieval = retrieval_mgr

    async def build_profile_injection(
        self,
        canonical_id: str,
        query: str,
        session_key: str,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> str:
        """Build the profile-aware injection block.

        Returns empty string if both context and profile blocks are empty.
        Zero LLM calls — reads from SQLite only.
        """
        blocks: List[str] = []

        # ── Working context from recent conversation turns ──
        context_block = self._build_context_block(canonical_id, session_key)
        if context_block:
            blocks.append(context_block)

        # ── Profile items grouped by facet ──
        items = self._retrieval.retrieve_profile_items(
            canonical_id,
            query,
            self._cfg.inject_memory_limit,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
        )
        if items:
            profile_block = self._assemble_profile_blocks(items)
            if profile_block:
                blocks.append(profile_block)

        block = "\n\n".join(blocks)
        if self._cfg.inject_max_chars > 0 and len(block) > self._cfg.inject_max_chars:
            cutoff = max(self._cfg.inject_max_chars - 3, 1)
            block = block[:cutoff] + "\u2026"
        return block

    # Backward-compat alias used by existing callers that haven't been updated yet
    async def build_layered_injection(
        self,
        canonical_id: str,
        query: str,
        session_key: str,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> str:
        """Deprecated alias. Delegates to build_profile_injection."""
        return await self.build_profile_injection(
            canonical_id, query, session_key,
            scope=scope, persona_id=persona_id, exclude_private=exclude_private,
        )

    def _build_context_block(
        self,
        canonical_id: str,
        session_key: str,
    ) -> str:
        """Build [当前对话] block from recent conversation turns."""
        working_turns = self._retrieval.retrieve_working_context(
            canonical_id, session_key, self._cfg.inject_working_turns
        )
        if not working_turns:
            return ""

        lines = ["[当前对话]"]
        for turn in working_turns:
            role = str(turn.get("role", "user"))
            content = str(turn.get("content", ""))
            role_label = "用户" if role == "user" else "助手"
            lines.append(f"- {role_label}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _assemble_profile_blocks(
        items: List[Dict[str, object]],
    ) -> str:
        """Group profile items by facet_type into labeled blocks.

        Facets with no items are omitted. Items already arrive sorted by
        relevance/importance from the retrieval layer.
        """
        groups: Dict[str, List[str]] = {}
        for item in items:
            facet = str(item.get("facet_type", "fact"))
            content = str(item.get("content", ""))
            if not content:
                continue
            groups.setdefault(facet, []).append(content)

        if not groups:
            return ""

        # Order facets by priority: restriction > preference > fact > style > task_pattern
        facet_order = ["restriction", "preference", "fact", "style", "task_pattern"]
        blocks: List[str] = []
        for facet in facet_order:
            contents = groups.get(facet)
            if not contents:
                continue
            heading = _FACET_HEADINGS.get(facet, f"[用户画像·{facet}]")
            lines = [heading]
            for c in contents:
                lines.append(f"- {c}")
            blocks.append("\n".join(lines))

        return "\n".join(blocks)

    @staticmethod
    def inject_block_by_position(
        req, block: str, position: str, slot_marker: str
    ) -> None:
        """Inject a text block into a ProviderRequest at the configured position."""
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
