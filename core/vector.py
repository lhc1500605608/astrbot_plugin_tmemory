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
    """生成文本向量。优先使用 VectorManager 的 Provider/独立提供者，
    提供者异常时回退到独立 HTTP 配置。

    包含:并发限流(semaphore)、429/5xx 重试(最多 2 次)、可观测计数。
    """
    vm = getattr(plugin, "_vector_manager", None)
    provider = getattr(vm, "embedding_provider", None) if vm is not None else None
    if provider is not None:
        try:
            vec = await provider.embed_text(text)
        except Exception as e:
            plugin._embed_provider_fail_count = (
                getattr(plugin, "_embed_provider_fail_count", 0) + 1
            )
            plugin._embed_last_error = f"provider_fail: {str(e)[:180]}"
            logger.warning(
                "[tmemory] provider embed_text failed, fallback to standalone: %s", e
            )
            vec = None
        if vec:
            if len(vec) != plugin._cfg.embed_dim:
                plugin._embed_fail_count += 1
                plugin._embed_last_error = (
                    f"provider dim mismatch {len(vec)} vs {plugin._cfg.embed_dim}"
                )
                logger.warning(
                    "[tmemory] provider embed dim mismatch: got %d, expected %d",
                    len(vec),
                    plugin._cfg.embed_dim,
                )
            else:
                plugin._embed_ok_count += 1
                plugin._embed_last_source = "provider"
                return vec

    if not plugin._vec_available or not plugin._cfg.embed_base_url:
        return None

    url = plugin._cfg.embed_base_url.rstrip("/") + "/v1/embeddings"
    payload = {"model": plugin.embed_model, "input": text[:2000]}
    headers: Dict[str, str] = {}
    if plugin._cfg.embed_api_key:
        headers["Authorization"] = f"Bearer {plugin._cfg.embed_api_key}"

    max_retries = 2
    if plugin._embed_semaphore is None:
        plugin._embed_semaphore = asyncio.Semaphore(4)
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
                    plugin._embed_last_source = "standalone"
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


def embedding_status(plugin) -> Dict[str, object]:
    """返回当前 embedding/rerank 来源快照（日志 / 命令 / UI 展示用）。"""
    vm = getattr(plugin, "_vector_manager", None)
    if vm is None:
        return {
            "active_source": "none",
            "embedding_source": getattr(getattr(plugin, "_cfg", None), "embedding_source", ""),
            "provider_id": "",
            "provider_dim": 0,
            "rerank_source": "disabled",
        }
    status_fn = getattr(vm, "status", None)
    if callable(status_fn):
        try:
            return status_fn()
        except Exception as e:
            logger.debug("[tmemory] vector status() failed: %s", e)
    provider = getattr(vm, "embedding_provider", None)
    return {
        "active_source": "provider" if getattr(vm, "source", "") == "provider" else (
            "standalone" if provider is not None else "none"
        ),
        "embedding_source": getattr(vm, "embedding_source", ""),
        "provider_id": getattr(vm, "provider_id", ""),
        "provider_dim": getattr(vm, "provider_dim", 0),
        "rerank_source": "provider" if getattr(vm, "rerank_provider", None) else "disabled",
    }


async def apply_provider_dim_change(plugin) -> Dict[str, object]:
    """Provider 嵌入维度与本地配置不一致时：更新维度、失效缓存、重建索引。

    仅在 ``embedding_source=provider`` 且 Provider 已解析成功时生效。
    维度未变化或非 Provider 来源直接返回 ``changed=False``。
    """
    result: Dict[str, object] = {
        "changed": False,
        "old_dim": int(plugin._cfg.embed_dim),
        "new_dim": 0,
        "cache_cleared": False,
        "rebuilt": False,
    }
    vm = getattr(plugin, "_vector_manager", None)
    provider = getattr(vm, "embedding_provider", None) if vm is not None else None
    if provider is None or getattr(vm, "source", "") != "provider":
        return result

    try:
        new_dim = int(getattr(vm, "provider_dim", 0) or 0)
    except Exception:
        new_dim = 0
    result["new_dim"] = new_dim
    old_dim = int(plugin._cfg.embed_dim)
    if new_dim <= 0 or new_dim == old_dim:
        return result

    auto_rebuild = bool(getattr(plugin._cfg, "auto_rebuild_on_dim_change", True))
    logger.warning(
        "[tmemory] embedding dim changed %d -> %d (provider=%s, auto_rebuild=%s)",
        old_dim,
        new_dim,
        getattr(vm, "provider_id", "") or "<auto>",
        auto_rebuild,
    )
    plugin._cfg.embed_dim = new_dim
    result["changed"] = True

    # Query embedding 缓存与维度绑定：变更后必须整体失效，避免旧维度向量污染检索。
    try:
        with plugin._db() as conn:
            conn.execute("DELETE FROM query_embedding_cache")
        result["cache_cleared"] = True
    except Exception as e:
        logger.warning("[tmemory] query embedding cache clear failed: %s", e)

    if plugin._vec_available and auto_rebuild:
        try:
            with plugin._db() as conn:
                conn.execute("DROP TABLE IF EXISTS memory_vectors")
                conn.execute("DROP TABLE IF EXISTS profile_item_vectors")
            plugin._db_mgr.init_db(True, new_dim)
            ok, fail = await rebuild_vector_index(plugin)
            result["rebuilt"] = True
            result["rebuilt_ok"] = ok
            result["rebuilt_fail"] = fail
            logger.info(
                "[tmemory] vector index rebuilt after dim change: ok=%d fail=%d", ok, fail
            )
        except Exception as e:
            logger.error("[tmemory] vector index rebuild after dim change failed: %s", e)
            result["error"] = str(e)[:200]
    return result


