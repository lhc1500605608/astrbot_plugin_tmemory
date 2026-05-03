"""Memory consolidation pipeline: Stage B (Episodic Summarization) + Stage C (Semantic Extraction).

Integrated into the existing _distill_worker_loop() behind enable_consolidation_pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from .config import PluginConfig

logger = logging.getLogger("astrbot")


# ═══════════════════════════════════════════════════════════════════════════════
# Episode Manager — Stage B: Episodic Summarization
# ═══════════════════════════════════════════════════════════════════════════════

class EpisodeManager:
    """Groups conversations into episodes and summarizes them via LLM."""

    def __init__(self, cfg: PluginConfig):
        self._cfg = cfg

    def build_summarization_prompt(self, transcript: str) -> str:
        lines = [
            "你是对话情节摘要器。从以下用户对话中提取一个连贯的情节摘要。",
            "",
            "输出格式(仅JSON，不要任何解释或markdown):",
            "{",
            '  "episode_title": "简短标题(2-10字)",',
            '  "episode_summary": "2-5句话概括本段对话的情节。300字以内。",',
            '  "topic_tags": ["标签1", "标签2"],',
            '  "key_entities": ["关键实体1", "关键实体2"],',
            '  "status": "ongoing|resolved|background",',
            '  "importance": 0.0到1.0,',
            '  "confidence": 0.0到1.0',
            "}",
            "",
            "规则:",
            "1. 只提炼关于用户的信息(需求、偏好、决定、进展)，忽略AI回复。",
            "2. 如果对话太少或没有实质内容，importance和confidence设低(<0.3)。",
            "3. topic_tags最多5个，每个2-6字。",
            "4. key_entities最多5个。",
            "5. status: ongoing=话题仍在进行, resolved=问题已解决, background=背景信息。",
            "",
            "对话:",
            transcript,
        ]
        return "\n".join(lines)

    def build_stricter_prompt(self, transcript: str) -> str:
        """Retry prompt with stricter formatting instructions after a parse failure."""
        lines = [
            "你是对话情节摘要器。从以下用户对话中提取一个连贯的情节摘要。",
            "",
            "重要:你必须且只能输出一个合法的JSON对象，不要输出任何其他文字。",
            "不要使用markdown代码块(```)。确保JSON中的字符串使用双引号。",
            "",
            "输出格式:",
            '{"episode_title":"标题","episode_summary":"摘要","topic_tags":["标签"],'
            '"key_entities":["实体"],"status":"ongoing","importance":0.5,"confidence":0.5}',
            "",
            "对话:",
            transcript,
        ]
        return "\n".join(lines)

    def parse_episode_json(self, raw_text: str) -> Optional[Dict]:
        """Parse LLM output into episode dict. Returns None on failure."""
        if not raw_text:
            return None
        raw_text = _strip_think_tags(raw_text)
        data = _extract_json_object(raw_text)
        if not isinstance(data, dict):
            return None

        title = str(data.get("episode_title", "")).strip()
        summary = str(data.get("episode_summary", "")).strip()
        if not title or not summary or len(summary) < 10:
            return None

        tags = data.get("topic_tags", [])
        if not isinstance(tags, list):
            tags = []
        entities = data.get("key_entities", [])
        if not isinstance(entities, list):
            entities = []

        status = str(data.get("status", "ongoing")).strip().lower()
        if status not in {"ongoing", "resolved", "background"}:
            status = "ongoing"

        importance = _clamp(float(data.get("importance", 0.5)))
        confidence = _clamp(float(data.get("confidence", 0.5)))

        return {
            "episode_title": title[:100],
            "episode_summary": summary[:600],
            "topic_tags": json.dumps(tags[:5], ensure_ascii=False),
            "key_entities": json.dumps(entities[:5], ensure_ascii=False),
            "status": status,
            "importance": importance,
            "confidence": confidence,
        }

    def extractive_summary(self, rows: List[Dict]) -> Dict:
        """Rule-based fallback: concatenate first 100 chars of each user message."""
        user_lines = []
        for r in rows:
            if str(r.get("role", "")) == "user":
                content = str(r.get("content", ""))[:100]
                if content.strip():
                    user_lines.append(content)
        if not user_lines:
            return {
                "episode_title": "未命名对话",
                "episode_summary": "(无内容)",
                "topic_tags": "[]",
                "key_entities": "[]",
                "status": "ongoing",
                "importance": 0.3,
                "confidence": 0.2,
            }
        combined = "; ".join(user_lines[:20])
        return {
            "episode_title": user_lines[0][:30] if user_lines else "未命名对话",
            "episode_summary": combined[:600],
            "topic_tags": "[]",
            "key_entities": "[]",
            "status": "ongoing",
            "importance": 0.3,
            "confidence": 0.2,
        }

    def group_conversations_into_sessions(
        self, rows: List[Dict]
    ) -> List[List[Dict]]:
        """Group conversation rows into sessions by time proximity.

        Rows are assumed sorted by created_at ASC. A new session starts when
        the gap between consecutive messages exceeds episode_session_gap_minutes.
        """
        if not rows:
            return []

        gap_sec = self._cfg.episode_session_gap_minutes * 60
        sessions: List[List[Dict]] = []
        current: List[Dict] = []

        for row in rows:
            if not current:
                current.append(row)
                continue

            prev_ts = _parse_iso_timestamp(str(current[-1].get("created_at", "")))
            this_ts = _parse_iso_timestamp(str(row.get("created_at", "")))
            if prev_ts > 0 and this_ts > 0 and (this_ts - prev_ts) > gap_sec:
                sessions.append(current)
                current = [row]
            else:
                current.append(row)

        if current:
            sessions.append(current)
        return sessions


# ═══════════════════════════════════════════════════════════════════════════════
# Semantic Extractor — Stage C: Extract memories from episodes
# ═══════════════════════════════════════════════════════════════════════════════

class SemanticExtractor:
    """Extracts atomic, long-lived memories from episode summaries."""

    def __init__(self, cfg: PluginConfig):
        self._cfg = cfg

    def build_extraction_prompt(self, episode_summary: str, source_snippets: str) -> str:
        """Build prompt for distilling memories from an episode summary."""
        memory_types = "preference|fact|task|restriction|style"
        lines = [
            "你是高质量记忆蒸馏器。从以下情节摘要和相关对话中提炼长期有价值的用户信息。",
            "仅输出 JSON，不要输出任何解释文字或 markdown 标记。",
            "",
            "输出格式(必须严格遵守):",
            "{",
            '  "memories": [',
            "    {",
            '      "memory": "一句话，主语必须是用户，10-80字，简洁精确",',
            f'      "memory_type": "{memory_types}",',
            '      "importance": 0.0到1.0,',
            '      "confidence": 0.0到1.0,',
            '      "score": 0.0到1.0',
            "    }",
            "  ]",
            "}",
            "",
            "质量规则(严格执行):",
            "1. 只提炼关于用户本人的稳定信息:偏好、身份、习惯、长期目标、约束条件、沟通风格。",
            "2. 严格排除:一次性寒暄、单次提问、AI说的话、情绪化表达、安全敏感信息。",
            "3. memory 字段必须是一个完整的陈述句，主语是用户。",
            "4. 如果没有任何值得长期记住的信息，返回空数组。",
            "5. confidence 低于 0.6 的不要输出。importance 低于 0.4 的不要输出。",
            "6. 最多返回 5 条，宁缺毋滥。",
            "7. 优先从摘要中提取跨会话的稳定模式，而非单次对话的细节。",
            "",
            "情节摘要:",
            episode_summary,
            "",
            "关键对话片段:",
            source_snippets,
        ]
        return "\n".join(lines)

    def parse_memories_json(
        self, raw_text: str, normalize_text_func, safe_memory_type_func, clamp01_func
    ) -> List[Dict]:
        """Parse LLM output into memory items. Reuses the existing parsing pattern."""
        if not raw_text:
            return []
        raw_text = _strip_think_tags(raw_text)
        data = _extract_json_object(raw_text)
        if not isinstance(data, dict):
            return []

        items = data.get("memories")
        if not isinstance(items, list):
            return []

        result = []
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            mem = normalize_text_func(str(item.get("memory", "")))
            if not mem:
                continue
            result.append({
                "memory": mem,
                "memory_type": safe_memory_type_func(item.get("memory_type", "fact")),
                "importance": clamp01_func(item.get("importance", 0.6)),
                "confidence": clamp01_func(item.get("confidence", 0.7)),
                "score": clamp01_func(item.get("score", 0.7)),
            })
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# Consolidation Runtime Mixin — integrates into TMemoryPlugin
# ═══════════════════════════════════════════════════════════════════════════════

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
                logger.warning(
                    "[tmemory] episode summarization failed for user %s: %s",
                    canonical_id, e,
                )

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
            logger.warning("[tmemory] episode summarization LLM call failed: %s", e)
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
                    logger.warning(
                        "[tmemory] semantic extraction LLM failed for episode %s: %s",
                        ep_id, e,
                    )
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
                logger.warning(
                    "[tmemory] semantic extraction failed for episode %s: %s",
                    ep_id, e,
                )
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


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

_THINK_RE = re.compile(
    r"<th(?:ink(?:ing)?|ought)>.*?</th(?:ink(?:ing)?|ought)>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_think_tags(text: str) -> str:
    stripped = _THINK_RE.sub("", text).strip()
    return stripped if stripped else text


def _extract_json_object(text: str) -> Optional[Dict]:
    """Extract a JSON object from text that may have surrounding content."""
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _parse_iso_timestamp(ts: str) -> float:
    """Parse an ISO-ish timestamp to Unix epoch. Returns 0 on failure."""
    if not ts:
        return 0.0
    try:
        if "T" in ts:
            import datetime
            ts_clean = ts.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(ts_clean)
            return dt.timestamp()
        return 0.0
    except (ValueError, TypeError):
        return 0.0


def _derive_session_key(rows: List[Dict]) -> str:
    """Derive a stable session key from the time range of a session."""
    first = str(rows[0].get("created_at", "")) if rows else ""
    last = str(rows[-1].get("created_at", "")) if rows else ""
    import hashlib
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
