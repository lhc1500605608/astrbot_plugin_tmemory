import time
import asyncio
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
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """执行底层数据库检索和重排序，返回 (未精排的候选结果列表, 需要更新reinforce的ID)
        
        实际 Rerank 将留给主类以便调用 LLM。
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
                        SELECT id, memory_type, memory, score, importance, confidence, reinforce_count,
                               last_seen_at, scope, persona_id
                        FROM memories
                        WHERE canonical_user_id=? AND is_active=1 {scope_cond} {persona_cond} {private_cond}
                        ORDER BY score DESC, updated_at DESC
                        LIMIT ?
                        """,
                        (canonical_id, *scope_params, *persona_params, limit * 3),
                    ).fetchall()
                    return [dict(r) | {"_retrieval_score": r["score"]} for r in rows], []

                rrf_scores = {item["id"]: item["rrf_score"] for item in fused_results}
                hit_ids = [str(r["id"]) for r in fused_results]
                placeholders = ",".join("?" * len(hit_ids))
                
                query_sql = f"""
                    SELECT id, memory_type, memory, score, importance, confidence, reinforce_count,
                           last_seen_at, scope, persona_id
                    FROM memories 
                    WHERE id IN ({placeholders}) AND is_active=1
                    {scope_cond} {persona_cond} {private_cond}
                """
                rows = conn.execute(query_sql, [*hit_ids, *scope_params, *persona_params]).fetchall()
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
                
                if query:
                    final_score = (
                        0.20 * float(row["score"])
                        + 0.15 * float(row["importance"])
                        + 0.15 * float(row["confidence"])
                        + 0.40 * search_relevance
                        + 0.05 * min(1.0, float(row["reinforce_count"]) / 10.0)
                        + recency_bonus
                    )
                else:
                    final_score = (
                        0.35 * float(row["score"])
                        + 0.25 * float(row["importance"])
                        + 0.20 * float(row["confidence"])
                        + 0.05 * min(1.0, float(row["reinforce_count"]) / 10.0)
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
