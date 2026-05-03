"""记忆维护：提纯、剪枝、衰减、统计、LLM 辅助与对话缓存辅助。

每个函数接受 plugin 实例作为第一参数，直接复用 plugin 原先依赖
(`_db`, `_now`, `_cfg`, `_distill_mgr`, `_memory_logger`, `context` 等)。
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("astrbot")


# =============================================================================
# Purify / LLM judge / split
# =============================================================================

async def run_memory_purify(plugin) -> Tuple[int, int]:
    """对全量已蒸馏记忆进行提纯。见原 _run_memory_purify。"""
    pruned_rule = 0
    if plugin._cfg.purify_min_score > 0:
        with plugin._db() as conn:
            rows = conn.execute(
                "SELECT id, score, importance, confidence FROM memories WHERE is_active=1 AND is_pinned=0"
            ).fetchall()
            for row in rows:
                quality = (
                    0.3 * float(row["score"])
                    + 0.4 * float(row["importance"])
                    + 0.3 * float(row["confidence"])
                )
                if quality < plugin._cfg.purify_min_score:
                    conn.execute(
                        "UPDATE memories SET is_active=0 WHERE id=?",
                        (int(row["id"]),),
                    )
                    pruned_rule += 1

    pruned_llm = 0
    kept = 0
    provider_id = (
        plugin._cfg.purify_model_id
        or plugin._cfg.distill_model_id
        or plugin._cfg.distill_provider_id
    )
    if not provider_id:
        with plugin._db() as conn:
            kept = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE is_active=1"
            ).fetchone()[0]
        return pruned_rule, int(kept)

    with plugin._db() as conn:
        user_rows = conn.execute(
            "SELECT DISTINCT canonical_user_id FROM memories WHERE is_active=1 AND is_pinned=0"
        ).fetchall()

    for user_row in user_rows:
        uid = str(user_row["canonical_user_id"])
        try:
            with plugin._db() as conn:
                mems = conn.execute(
                    "SELECT id, memory, memory_type, score, importance, confidence, reinforce_count "
                    "FROM memories WHERE canonical_user_id=? AND is_active=1 AND is_pinned=0 "
                    "ORDER BY importance ASC LIMIT 50",
                    (uid,),
                ).fetchall()
                mem_list = [dict(r) for r in mems]

            if not mem_list:
                continue

            ids_to_deactivate = await llm_purify_judge(plugin, provider_id, mem_list)
            if ids_to_deactivate:
                with plugin._db() as conn:
                    for mid in ids_to_deactivate:
                        conn.execute(
                            "UPDATE memories SET is_active=0 WHERE id=?", (mid,)
                        )
                pruned_llm += len(ids_to_deactivate)
        except Exception as e:
            logger.debug("[tmemory] memory_purify user=%s error: %s", uid, e)

    with plugin._db() as conn:
        kept = int(
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE is_active=1"
            ).fetchone()[0]
        )

    return pruned_rule + pruned_llm, kept


async def llm_purify_judge(
    plugin, provider_id: str, memories: List[Dict]
) -> List[int]:
    """让 LLM 评判一批记忆的质量，返回应该失活的记忆 ID 列表。"""
    prompt = (
        "你是记忆质量审核员。请对以下用户记忆进行质量评估，识别出不值得长期保留的记忆。\n"
        "不值得保留的记忆特征:\n"
        "- 内容模糊、无实质信息(如'用户说了一些话')\\n"
        "- 一次性事件，不反映用户稳定特征\n"
        "- 与其他记忆严重重复\n"
        "- 置信度极低(< 0.3)\n"
        "- 重要性极低且从未被强化召回\n\n"
        '只输出 JSON，格式:{"deactivate": [id1, id2, ...]}\n'
        '如果全部值得保留，返回:{"deactivate": []}\n\n'
        f"待审核记忆:\n{json.dumps(memories, ensure_ascii=False)}"
    )
    try:
        resp = await plugin.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
        )
        txt = plugin._strip_think_tags(
            plugin._normalize_text(getattr(resp, "completion_text", "") or "")
        )
        obj = plugin._parse_json_object(txt)
        if obj and isinstance(obj.get("deactivate"), list):
            return [int(x) for x in obj["deactivate"] if str(x).isdigit()]
    except Exception as e:
        logger.debug("[tmemory] _llm_purify_judge failed: %s", e)
    return []


async def llm_purify_operations(
    plugin,
    event,
    rows: List[Dict[str, object]],
    mode: str,
    extra_instruction: str,
) -> Dict[str, object]:
    """让 LLM 生成对已有记忆的手动提纯操作(更新/新增/删除)。"""
    prompt = (
        "你是记忆编辑器。请基于现有记忆做精炼优化。只输出 JSON，不要解释。\n"
        "目标:去重、合并重复、拆分过长、删除无意义条目。\n"
        f"模式: {mode}\n"
        f"附加要求: {extra_instruction or '无'}\n\n"
        "输出格式:\n"
        "{\n"
        '  "updates": [{"id": 1, "memory": "...", "memory_type": "...", "importance": 0.6, "confidence": 0.8, "score": 0.7}],\n'
        '  "adds": [{"memory": "...", "memory_type": "...", "importance": 0.6, "confidence": 0.8, "score": 0.7}],\n'
        '  "deletes": [3,4],\n'
        '  "note": "可选说明"\n'
        "}\n\n"
        "规则:\n"
        "1) updates 只允许引用输入里存在的 id。\n"
        "2) memory 必须以‘用户’为主语，避免废话。\n"
        "3) 删除明显重复/低价值/噪声记忆。\n"
        "4) mode=merge 时优先减少条目;mode=split 时优先拆分复合记忆;both 两者都可。\n"
        "5) 不要引入输入中不存在的新事实。\n\n"
        f"输入记忆:{json.dumps(rows, ensure_ascii=False)}"
    )

    provider_id = ""
    try:
        provider_id = await plugin.context.get_current_chat_provider_id(
            umo=plugin._safe_get_unified_msg_origin(event)
        )
    except Exception:
        provider_id = plugin._cfg.distill_provider_id

    if not provider_id:
        return {"updates": [], "adds": [], "deletes": [], "note": "no provider"}

    try:
        resp = await plugin.context.llm_generate(
            chat_provider_id=str(provider_id), prompt=prompt
        )
        txt = plugin._strip_think_tags(
            plugin._normalize_text(getattr(resp, "completion_text", "") or "")
        )
        obj = plugin._parse_json_object(txt)
        if isinstance(obj, dict):
            return obj
    except Exception as e:
        logger.warning("[tmemory] _llm_purify_operations failed: %s", e)
    return {"updates": [], "adds": [], "deletes": [], "note": "llm failed"}


async def llm_split_memory(plugin, event, memory_text: str) -> List[str]:
    """使用 LLM 将一条复合记忆拆分为多条。"""
    prompt = (
        "将以下一条用户记忆拆分为 2~5 条更原子化的记忆。\n"
        '只输出 JSON:{"segments":["用户...","用户..."]}\n'
        "每条必须以‘用户’开头，避免废话。\n"
        f"原记忆: {memory_text}"
    )

    provider_id = ""
    try:
        provider_id = await plugin.context.get_current_chat_provider_id(
            umo=plugin._safe_get_unified_msg_origin(event)
        )
    except Exception:
        provider_id = plugin._cfg.distill_provider_id

    if not provider_id:
        return [
            x.strip()
            for x in re.split(r"[;;，,]", memory_text)
            if len(x.strip()) >= 6
        ]

    try:
        resp = await plugin.context.llm_generate(
            chat_provider_id=str(provider_id), prompt=prompt
        )
        txt = plugin._strip_think_tags(
            plugin._normalize_text(getattr(resp, "completion_text", "") or "")
        )
        obj = plugin._parse_json_object(txt)
        if isinstance(obj, dict) and isinstance(obj.get("segments"), list):
            segs = [
                plugin._normalize_text(str(s))
                for s in obj["segments"]
                if plugin._normalize_text(str(s))
            ]
            if len(segs) >= 2:
                return segs[:5]
    except Exception as e:
        logger.debug("[tmemory] _llm_split_memory failed: %s", e)

    return [
        x.strip() for x in re.split(r"[;;，,]", memory_text) if len(x.strip()) >= 6
    ]


# =============================================================================
# Decay / Prune / Stats / Export / Purge
# =============================================================================

def decay_stale_memories(plugin) -> None:
    """将长期未命中的记忆标记为 stale(is_active=2)，超久的归档(is_active=3)。

    同时对 attention_score 应用指数衰减：未被召回的记忆随时间衰减，
    衰减率 λ=0.05/天，使 attention_score 综合 reinforce_count + freshness + initial_importance。
    """
    now_ts = int(time.time())
    stale_threshold = 30 * 86400
    archive_threshold = 90 * 86400
    decay_rate = 0.05  # per day

    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT id, last_seen_at, attention_score FROM memories WHERE is_active = 1 AND is_pinned = 0"
        ).fetchall()
        for row in rows:
            try:
                last_ts = int(
                    time.mktime(
                        time.strptime(str(row["last_seen_at"]), "%Y-%m-%d %H:%M:%S")
                    )
                )
            except Exception:
                continue
            age = now_ts - last_ts
            if age > archive_threshold:
                conn.execute(
                    "UPDATE memories SET is_active = 3 WHERE id = ?",
                    (int(row["id"]),),
                )
            elif age > stale_threshold:
                conn.execute(
                    "UPDATE memories SET is_active = 2 WHERE id = ?",
                    (int(row["id"]),),
                )

            # 指数衰减 attention_score（未被召回的时间越长衰减越多）
            days_since_seen = max(0.0, age / 86400.0)
            decay_factor = math.exp(-decay_rate * days_since_seen)
            current_attn = float(row["attention_score"] or 0.5)
            new_attn = max(0.05, current_attn * decay_factor)
            if abs(new_attn - current_attn) > 0.001:
                conn.execute(
                    "UPDATE memories SET attention_score = ? WHERE id = ?",
                    (new_attn, int(row["id"])),
                )

    auto_prune_low_quality(plugin)


def auto_prune_low_quality(plugin) -> None:
    """自动剪枝低质量记忆:低分 + 低强化次数 + 超过 7 天的记忆直接失效。"""
    now_ts = int(time.time())
    prune_age = 7 * 86400

    with plugin._db() as conn:
        rows = conn.execute(
            """
            SELECT id, score, importance, confidence, reinforce_count, created_at
            FROM memories WHERE is_active = 1 AND is_pinned = 0
            """
        ).fetchall()

        pruned = 0
        for row in rows:
            try:
                created_ts = int(
                    time.mktime(
                        time.strptime(str(row["created_at"]), "%Y-%m-%d %H:%M:%S")
                    )
                )
            except Exception:
                continue
            age = now_ts - created_ts
            if age < prune_age:
                continue

            score = float(row["score"])
            importance = float(row["importance"])
            confidence = float(row["confidence"])
            reinforce = int(row["reinforce_count"])

            quality = 0.3 * score + 0.4 * importance + 0.3 * confidence
            if quality < 0.35 and reinforce <= 1:
                conn.execute(
                    "UPDATE memories SET is_active = 0 WHERE id = ?",
                    (int(row["id"]),),
                )
                pruned += 1

        if pruned > 0:
            logger.info("[tmemory] auto-pruned %d low-quality memories", pruned)


def export_user_data(plugin, canonical_id: str) -> Dict:
    memories = plugin._list_memories(canonical_id, limit=500)
    with plugin._db() as conn:
        bindings = [
            dict(r)
            for r in conn.execute(
                "SELECT adapter, adapter_user_id FROM identity_bindings WHERE canonical_user_id = ?",
                (canonical_id,),
            ).fetchall()
        ]
    return {
        "canonical_user_id": canonical_id,
        "memories": memories,
        "bindings": bindings,
        "exported_at": plugin._now(),
    }


def purge_user_data(plugin, canonical_id: str) -> Dict[str, int]:
    with plugin._db() as conn:
        plugin._delete_vectors_for_user(canonical_id, conn=conn)
        m = conn.execute(
            "DELETE FROM memories WHERE canonical_user_id = ?", (canonical_id,)
        ).rowcount
        c = conn.execute(
            "DELETE FROM conversation_cache WHERE canonical_user_id = ?",
            (canonical_id,),
        ).rowcount
        conn.execute(
            "DELETE FROM memory_events WHERE canonical_user_id = ?",
            (canonical_id,),
        )
        conn.execute(
            "DELETE FROM conversations WHERE canonical_user_id = ?",
            (canonical_id,),
        )
        conn.execute(
            "DELETE FROM identity_bindings WHERE canonical_user_id = ?",
            (canonical_id,),
        )
    plugin._memory_logger.log_memory_event(
        canonical_user_id=canonical_id,
        event_type="purge",
        payload={"memories_deleted": m, "cache_deleted": c},
    )
    return {"memories": m, "cache": c}


def get_global_stats(plugin) -> Dict[str, Any]:
    """获取全局统计信息（含 0.8.0 三层架构指标）。"""
    with plugin._db() as conn:
        total_users = conn.execute(
            "SELECT COUNT(DISTINCT canonical_user_id) FROM memories"
        ).fetchone()[0]
        active_memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE is_active = 1"
        ).fetchone()[0]
        deactivated_memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE is_active = 0"
        ).fetchone()[0]
        pending_cached = conn.execute(
            "SELECT COUNT(*) FROM conversation_cache WHERE distilled = 0"
        ).fetchone()[0]
        total_events = conn.execute(
            "SELECT COUNT(*) FROM memory_events"
        ).fetchone()[0]
        vector_index_rows = 0
        if plugin._vec_available:
            try:
                vector_index_rows = conn.execute(
                    "SELECT COUNT(*) FROM memory_vectors"
                ).fetchone()[0]
            except Exception:
                pass

        # ── 0.8.0 three-tier metrics ──────────────────────────────────
        total_episodes = conn.execute(
            "SELECT COUNT(*) FROM memory_episodes"
        ).fetchone()[0]
        active_episodes = conn.execute(
            "SELECT COUNT(*) FROM memory_episodes WHERE status='ongoing'"
        ).fetchone()[0]
        pending_consolidation = conn.execute(
            "SELECT COUNT(*) FROM memory_episodes WHERE consolidation_status='pending_semantic'"
        ).fetchone()[0]
        processed_consolidation = conn.execute(
            "SELECT COUNT(*) FROM memory_episodes WHERE consolidation_status!='pending_semantic'"
        ).fetchone()[0]
        semantic_layer = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE derived_from='episode'"
        ).fetchone()[0]
        last_consolidation_row = conn.execute(
            "SELECT finished_at FROM distill_history ORDER BY id DESC LIMIT 1"
        ).fetchone()

        # ── profile metrics ─────────────────────────────────────────────
        profile_user_count = conn.execute(
            "SELECT COUNT(*) FROM user_profiles"
        ).fetchone()[0]
        profile_item_count = conn.execute(
            "SELECT COUNT(*) FROM profile_items WHERE status='active'"
        ).fetchone()[0]

    return {
        "total_users": int(total_users),
        "total_active_memories": int(active_memories),
        "total_deactivated_memories": int(deactivated_memories),
        "pending_cached_rows": int(pending_cached),
        "total_events": int(total_events),
        "vector_index_rows": int(vector_index_rows),
        # three-tier
        "total_episodes": int(total_episodes),
        "active_episodes": int(active_episodes),
        "pending_consolidation": int(pending_consolidation),
        "processed_consolidation": int(processed_consolidation),
        "working_layer_count": int(pending_cached),
        "episodic_layer_count": int(total_episodes),
        "semantic_layer_count": int(semantic_layer),
        "last_consolidation_at": str(last_consolidation_row["finished_at"]) if last_consolidation_row else None,
        # profile
        "profile_user_count": int(profile_user_count),
        "profile_item_count": int(profile_item_count),
    }


# =============================================================================
# Conversation cache helpers
# =============================================================================

def insert_conversation_sync(
    plugin,
    canonical_id: str,
    role: str,
    content: str,
    source_adapter: str,
    source_user_id: str,
    unified_msg_origin: str,
    scope: str = "user",
    persona_id: str = "",
) -> None:
    truncated = content[:1000]

    try:
        if plugin._cfg.capture_dedup_window > 0:
            with plugin._db() as conn:
                exists = conn.execute(
                    """
                    SELECT 1 FROM conversation_cache
                    WHERE canonical_user_id=? AND distilled=0
                    AND id IN (
                        SELECT id FROM conversation_cache
                        WHERE canonical_user_id=? AND distilled=0
                        ORDER BY id DESC LIMIT ?
                    )
                    AND content=?
                    LIMIT 1
                    """,
                    (canonical_id, canonical_id, plugin._cfg.capture_dedup_window, truncated),
                ).fetchone()
                if exists:
                    return

        with plugin._db() as conn:
            conn.execute(
                """
                INSERT INTO conversation_cache(
                    canonical_user_id, role, content, source_adapter, source_user_id,
                    unified_msg_origin, distilled, distilled_at, created_at, scope,
                    persona_id, session_key
                ) VALUES(?, ?, ?, ?, ?, ?, 0, '', ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    role,
                    truncated,
                    source_adapter,
                    source_user_id,
                    unified_msg_origin,
                    plugin._now(),
                    scope,
                    persona_id,
                    unified_msg_origin,
                ),
            )
    except Exception as e:
        logger.warning(
            "[tmemory] _insert_conversation failed for user %s: %s",
            canonical_id,
            e,
        )


def optimize_context(plugin, canonical_id: str) -> None:
    """对超出阈值的历史做轻量规则摘要压缩，不触发 LLM，以节省 token。"""
    rows = plugin._fetch_recent_conversation(canonical_id, limit=200)
    if len(rows) <= plugin._cfg.cache_max_rows:
        return

    joined = " ".join([c for _, c in rows[: -plugin._cfg.cache_max_rows]])
    summary = plugin._distill_mgr.distill_text(joined)
    now = plugin._now()

    with plugin._db() as conn:
        conn.execute(
            """
            INSERT INTO conversation_cache(
                canonical_user_id, role, content, source_adapter, source_user_id,
                unified_msg_origin, distilled, distilled_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                canonical_id,
                "summary",
                f"[auto-summary] {summary}",
                "system",
                canonical_id,
                "",
                now,
                now,
            ),
        )

    plugin._trim_conversation(canonical_id, keep_last=plugin._cfg.cache_max_rows)
