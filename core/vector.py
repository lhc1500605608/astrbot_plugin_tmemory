"""向量检索辅助：embed、upsert、delete、rebuild、rerank。

所有函数接收 plugin 实例作为第一参数，保留原主类的调用语义；
主类 main.py 中的方法全部委托到此处，以便瘦身 main.py。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("astrbot")


async def get_http_session(plugin):
    """获取或创建复用的 aiohttp.ClientSession。"""
    if plugin._http_session is None or plugin._http_session.closed:
        import aiohttp  # type: ignore[import-not-found]

        timeout = aiohttp.ClientTimeout(total=15)
        plugin._http_session = aiohttp.ClientSession(timeout=timeout)
    return plugin._http_session


async def embed_text(plugin, text: str) -> Optional[List[float]]:
    """生成文本向量。优先使用 VectorManager，如果不可用则回退到旧方法。

    包含:并发限流(semaphore)、429/5xx 重试(最多 2 次)、可观测计数。
    """
    if plugin._vector_manager and plugin._vector_manager.embedding_provider:
        try:
            return await plugin._vector_manager.embedding_provider.embed_text(text)
        except Exception as e:
            logger.warning("[tmemory] VectorManager embed_text failed: %s", e)

    if not plugin._vec_available or not plugin._cfg.embed_base_url:
        return None

    url = plugin._cfg.embed_base_url.rstrip("/") + "/v1/embeddings"
    payload = {"model": plugin.embed_model, "input": text[:2000]}
    headers: Dict[str, str] = {}
    if plugin._cfg.embed_api_key:
        headers["Authorization"] = f"Bearer {plugin._cfg.embed_api_key}"

    max_retries = 2
    async with plugin._embed_semaphore:
        for attempt in range(1, max_retries + 1):
            try:
                session = await get_http_session(plugin)
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        if attempt < max_retries:
                            wait = min(2.0 * attempt, 5.0)
                            logger.debug(
                                "[tmemory] embed API %d, retry %d after %.1fs",
                                resp.status,
                                attempt,
                                wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        plugin._embed_fail_count += 1
                        plugin._embed_last_error = f"HTTP {resp.status}"
                        return None
                    if resp.status != 200:
                        plugin._embed_fail_count += 1
                        plugin._embed_last_error = f"HTTP {resp.status}"
                        logger.debug("[tmemory] embed API status=%s", resp.status)
                        return None
                    data = await resp.json()
                    vec = data["data"][0]["embedding"]
                    if len(vec) != plugin._cfg.embed_dim:
                        logger.warning(
                            "[tmemory] embed dim mismatch: got %d, expected %d",
                            len(vec),
                            plugin._cfg.embed_dim,
                        )
                        plugin._embed_fail_count += 1
                        plugin._embed_last_error = (
                            f"dim mismatch {len(vec)} vs {plugin._cfg.embed_dim}"
                        )
                        return None
                    plugin._embed_ok_count += 1
                    return vec
            except Exception as e:
                if attempt < max_retries:
                    await asyncio.sleep(1.0 * attempt)
                    continue
                plugin._embed_fail_count += 1
                plugin._embed_last_error = str(e)[:200]
                logger.debug("[tmemory] _embed_text failed: %s", e)
                return None
    return None


async def upsert_vector(plugin, memory_id: int, text: str) -> bool:
    """为一条记忆生成并写入向量。成功返回 True，失败返回 False。"""
    if not plugin._vec_available:
        return False
    vec = await embed_text(plugin, text)
    if vec is None:
        return False
    try:
        blob = plugin._sqlite_vec.serialize_float32(vec)
        with plugin._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_vectors(memory_id, embedding) VALUES(?, ?)",
                (memory_id, blob),
            )
        return True
    except Exception as e:
        logger.debug("[tmemory] _upsert_vector failed for id=%s: %s", memory_id, e)
        return False


async def upsert_profile_vector(plugin, profile_item_id: int, text: str) -> bool:
    """为一条画像条目生成并写入向量到 profile_item_vectors。"""
    if not plugin._vec_available:
        return False
    vec = await embed_text(plugin, text)
    if vec is None:
        return False
    try:
        blob = plugin._sqlite_vec.serialize_float32(vec)
        with plugin._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profile_item_vectors(profile_item_id, embedding) VALUES(?, ?)",
                (profile_item_id, blob),
            )
        return True
    except Exception as e:
        logger.debug("[tmemory] upsert_profile_vector failed for id=%s: %s", profile_item_id, e)
        return False


def delete_vector(plugin, memory_id: int, conn=None) -> None:
    """删除单条记忆的向量行。"""
    if not plugin._vec_available:
        return
    try:
        if conn is not None:
            conn.execute(
                "DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,)
            )
        else:
            with plugin._db() as _conn:
                _conn.execute(
                    "DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,)
                )
    except Exception as e:
        logger.debug("[tmemory] _delete_vector failed: %s", e)


def delete_vectors_for_user(plugin, canonical_id: str, conn=None) -> None:
    """删除某用户所有记忆的向量行。"""
    if not plugin._vec_available:
        return
    try:
        sql = (
            "DELETE FROM memory_vectors WHERE memory_id IN "
            "(SELECT id FROM memories WHERE canonical_user_id = ?)"
        )
        if conn is not None:
            conn.execute(sql, (canonical_id,))
        else:
            with plugin._db() as _conn:
                _conn.execute(sql, (canonical_id,))
    except Exception as e:
        logger.debug("[tmemory] _delete_vectors_for_user failed: %s", e)


async def rebuild_vector_index(plugin) -> Tuple[int, int]:
    """为所有 is_active=1 的记忆补全向量索引(跳过已有向量的)。"""
    with plugin._db() as conn:
        rows = conn.execute(
            """
            SELECT m.id, m.memory FROM memories m
            LEFT JOIN memory_vectors v ON m.id = v.memory_id
            WHERE m.is_active = 1 AND v.memory_id IS NULL
            ORDER BY m.id ASC
            """
        ).fetchall()
        pending = [(int(r["id"]), str(r["memory"])) for r in rows]

    ok = fail = 0
    for mem_id, mem_text in pending:
        try:
            if await upsert_vector(plugin, mem_id, mem_text):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.debug("[tmemory] rebuild vector failed id=%s: %s", mem_id, e)
            fail += 1
    return ok, fail


async def rerank_results(
    plugin,
    query: str,
    candidates: List[Dict[str, object]],
    top_n: int,
) -> List[Dict[str, object]]:
    """调用 Reranker API 对候选记忆精排。

    兼容 /v1/rerank 接口(Jina、Cohere、混元、本地 rerank 服务)。
    """
    if not candidates:
        return candidates[:top_n]
    documents = [str(c["memory"]) for c in candidates]
    payload: Dict[str, object] = {
        "query": query,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
    }
    if plugin.rerank_model:
        payload["model"] = plugin.rerank_model
    headers: Dict[str, str] = {}
    rerank_api_key = getattr(plugin, "rerank_api_key", "")
    if rerank_api_key:
        headers["Authorization"] = f"Bearer {rerank_api_key}"
    url = plugin._cfg.rerank_base_url.rstrip("/") + "/v1/rerank"
    try:
        session = await get_http_session(plugin)
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                logger.debug(
                    "[tmemory] rerank API %s, fallback to score order", resp.status
                )
                return candidates[:top_n]
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                return candidates[:top_n]
            reranked = []
            for r in results:
                idx = int(r.get("index", -1))
                if 0 <= idx < len(candidates):
                    item = dict(candidates[idx])
                    item["rerank_score"] = float(r.get("relevance_score", 0.0))
                    reranked.append(item)
            return reranked[:top_n]
    except Exception as e:
        logger.debug("[tmemory] rerank failed, fallback: %s", e)
        return candidates[:top_n]
