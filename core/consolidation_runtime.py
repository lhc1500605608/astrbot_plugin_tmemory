"""Consolidation Runtime Mixin — integrates Stage B (episode summarization) and Stage C (semantic extraction) into TMemoryPlugin."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

from .config import PluginConfig
from .distill_errors import classify_llm_error
from .episode_manager import EpisodeManager
from .semantic_extractor import SemanticExtractor

logger = logging.getLogger("astrbot")


def _derive_session_key(rows: List[Dict]) -> str:
    """Derive a stable session key from the time range of a session."""
    first = str(rows[0].get("created_at", "")) if rows else ""
    last = str(rows[-1].get("created_at", "")) if rows else ""
    return hashlib.sha256(f"{first}:{last}:{len(rows)}".encode()).hexdigest()[:16]


def _build_transcript(rows: List[Dict]) -> str:
    """Build a compact transcript from conversation rows."""
    lines = []
    for i, r in enumerate(rows):
        role = str(r.get("role", "user"))
        content = str(r.get("content", ""))
        prefix = f"[{i + 1}][{role}]"
        lines.append(f"{prefix} {content}")
    return "\n".join(lines)


class ConsolidationRuntimeMixin:
    """Mixin that adds consolidation pipeline methods to TMemoryPlugin."""

    async def _run_consolidation_cycle(
        self, force: bool = False, trigger: str = "auto"
    ) -> Tuple[int, int]:
        """Run Stage B (episode summarization) then Stage C (semantic extraction).

        Returns (episodes_created, memories_extracted).
        """
        if not self._cfg.enable_consolidation_pipeline:
            return 0, 0
        if self._cfg.distill_pause or self._cfg.memory_mode == "active_only":
            return 0, 0

        episodes_created = 0
        memories_extracted = 0

        # Stage B: Episodic Summarization
        if self._cfg.enable_episodic_summarization:
            try:
                episodes_created = await asyncio.wait_for(
                    self._run_episode_summarization(force),
                    timeout=self._cfg.stage_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning("[tmemory] Stage B episode summarization timed out")
            except Exception as e:
                logger.warning("[tmemory] Stage B episode summarization error: %s", e)

        # Stage C: Semantic Extraction
        if self._cfg.enable_episode_semantic_distill:
            try:
                memories_extracted = await asyncio.wait_for(
                    self._run_semantic_extraction(force),
                    timeout=self._cfg.stage_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning("[tmemory] Stage C semantic extraction timed out")
            except Exception as e:
                logger.warning("[tmemory] Stage C semantic extraction error: %s", e)

        return episodes_created, memories_extracted

    # ── Stage B implementation ──────────────────────────────────────────────

    async def _run_episode_summarization(self, force: bool = False) -> int:
        """Group pending conversations into episodes and summarize each."""
        ep_mgr = EpisodeManager(self._cfg)
        min_msgs = 1 if force else self._cfg.episode_summary_min_messages
        max_users = self._cfg.distill_max_users_per_cycle

        pending_users = self._pending_consolidation_users(limit=max_users, min_batch=min_msgs)
        total_episodes = 0

        for canonical_id in pending_users:
            try:
                rows = self._fetch_pending_consolidation_rows(canonical_id, limit=200)
                if len(rows) < min_msgs:
                    continue

                # Group by (scope, persona_id), then subdivide into sessions
                groups: Dict[Tuple[str, str], List[Dict]] = {}
                for r in rows:
                    key = (str(r.get("scope", "user")), str(r.get("persona_id", "")))
                    groups.setdefault(key, []).append(r)

                for (_scope, _persona_id), group_rows in groups.items():
                    sessions = ep_mgr.group_conversations_into_sessions(group_rows)
                    for session_rows in sessions:
                        if len(session_rows) < min_msgs:
                            continue

                        transcript = _build_transcript(session_rows)
                        episode = await self._summarize_session(
                            ep_mgr, transcript, session_rows
                        )
                        if episode is None:
                            continue

                        ep_id = self._insert_episode(
                            canonical_id=canonical_id,
                            scope=str(session_rows[0].get("scope", "user")),
                            persona_id=str(session_rows[0].get("persona_id", "")),
                            session_key=_derive_session_key(session_rows),
                            episode=episode,
                            source_rows=session_rows,
                        )
                        if ep_id:
                            total_episodes += 1
            except Exception as e:
                err = classify_llm_error(
                    e,
                    pipeline="consolidation",
                    user_id=canonical_id,
                    context_message="episode summarization failed for user",
                )
                err.log()

        return total_episodes

    async def _summarize_session(
        self, ep_mgr: EpisodeManager, transcript: str, rows: List[Dict]
    ) -> Optional[Dict]:
        """Call LLM to summarize a session into an episode. Returns parsed dict or None."""
        prompt = ep_mgr.build_summarization_prompt(transcript)

        provider_id, model_id = await self._resolve_consolidation_model(rows)
        if not provider_id:
            return ep_mgr.extractive_summary(rows)

        try:
            llm_kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
            if model_id:
                llm_kwargs["model_id"] = model_id

            llm_resp = await self.context.llm_generate(**llm_kwargs)
            completion = self._normalize_text(
                getattr(llm_resp, "completion_text", "") or ""
            )
            episode = ep_mgr.parse_episode_json(completion)
            if episode is not None:
                return episode

            # Retry once with stricter prompt
            logger.debug("[tmemory] episode JSON parse failed, retrying")
            stricter_prompt = ep_mgr.build_stricter_prompt(transcript)
            llm_kwargs["prompt"] = stricter_prompt
            llm_resp2 = await self.context.llm_generate(**llm_kwargs)
            completion2 = self._normalize_text(
                getattr(llm_resp2, "completion_text", "") or ""
            )
            episode2 = ep_mgr.parse_episode_json(completion2)
            if episode2 is not None:
                return episode2

            logger.warning("[tmemory] episode JSON parse failed after retry, using extractive fallback")
            return ep_mgr.extractive_summary(rows)
        except Exception as e:
            err = classify_llm_error(
                e,
                pipeline="consolidation",
                user_id=str(rows[0].get("canonical_user_id", "")),
                context_message="episode summarization LLM call failed, using extractive fallback",
            )
            err.log()
            return ep_mgr.extractive_summary(rows)

    def _insert_episode(
        self,
        canonical_id: str,
        scope: str,
        persona_id: str,
        session_key: str,
        episode: Dict,
        source_rows: List[Dict],
    ) -> Optional[int]:
        """Insert a memory_episodes row and episode_sources mappings."""
        now = self._now()
        source_ids = [int(r["id"]) for r in source_rows]
        first_at = str(source_rows[0].get("created_at", now))
        last_at = str(source_rows[-1].get("created_at", now))

        with self._db() as conn:
            cur = conn.execute(
                """
                INSERT INTO memory_episodes(
                    canonical_user_id, scope, persona_id, session_key,
                    episode_title, episode_summary, topic_tags, key_entities,
                    status, importance, confidence,
                    consolidation_status, source_count,
                    first_source_at, last_source_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_semantic', ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id, scope, persona_id, session_key,
                    str(episode.get("episode_title", "")),
                    str(episode.get("episode_summary", "")),
                    str(episode.get("topic_tags", "[]")),
                    str(episode.get("key_entities", "[]")),
                    str(episode.get("status", "ongoing")),
                    float(episode.get("importance", 0.5)),
                    float(episode.get("confidence", 0.5)),
                    len(source_ids),
                    first_at, last_at, now, now,
                ),
            )
            ep_id = int(cur.lastrowid or 0)
            if not ep_id:
                return None

            for sid in source_ids:
                conn.execute(
                    "INSERT INTO episode_sources(episode_id, conversation_cache_id, canonical_user_id, created_at) VALUES(?, ?, ?, ?)",
                    (ep_id, sid, canonical_id, now),
                )
            placeholders = ",".join(["?"] * len(source_ids))
            conn.execute(
                f"UPDATE conversation_cache SET episode_id=? WHERE id IN ({placeholders})",
                [ep_id] + source_ids,
            )
            return ep_id

    # ── Stage C implementation ──────────────────────────────────────────────

    async def _run_semantic_extraction(self, force: bool = False) -> int:
        """Extract memories from episodes with consolidation_status='pending_semantic'."""
        extractor = SemanticExtractor(self._cfg)
        episodes = self._fetch_pending_episodes(limit=20)
        total_memories = 0

        for ep in episodes:
            ep_id = int(ep["id"])
            try:
                sources = self._fetch_episode_sources(ep_id, limit=5)
                snippets = _build_transcript(sources) if sources else ""

                prompt = extractor.build_extraction_prompt(
                    str(ep["episode_summary"]), snippets
                )

                provider_id, model_id = await self._resolve_consolidation_model(sources or [ep])
                if not provider_id:
                    self._mark_episode_semantic_done(ep_id, "no_provider")
                    continue

                try:
                    llm_kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
                    if model_id:
                        llm_kwargs["model_id"] = model_id

                    llm_resp = await self.context.llm_generate(**llm_kwargs)
                    completion = self._normalize_text(
                        getattr(llm_resp, "completion_text", "") or ""
                    )
                    items = extractor.parse_memories_json(
                        completion,
                        self._normalize_text,
                        self._safe_memory_type,
                        self._clamp01,
                    )
                except Exception as e:
                    err = classify_llm_error(
                        e,
                        pipeline="consolidation",
                        user_id=str(ep.get("canonical_user_id", "")),
                        context_message=f"semantic extraction LLM failed for episode {ep_id}",
                    )
                    err.log()
                    self._mark_episode_semantic_failed(ep_id)
                    continue

                if not items:
                    self._mark_episode_semantic_done(ep_id, "empty_result")
                    continue

                valid_items = self._validate_distill_output(items)
                if not valid_items:
                    self._mark_episode_semantic_done(ep_id, "all_invalid")
                    continue

                canonical_id = str(ep["canonical_user_id"])
                scope = str(ep.get("scope", "user"))
                persona_id = str(ep.get("persona_id", ""))
                evidence = json.dumps(
                    {"episode_id": ep_id, "episode_title": str(ep["episode_title"])},
                    ensure_ascii=False,
                )

                for item in valid_items:
                    mem_text = self._sanitize_text(
                        self._normalize_text(str(item.get("memory", "")))
                    )
                    if not mem_text:
                        continue

                    from .memory_ops import MemoryOps
                    ops = MemoryOps(self)
                    new_id = ops.insert_memory(
                        canonical_id=canonical_id,
                        adapter=str(sources[0]["source_adapter"]) if sources else "consolidation",
                        adapter_user=str(sources[0]["source_user_id"]) if sources else canonical_id,
                        memory=mem_text,
                        score=self._clamp01(item.get("score", 0.7)),
                        memory_type=str(item.get("memory_type", "fact")),
                        importance=self._clamp01(item.get("importance", 0.6)),
                        confidence=self._clamp01(item.get("confidence", 0.7)),
                        source_channel="episode_distill",
                        scope=scope,
                        persona_id=persona_id,
                    )
                    if new_id:
                        with self._db() as conn:
                            conn.execute(
                                "UPDATE memories SET episode_id=?, derived_from='episode', evidence_json=? WHERE id=?",
                                (ep_id, evidence, new_id),
                            )
                        if self._vec_available:
                            await self._upsert_vector(new_id, mem_text)
                        total_memories += 1

                self._mark_episode_semantic_done(ep_id, "done")
            except Exception as e:
                err = classify_llm_error(
                    e,
                    pipeline="consolidation",
                    user_id=str(ep.get("canonical_user_id", "")),
                    context_message=f"semantic extraction failed for episode {ep_id}",
                )
                err.log()
                self._mark_episode_semantic_failed(ep_id)

        return total_memories

    # ── Helper queries ──────────────────────────────────────────────────────

    def _pending_consolidation_users(self, limit: int, min_batch: int) -> List[str]:
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
                (min_batch, limit),
            ).fetchall()
        return [str(r["canonical_user_id"]) for r in rows]

    def _fetch_pending_consolidation_rows(
        self, canonical_id: str, limit: int
    ) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, canonical_user_id, role, content, source_adapter, source_user_id,
                       unified_msg_origin, scope, persona_id, created_at
                FROM conversation_cache
                WHERE canonical_user_id=? AND distilled=0 AND episode_id=0
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def _fetch_pending_episodes(self, limit: int) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_episodes
                WHERE consolidation_status='pending_semantic'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def _fetch_episode_sources(self, episode_id: int, limit: int) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT cc.* FROM conversation_cache cc
                JOIN episode_sources es ON es.conversation_cache_id = cc.id
                WHERE es.episode_id = ?
                ORDER BY cc.created_at ASC
                LIMIT ?
                """,
                (episode_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def _mark_episode_semantic_done(self, episode_id: int, reason: str) -> None:
        now = self._now()
        with self._db() as conn:
            conn.execute(
                "UPDATE memory_episodes SET consolidation_status='semantic_done', updated_at=? WHERE id=?",
                (now, episode_id),
            )
        logger.debug("[tmemory] episode %s semantic_done: %s", episode_id, reason)

    def _mark_episode_semantic_failed(self, episode_id: int) -> None:
        now = self._now()
        with self._db() as conn:
            conn.execute(
                "UPDATE memory_episodes SET consolidation_status='semantic_failed', updated_at=? WHERE id=?",
                (now, episode_id),
            )

    # ── Model resolution ────────────────────────────────────────────────────

    async def _resolve_consolidation_model(
        self, rows: List[Dict]
    ) -> Tuple[str, str]:
        """Resolve provider_id and model_id for consolidation LLM calls."""
        if self._cfg.use_independent_consolidation_model:
            pid = self._cfg.consolidation_provider_id
            mid = self._cfg.consolidation_model_id
            if pid:
                return pid, mid

        return (
            await self._distill_mgr.resolve_distill_provider_id(rows, self.context),
            await self._distill_mgr.resolve_distill_model_id(rows),
        )
