"""Profile Extraction Runtime Mixin — integrates profile extraction (conversation → profile_items) into TMemoryPlugin."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List

from .distill_errors import classify_llm_error
from .profile_extractor import ProfileExtractor

logger = logging.getLogger("astrbot")


def _build_transcript(rows: List[Dict]) -> str:
    """Build a compact transcript from conversation rows."""
    lines = []
    for i, r in enumerate(rows):
        role = str(r.get("role", "user"))
        content = str(r.get("content", ""))
        prefix = f"[{i + 1}][{role}]"
        lines.append(f"{prefix} {content}")
    return "\n".join(lines)


class ProfileExtractionRuntimeMixin:
    """Mixin that adds profile extraction methods to TMemoryPlugin."""

    async def _run_profile_extraction_cycle(
        self, force: bool = False, trigger: str = "auto"
    ) -> int:
        """Run one cycle of profile extraction: conversation_cache → profile_items.

        Returns number of profile items created/updated.
        """
        if not self._cfg.profile_extraction_enabled:
            return 0
        if self._cfg.distill_pause or self._cfg.memory_mode == "active_only":
            return 0

        try:
            return await asyncio.wait_for(
                self._extract_profiles(force),
                timeout=self._cfg.profile_extraction_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning("[tmemory] profile extraction timed out")
            return 0
        except Exception as e:
            err = classify_llm_error(
                e, pipeline="profile_extraction",
                context_message="profile extraction cycle failed",
            )
            err.log()
            return 0

    async def _extract_profiles(self, force: bool = False) -> int:
        extractor = ProfileExtractor(self._cfg)
        min_msgs = 1 if force else self._cfg.profile_extraction_min_messages
        max_users = self._cfg.profile_extraction_max_users_per_cycle

        pending_users = self._pending_profile_users(limit=max_users, min_batch=min_msgs)
        total_items = 0

        for canonical_id in pending_users:
            try:
                rows = self._fetch_pending_profile_rows(canonical_id, limit=150)
                if len(rows) < min_msgs:
                    continue

                if not any(str(r.get("role", "")) == "user" for r in rows):
                    self._mark_rows_distilled([int(r["id"]) for r in rows])
                    continue

                transcript = _build_transcript(rows)
                prompt = extractor.build_extraction_prompt(transcript)

                provider_id, model_id = await self._resolve_consolidation_model(rows)
                if not provider_id:
                    self._mark_rows_distilled([int(r["id"]) for r in rows])
                    continue

                row_scope = str(rows[0].get("scope", "user"))
                row_persona = str(rows[0].get("persona_id", ""))

                try:
                    llm_kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
                    if model_id:
                        llm_kwargs["model_id"] = model_id

                    llm_resp = await self.context.llm_generate(**llm_kwargs)
                    completion = self._normalize_text(
                        getattr(llm_resp, "completion_text", "") or ""
                    )
                    items = extractor.parse_profile_json(
                        completion,
                        self._normalize_text,
                        ProfileExtractor.safe_facet_type,
                        self._clamp01,
                    )
                except Exception as e:
                    err = classify_llm_error(
                        e,
                        pipeline="profile_extraction",
                        user_id=canonical_id,
                        context_message="profile extraction LLM call failed",
                    )
                    err.log()
                    self._mark_rows_distilled([int(r["id"]) for r in rows])
                    continue

                if not items:
                    self._mark_rows_distilled([int(r["id"]) for r in rows])
                    continue

                valid_items = self._validate_profile_items(items)
                if not valid_items:
                    self._mark_rows_distilled([int(r["id"]) for r in rows])
                    continue

                source_ids = [int(r["id"]) for r in rows]
                adapter = str(rows[0].get("source_adapter", "profile_extraction"))
                adapter_user = str(rows[0].get("source_user_id", canonical_id))

                for item in valid_items:
                    mem_text = self._sanitize_text(
                        self._normalize_text(str(item.get("content", "")))
                    )
                    if not mem_text:
                        continue
                    facet = ProfileExtractor.safe_facet_type(
                        str(item.get("facet_type", "fact"))
                    )

                    from .memory_ops import ProfileItemOps
                    ops = ProfileItemOps(self)
                    item_id = ops.upsert_profile_item(
                        canonical_id=canonical_id,
                        facet_type=facet,
                        title=str(item.get("title", ""))[:100],
                        content=mem_text,
                        confidence=self._clamp01(item.get("confidence", 0.7)),
                        importance=self._clamp01(item.get("importance", 0.6)),
                        source_scope=row_scope,
                        persona_id=row_persona,
                    )
                    if item_id:
                        ops.add_evidence(
                            profile_item_id=item_id,
                            canonical_user_id=canonical_id,
                            source_ids=source_ids,
                            source_role="user",
                            evidence_kind="conversation",
                            confidence_delta=0.1,
                        )
                        if self._vec_available:
                            await self._upsert_profile_vector(item_id, mem_text)
                        total_items += 1

                self._mark_rows_distilled(source_ids)
                self._optimize_context(canonical_id)

            except Exception as e:
                err = classify_llm_error(
                    e,
                    pipeline="profile_extraction",
                    user_id=canonical_id,
                    context_message="profile extraction failed for user",
                )
                err.log()

        return total_items

    @staticmethod
    def _validate_profile_items(items: List[Dict]) -> List[Dict]:
        """Validate profile items: minimum content length, max length, junk filter."""
        valid = []
        for item in items:
            content = str(item.get("content", "")).strip()
            if not content or len(content) < 6:
                continue
            if len(content) > 300:
                content = content[:300]
                item["content"] = content
            if content in ("空白输入",):
                continue
            valid.append(item)
        return valid

    # ── Query helpers ──────────────────────────────────────────────────────

    def _pending_profile_users(self, limit: int, min_batch: int) -> List[str]:
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

    def _fetch_pending_profile_rows(
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
