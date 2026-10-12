"""Profile Extraction Runtime Mixin — integrates profile extraction (conversation → profile_items) into TMemoryPlugin."""

from __future__ import annotations

from ..adapters.llm import generate as generate_llm

import asyncio
import logging
from typing import Dict, List

from .distill_errors import classify_llm_error
from .profile_extractor import ProfileExtractor
from .utils_shared import _earliest_date

logger = logging.getLogger("astrbot")


def _build_transcript(rows: List[Dict]) -> str:
    """Build a role-separated transcript: assistant turns are context only."""
    user_lines: List[str] = []
    assistant_lines: List[str] = []
    for i, r in enumerate(rows):
        entry = f"[{i + 1}] {r.get('content', '')}"
        if str(r.get("role", "user")) == "assistant":
            assistant_lines.append(entry)
        else:
            user_lines.append(entry)

    transcript = "【用户发言（唯一可作为用户画像依据）】\n" + "\n".join(user_lines)
    if assistant_lines:
        transcript += (
            "\n【助手发言（仅作上下文参考，禁止作为用户画像依据）】\n"
            + "\n".join(assistant_lines)
        )
    return transcript


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
        from .data_safety import generation_guard
        from .runtime_reliability import claim, fail, recover, retain, BudgetExceeded
        from .profile_ops import ProfileItemOps
        from .attribution import filter_assistant_attributed
        recover(self, manual=force)
        extractor = ProfileExtractor(self._cfg)
        min_msgs = 1 if force else self._cfg.profile_extraction_min_messages
        users = self._pending_profile_users(self._cfg.profile_extraction_max_users_per_cycle, min_msgs)
        total_items = 0
        for uid in users:
            rows = []
            with generation_guard(self, uid):
                try:
                    rows = self._fetch_pending_profile_rows(uid, 150)
                    if len(rows) < min_msgs:
                        continue
                    ids = [int(r["id"]) for r in rows]
                    if not any(r.get("role") == "user" for r in rows):
                        self._mark_rows_distilled(ids)
                        continue
                    provider, model = await self._resolve_consolidation_model(rows)
                    if not provider:
                        fail(self, rows, "no_provider", waiting=True)
                        continue
                    if not claim(self, rows):
                        continue
                    prompt = extractor.build_extraction_prompt(_build_transcript(rows), _earliest_date(r.get("created_at", "") for r in rows))
                    response = await generate_llm(self, chat_provider_id=provider, prompt=prompt, **({"model": model} if model else {}))
                    completion = self._strip_think_tags(self._normalize_text(getattr(response, "completion_text", "") or ""))
                    parsed = self._parse_json_object(completion)
                    if not parsed or not any(isinstance(parsed.get(k), list) for k in ("profile_items", "event_items")):
                        raise ValueError("invalid extraction response")
                    items = extractor.parse_profile_json(completion, self._normalize_text, extractor.safe_facet_type, self._clamp01)
                    items = filter_assistant_attributed(self._validate_profile_items(items), rows, key="content")
                    scope, persona = rows[0].get("scope", "user"), rows[0].get("persona_id", "")
                    evidence = [int(r["id"]) for r in rows if r.get("role") == "user"]
                    vectors = []
                    written = 0
                    # All object writes, evidence and queue completion commit together.
                    with self._db() as conn:
                        ops = ProfileItemOps(self)
                        for item in items:
                            text = self._sanitize_text(self._normalize_text(item["content"]))
                            kind = item.get("memory_type", "")
                            if kind in ("event", "task"):
                                from .data_safety import review_reason, quarantine
                                reason = review_reason(text)
                                if reason:
                                    quarantine(conn, uid, text, reason, self._now(), scope, persona)
                                    continue
                                alias = self._insert_memory(uid, rows[0].get("source_adapter", ""), rows[0].get("source_user_id", uid), text,
                                    .8, kind, item["importance"], item["confidence"], source_channel="profile_extraction", persona_id=persona,
                                    scope=scope, event_date=item.get("event_date", ""), valid_until=item.get("valid_until", ""))
                                obj = conn.execute("SELECT object_id FROM memory_record_ids WHERE id=? AND kind='e'", (alias,)).fetchone()
                                if obj:
                                    state = item.get("task_status", "pending")
                                    conn.execute("UPDATE event_items SET task_status=? WHERE id=?", (state if state in ("pending", "completed", "cancelled") else "pending", obj[0]))
                                    for sid in evidence:
                                        conn.execute("INSERT OR IGNORE INTO event_item_evidence(event_item_id,conversation_cache_id,canonical_user_id,created_at) VALUES(?,?,?,?)", (obj[0], sid, uid, self._now()))
                                written += bool(alias)
                            else:
                                item_id = ops.upsert_profile_item(uid, extractor.safe_facet_type(item["facet_type"]), item.get("title", ""), text,
                                    item["confidence"], item["importance"], scope, persona)
                                if item_id:
                                    ops.add_evidence(item_id, uid, evidence)
                                    vectors.append((item_id, text))
                                    written += 1
                        self._mark_rows_distilled(ids)
                        retain(self, uid)
                    total_items += written
                    for item_id, text in vectors:
                        if self._vec_available:
                            try:
                                await self._upsert_profile_vector(item_id, text)
                            except Exception:
                                with self._db() as conn:
                                    conn.execute("UPDATE profile_items SET embedding_status='failed' WHERE id=?", (item_id,))
                except asyncio.CancelledError:
                    if rows:
                        try:
                            fail(self, rows, "cancelled")
                        except RuntimeError:
                            pass
                    raise
                except Exception as exc:
                    if rows:
                        try:
                            fail(self, rows, "budget" if isinstance(exc, BudgetExceeded) else type(exc).__name__, waiting=isinstance(exc, BudgetExceeded))
                        except RuntimeError:
                            pass
                    logger.warning("[tmemory] extraction deferred user=%s reason=%s", uid, type(exc).__name__)
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
                WHERE distilled=0 AND episode_id=0 AND processing_status IN ('pending','retry','waiting_provider') AND next_retry_at<=strftime('%s','now')
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
                WHERE canonical_user_id=? AND distilled=0 AND episode_id=0 AND processing_status IN ('pending','retry','waiting_provider') AND next_retry_at<=strftime('%s','now')
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()
            from .data_safety import first_isolated_batch
            return first_isolated_batch([dict(r) for r in rows])
