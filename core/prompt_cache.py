"""蒸馏 prompt/结果缓存（Plan TMEAAA-379 T3）。

相同 transcript（同一模型、同一对话内容）的蒸馏结果可直接复用：
- 命中缓存时不再调用 LLM，token 记为 0（与 ``daily_token_budget`` 联动，节省预算）；
- 缓存仅影响成本，不改变对外产出（结果与首次 LLM 蒸馏一致）。

缓存键 = ``sha256(model_id + "\\x00" + transcript)``，避免不同模型间串用结果。
所有函数接受 plugin 实例，复用其 ``_db`` / ``_now``。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("astrbot")

_PROMPT_CACHE_KEY_SEP = "\x00"


def compute_cache_key(transcript: str, model_id: str = "") -> str:
    payload = f"{str(model_id or '').strip()}{_PROMPT_CACHE_KEY_SEP}{str(transcript or '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_cached_distill_items(plugin, cache_key: str) -> Optional[List[Dict]]:
    """读取缓存；命中时更新 hit_count 并返回 items，未命中/损坏返回 None。"""
    if not cache_key:
        return None
    try:
        with plugin._db() as conn:
            row = conn.execute(
                "SELECT memories_json, hit_count FROM distill_prompt_cache"
                " WHERE transcript_hash=?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            try:
                items = json.loads(row["memories_json"])
            except (TypeError, ValueError):
                conn.execute(
                    "DELETE FROM distill_prompt_cache WHERE transcript_hash=?",
                    (cache_key,),
                )
                return None
            conn.execute(
                "UPDATE distill_prompt_cache SET hit_count=hit_count+1, last_hit_at=?"
                " WHERE transcript_hash=?",
                (plugin._now(), cache_key),
            )
        if not isinstance(items, list):
            return None
        return [item for item in items if isinstance(item, dict)]
    except Exception as e:
        logger.debug("[tmemory] distill prompt cache read failed: %s", e)
        return None


def store_distill_items(
    plugin,
    cache_key: str,
    transcript: str,
    items: List[Dict],
    model_id: str = "",
) -> None:
    """写入缓存（幂等），并按 ``distill_prompt_cache_max_rows`` 做 LRU 淘汰。"""
    if not cache_key or items is None:
        return
    now = plugin._now()
    try:
        payload = json.dumps(items, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    try:
        with plugin._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO distill_prompt_cache("
                "transcript_hash, transcript, memories_json, model_id,"
                " created_at, last_hit_at, hit_count"
                ") VALUES(?, ?, ?, ?, ?, ?, 0)",
                (cache_key, str(transcript or "")[:20000], payload, str(model_id or ""), now, now),
            )
            max_rows = int(getattr(plugin._cfg, "distill_prompt_cache_max_rows", 0) or 0)
            if max_rows > 0:
                conn.execute(
                    "DELETE FROM distill_prompt_cache WHERE transcript_hash NOT IN ("
                    " SELECT transcript_hash FROM distill_prompt_cache"
                    " ORDER BY last_hit_at DESC, created_at DESC, rowid DESC LIMIT ?"
                    ")",
                    (max_rows,),
                )
    except Exception as e:
        logger.debug("[tmemory] distill prompt cache write failed: %s", e)


def clear_distill_prompt_cache(plugin) -> int:
    """清空缓存，返回删除条数。"""
    try:
        with plugin._db() as conn:
            cur = conn.execute("DELETE FROM distill_prompt_cache")
            return int(cur.rowcount or 0)
    except Exception as e:
        logger.debug("[tmemory] distill prompt cache clear failed: %s", e)
        return 0


def prompt_cache_stats(plugin) -> Dict[str, object]:
    """返回缓存条目数与累计命中数（供状态/成本视图展示）。"""
    try:
        with plugin._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS entries, COALESCE(SUM(hit_count), 0) AS hits"
                " FROM distill_prompt_cache"
            ).fetchone()
        return {"entries": int(row["entries"] or 0), "hits": int(row["hits"] or 0)}
    except Exception:
        return {"entries": 0, "hits": 0}


__all__ = [
    "clear_distill_prompt_cache",
    "compute_cache_key",
    "get_cached_distill_items",
    "prompt_cache_stats",
    "store_distill_items",
]
