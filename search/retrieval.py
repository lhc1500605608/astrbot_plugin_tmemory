import time
import asyncio
import datetime
from typing import List, Dict, Optional, Tuple
import sqlite3

from ..core.config import PluginConfig
from ..hybrid_search import HybridMemorySystem

class RetrievalManager:
    def __init__(self, cfg: PluginConfig, db_manager):
        self._cfg = cfg
        self._db_mgr = db_manager

    def parse_ts(self, ts_str: str) -> int:
        if not ts_str:
            return 0
        try:
            return int(time.mktime(time.strptime(str(ts_str), "%Y-%m-%d %H:%M:%S")))
        except Exception:
            return 0

    async def retrieve_memories(
        self,
        canonical_id: str,
        query: str,
        limit: int,
        query_vec: Optional[List[float]],
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
        summary_channel: str = "canonical",
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """执行底层数据库检索和重排序，返回 (未精排的候选结果列表, 需要更新reinforce的ID)

        实际 Rerank 将留给主类以便调用 LLM。
        summary_channel: 'canonical' for fact-oriented search, 'persona' for style injection,
                         empty string for all channels.
        """
        now_ts = int(time.time())

        def _sync_db_ops():
            scope_cond = ""
            scope_params: list = []
            if self._cfg.memory_scope == "session":
                scope_cond = "AND (scope=? OR scope='user')"
                scope_params = [scope]
            persona_cond = "AND (persona_id=? OR persona_id='')"
            persona_params = [persona_id]
            private_cond = "AND scope != 'private'" if exclude_private else ""
            channel_cond = "AND summary_channel = ?" if summary_channel else ""
            channel_params = [summary_channel] if summary_channel else []

            with self._db_mgr.db() as conn:
                hybrid_system = HybridMemorySystem(conn, self._cfg.embed_dim)
                
                fused_results = hybrid_system.hybrid_search(
                    query=query, 
                    query_vector=query_vec, 
                    canonical_user_id=canonical_id, 
                    top_k=80
                )
                
                if not query or not fused_results:
                    rows = conn.execute(
                        f"""
                        SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, attention_score,
                               last_seen_at, scope, persona_id
                        FROM memories
                        WHERE canonical_user_id=? AND is_active=1 {scope_cond} {persona_cond} {private_cond} {channel_cond}
                        ORDER BY score DESC, updated_at DESC
                        LIMIT ?
                        """,
                        (canonical_id, *scope_params, *persona_params, *channel_params, limit * 3),
                    ).fetchall()
                    return [dict(r) | {"_retrieval_score": r["score"]} for r in rows], []

                rrf_scores = {item["id"]: item["rrf_score"] for item in fused_results}
                hit_ids = [str(r["id"]) for r in fused_results]
                placeholders = ",".join("?" * len(hit_ids))

                query_sql = f"""
                    SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, attention_score,
                           last_seen_at, scope, persona_id
                    FROM memories
                    WHERE id IN ({placeholders}) AND is_active=1
                    AND canonical_user_id=?
                    {scope_cond} {persona_cond} {private_cond} {channel_cond}
                """
                rows = conn.execute(query_sql, [*hit_ids, canonical_id, *scope_params, *persona_params, *channel_params]).fetchall()
                candidates = {int(r["id"]): dict(r) for r in rows}
                
                return candidates, rrf_scores
                
        fallback_rows, rrf_scores_or_fused = await asyncio.to_thread(_sync_db_ops)

        if isinstance(fallback_rows, list):
            rows = fallback_rows
            scored = []
            for row in rows:
                scored.append(dict(row))
        else:
            candidates = fallback_rows
            rrf_scores = rrf_scores_or_fused
            
            scored = []
            for row_id, row in candidates.items():
                recency_bonus = 0.0
                last_seen = str(row["last_seen_at"])
                try:
                    last_ts = self.parse_ts(last_seen)
                    age_hours = max(1.0, (now_ts - last_ts) / 3600)
                    recency_bonus = min(0.15, 0.15 / age_hours)
                except Exception:
                    pass

                search_relevance = rrf_scores.get(row_id, 0.0)
                attention_score = float(row.get("attention_score", 0.5) or 0.5)

                if query:
                    final_score = (
                        0.20 * float(row["score"])
                        + 0.10 * float(row["importance"])
                        + 0.15 * float(row["confidence"])
                        + 0.40 * search_relevance
                        + 0.10 * attention_score
                        + recency_bonus
                    )
                else:
                    final_score = (
                        0.35 * float(row["score"])
                        + 0.20 * float(row["importance"])
                        + 0.20 * float(row["confidence"])
                        + 0.15 * attention_score
                        + recency_bonus
                    )

                scored.append(
                    {
                        "id": row_id,
                        "memory_type": str(row["memory_type"]),
                        "memory": str(row["memory"]),
                        "final_score": float(final_score),
                    }
                )

        scored.sort(key=lambda x: float(x.get("final_score", x.get("_retrieval_score", 0.0))), reverse=True)
        return scored, []

    def retrieve_events(
        self,
        canonical_id: str,
        today,
        window_days: int = 0,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> list[dict[str, object]]:
        """Return active ``event`` memories dated within ±window_days of ``today``.

        Read-only. Rows whose ``valid_until`` is set and earlier than today are
        dropped; results are ordered by ``event_date`` ascending.
        """
        try:
            if isinstance(today, datetime.datetime):
                today_date = today.date()
            elif isinstance(today, datetime.date):
                today_date = today
            else:
                today_date = datetime.date.fromisoformat(str(today)[:10])
        except Exception:  # noqa: BLE001 - malformed input falls back to today
            today_date = datetime.datetime.now(tz=datetime.timezone.utc).astimezone().date()
        try:
            window = max(0, int(window_days))
        except (TypeError, ValueError):
            window = 0

        today_iso = today_date.isoformat()
        lower = (today_date - datetime.timedelta(days=window)).isoformat()
        upper = (today_date + datetime.timedelta(days=window)).isoformat()

        scope_cond = ""
        scope_params: list = []
        if self._cfg.memory_scope == "session":
            scope_cond = "AND (scope=? OR scope='user')"
            scope_params = [scope]
        persona_cond = "AND (persona_id=? OR persona_id='')"
        private_cond = "AND scope != 'private'" if exclude_private else ""

        with self._db_mgr.db() as conn:
            rows = conn.execute(
                f"""
                SELECT id, memory_type, memory, event_date, valid_until,
                       score, importance, confidence, scope, persona_id
                FROM memories
                WHERE canonical_user_id=? AND is_active=1 AND memory_type='event'
                  AND event_date != '' AND event_date >= ? AND event_date <= ?
                  AND (valid_until = '' OR valid_until >= ?)
                  {scope_cond} {persona_cond} {private_cond}
                ORDER BY event_date ASC, id ASC
                """,
                (canonical_id, lower, upper, today_iso, *scope_params, persona_id),
            ).fetchall()

        return [
            {
                "id": int(r["id"]),
                "memory_type": str(r["memory_type"]),
                "memory": str(r["memory"]),
                "event_date": str(r["event_date"]),
                "valid_until": str(r["valid_until"]),
                "score": float(r["score"]),
                "importance": float(r["importance"]),
                "confidence": float(r["confidence"]),
                "scope": str(r["scope"]),
                "persona_id": str(r["persona_id"]),
            }
            for r in rows
        ]

    def retrieve_working_context(
        self,
        canonical_id: str,
        session_key: str,
        limit: int,
    ) -> List[Dict[str, object]]:
        """Retrieve recent conversation_cache turns for working memory injection.

        Returns list of {role, content} dicts ordered by id DESC (most recent first),
        then reversed so they appear in chronological order for prompt assembly.
        """
        if not session_key or limit <= 0:
            return []

        with self._db_mgr.db() as conn:
            rows = conn.execute(
                """
                SELECT role, content FROM conversation_cache
                WHERE canonical_user_id = ? AND session_key = ?
                  AND archived_at = ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (canonical_id, session_key, limit),
            ).fetchall()
        result = [{"role": r["role"], "content": r["content"]} for r in rows]
        result.reverse()
        return result

    # Per-facet quota weights: higher weight = more slots allocated
    _FACET_QUOTA_WEIGHT = {
        "restriction": 2.0,
        "preference": 1.5,
        "fact": 1.0,
        "style": 0.5,
        "task_pattern": 0.5,
    }

    def retrieve_profile_items(
        self,
        canonical_id: str,
        query: str,
        limit: int,
        query_vec: Optional[List[float]] = None,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> List[Dict[str, object]]:
        """Retrieve active profile items for injection with hybrid search + per-facet quota.

        When query is non-empty, uses FTS (+ optional vector + RRF fusion) against
        profile_items_fts / profile_item_vectors.  Otherwise falls back to a
        facet-priority sort.

        Per-facet quota ensures diverse coverage: restriction and preference get
        proportionally more slots than style and task_pattern.
        """
        now_ts = int(time.time())
        scope_cond = "AND (pi.source_scope=? OR pi.source_scope='user')"
        persona_cond = "AND (pi.persona_id=? OR pi.persona_id='')"
        private_cond = "AND pi.source_scope != 'private'" if exclude_private else ""

        def _sync_retrieve():
            with self._db_mgr.db() as conn:
                hybrid_system = HybridMemorySystem(
                    conn, self._cfg.embed_dim, table_prefix="profile_item"
                )
                fused_results = hybrid_system.hybrid_search(
                    query=query,
                    query_vector=query_vec,
                    canonical_user_id=canonical_id,
                    top_k=80,
                ) if query else []

                if not query or not fused_results:
                    rows = conn.execute(
                        f"""
                        SELECT pi.id, pi.facet_type, pi.content, pi.confidence, pi.importance,
                               pi.stability, pi.usage_count, pi.last_confirmed_at, pi.updated_at
                        FROM profile_items pi
                        WHERE pi.canonical_user_id = ? AND pi.status = 'active'
                          {scope_cond} {persona_cond} {private_cond}
                        ORDER BY
                          CASE pi.facet_type
                            WHEN 'restriction' THEN 0
                            WHEN 'preference' THEN 1
                            WHEN 'fact' THEN 2
                            WHEN 'style' THEN 3
                            WHEN 'task_pattern' THEN 4
                            ELSE 5
                          END,
                          pi.importance DESC,
                          pi.confidence DESC,
                          pi.last_confirmed_at DESC
                        LIMIT ?
                        """,
                        (canonical_id, scope, persona_id, limit * 3),
                    ).fetchall()
                    return [dict(r) | {"_retrieval_score": float(r["importance"])} for r in rows], {}

                rrf_scores = {item["id"]: item["rrf_score"] for item in fused_results}
                hit_ids = [str(r["id"]) for r in fused_results]
                placeholders = ",".join("?" * len(hit_ids))

                rows = conn.execute(
                    f"""
                    SELECT pi.id, pi.facet_type, pi.content, pi.confidence, pi.importance,
                           pi.stability, pi.usage_count, pi.last_confirmed_at, pi.updated_at
                    FROM profile_items pi
                    WHERE pi.id IN ({placeholders}) AND pi.status = 'active'
                      AND pi.canonical_user_id = ?
                      {scope_cond} {persona_cond} {private_cond}
                    """,
                    [*hit_ids, canonical_id, scope, persona_id],
                ).fetchall()
                candidates = {int(r["id"]): dict(r) for r in rows}
                return candidates, rrf_scores

        fallback_rows, rrf_scores_or_candidates = _sync_retrieve()

        if isinstance(fallback_rows, list):
            rows = fallback_rows
            scored = []
            for row in rows:
                scored.append({
                    "id": int(row["id"]),
                    "facet_type": str(row["facet_type"]),
                    "content": str(row["content"]),
                    "confidence": float(row["confidence"]),
                    "importance": float(row["importance"]),
                    "stability": float(row["stability"]),
                    "final_score": float(row.get("_retrieval_score", row["importance"])),
                })
        else:
            candidates = fallback_rows
            rrf_scores = rrf_scores_or_candidates

            scored = []
            for row_id, row in candidates.items():
                search_relevance = rrf_scores.get(row_id, 0.0)

                if query:
                    final_score = (
                        0.25 * float(row["confidence"])
                        + 0.25 * float(row["importance"])
                        + 0.10 * float(row["stability"])
                        + 0.40 * search_relevance
                    )
                else:
                    final_score = (
                        0.35 * float(row["confidence"])
                        + 0.35 * float(row["importance"])
                        + 0.30 * float(row["stability"])
                    )

                scored.append({
                    "id": row_id,
                    "facet_type": str(row["facet_type"]),
                    "content": str(row["content"]),
                    "confidence": float(row["confidence"]),
                    "importance": float(row["importance"]),
                    "stability": float(row["stability"]),
                    "final_score": float(final_score),
                })

        scored.sort(key=lambda x: float(x.get("final_score", 0.0)), reverse=True)

        # Per-facet quota distribution
        quota = _compute_facet_quota(limit, self._FACET_QUOTA_WEIGHT)
        return _profile_dedup_with_quota(scored, limit, quota)

    def tokenize(self, text: str) -> List[str]:
        # Import dynamically if not using distill manager
        import re
        normalized = re.sub(r"\s+", " ", (text or "").strip())
        return [
            w.lower()
            for w in re.split(r"[^\w\u4e00-\u9fff]+", normalized)
            if len(w) >= 2
        ]

    def deduplicate_results(
        self, scored: List[Dict[str, object]], limit: int
    ) -> List[Dict[str, object]]:
        """对排序后的结果做轻量去重:已有高分相似记忆时跳过低分重复项。"""
        if not scored:
            return []
        accepted: List[Dict[str, object]] = []
        accepted_words: List[set] = []
        for item in scored:
            if len(accepted) >= limit:
                break
            mem_words = set(self.tokenize(str(item["memory"])))
            # 与已接受的记忆比较词重叠度
            is_dup = False
            for aw in accepted_words:
                if not mem_words or not aw:
                    continue
                overlap = len(mem_words.intersection(aw))
                ratio = overlap / min(len(mem_words), len(aw))
                if ratio > 0.7:  # 70% 以上关键词重叠视为重复
                    is_dup = True
                    break
            if not is_dup:
                accepted.append(item)
                accepted_words.append(mem_words)
        return accepted


def _compute_facet_quota(
    total: int, weights: Dict[str, float]
) -> Dict[str, int]:
    """Distribute *total* slots across facets proportionally to *weights*.

    Each facet gets at least 1 slot if its weight > 0 and total >= facet_count.
    Remaining slots are distributed by weight proportion.
    """
    active = {k: v for k, v in weights.items() if v > 0}
    if not active:
        return {}
    total_weight = sum(active.values())
    quota: Dict[str, int] = {}
    allocated = 0
    # First pass: floor allocation
    for facet, w in active.items():
        q = max(1, int(total * w / total_weight))
        quota[facet] = q
        allocated += q
    # Second pass: distribute remainder to highest-weight facets
    remaining = total - allocated
    if remaining > 0:
        sorted_facets = sorted(active, key=lambda f: active[f], reverse=True)
        for i in range(remaining):
            quota[sorted_facets[i % len(sorted_facets)]] += 1
    return quota


def _profile_dedup_with_quota(
    items: List[Dict[str, object]],
    total_limit: int,
    quota: Dict[str, int],
) -> List[Dict[str, object]]:
    """Dedup profile items respecting per-facet quotas.

    Within each facet, skip items with near-identical content prefix.
    Unused quota in one facet does NOT spill to other facets.
    """
    if not items:
        return []
    accepted: List[Dict[str, object]] = []
    facet_counts: Dict[str, int] = {}
    seen_prefixes: set = set()

    for item in items:
        if len(accepted) >= total_limit:
            break
        facet = str(item.get("facet_type", "fact"))
        facet_limit = quota.get(facet, 0)
        if facet_limit <= 0:
            continue
        current = facet_counts.get(facet, 0)
        if current >= facet_limit:
            continue
        prefix = str(item.get("content", ""))[:20].lower().strip()
        if not prefix or prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        accepted.append(item)
        facet_counts[facet] = current + 1

    return accepted


def _profile_dedup(
    items: List[Dict[str, object]], limit: int
) -> List[Dict[str, object]]:
    """Lightweight dedup for profile items: skip items with near-identical content prefix."""
    if not items:
        return []
    accepted = []
    seen_prefixes: set = set()
    for item in items:
        if len(accepted) >= limit:
            break
        prefix = str(item.get("content", ""))[:20].lower().strip()
        if not prefix or prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        accepted.append(item)
    return accepted