async def get_cached_query_embedding(
    plugin, query: str
) -> Optional[List[float]]:
    """Look up a cached embedding for *query*. Returns None on miss.

    Updates hit_count and last_hit_at on cache hit.
    """
    import hashlib

    if not plugin._vec_available:
        return None
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    try:
        with plugin._db() as conn:
            row = conn.execute(
                "SELECT embedding, embed_dim FROM query_embedding_cache WHERE query_hash=?",
                (query_hash,),
            ).fetchone()
            if row is None:
                plugin._embed_cache_miss_count += 1
                return None
            blob = bytes(row["embedding"])
            dim = int(row["embed_dim"])
            if dim != plugin._cfg.embed_dim:
                conn.execute(
                    "DELETE FROM query_embedding_cache WHERE query_hash=?", (query_hash,)
                )
                plugin._embed_cache_miss_count += 1
                return None
            conn.execute(
                "UPDATE query_embedding_cache SET hit_count=hit_count+1, last_hit_at=? WHERE query_hash=?",
                (plugin._now(), query_hash),
            )
            plugin._embed_cache_hit_count += 1
            return plugin._sqlite_vec.deserialize_float32(blob)
    except Exception as e:
        logger.debug("[tmemory] query embedding cache lookup failed: %s", e)
        return None


async def store_query_embedding(plugin, query: str, vec: List[float]) -> None:
    """Store a generated query embedding in the cache."""
    import hashlib

    if not plugin._vec_available:
        return
    query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    blob = plugin._sqlite_vec.serialize_float32(vec)
    now = plugin._now()
    try:
        with plugin._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO query_embedding_cache("
                "query_hash, query_text, embedding, embed_dim, created_at, last_hit_at, hit_count"
                ") VALUES(?, ?, ?, ?, ?, ?, 1)",
                (query_hash, query[:500], blob, len(vec), now, now),
            )
    except Exception as e:
        logger.debug("[tmemory] query embedding cache store failed: %s", e)


async def get_or_generate_query_embedding(
    plugin, query: str
) -> Optional[List[float]]:
    """Cache-first query embedding: check local SQLite cache, fall back to embed API.

    Updates cache hit/miss counters for observability.
    Zero LLM calls — only local SQLite reads or embedding API call.
    """
    if not plugin._vec_available:
        return None
    if not query or not query.strip():
        return None
    # Trim query to avoid embedding very long text on the hot path
    trimmed = query[:2000]
    cached = await get_cached_query_embedding(plugin, trimmed)
    if cached is not None:
        return cached
    vec = await embed_text(plugin, trimmed)
    if vec is not None:
        await store_query_embedding(plugin, trimmed, vec)
    return vec


async def rerank_results(
    plugin,
    query: str,
    candidates: List[Dict[str, object]],
    top_n: int,
) -> List[Dict[str, object]]:
    """调用 Reranker 对候选记忆精排。

    优先使用 AstrBot Rerank Provider（``rerank_provider_id``），缺失/异常时
    回退独立 ``/v1/rerank`` 接口(Jina、Cohere、混元、本地 rerank 服务)。
    """
    if not candidates:
        return candidates[:top_n]
    documents = [str(c["memory"]) for c in candidates]

    vm = getattr(plugin, "_vector_manager", None)
    rerank_provider = getattr(vm, "rerank_provider", None) if vm is not None else None
    if rerank_provider is not None:
        try:
            results = await rerank_provider.rerank(
                query, documents, min(top_n, len(documents))
            )
            reranked_provider: List[Dict[str, object]] = []
            for r in results or []:
                idx = int(r.get("index", -1))
                if 0 <= idx < len(candidates):
                    item = dict(candidates[idx])
                    item["rerank_score"] = float(r.get("relevance_score", 0.0))
                    reranked_provider.append(item)
            if reranked_provider:
                return reranked_provider[:top_n]
        except Exception as e:
            logger.warning(
                "[tmemory] provider rerank failed, fallback to HTTP rerank: %s", e
            )

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
