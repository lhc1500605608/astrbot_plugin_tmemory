import json
import logging
import sqlite3
import hashlib
from typing import Dict, Any, List, Optional
import jieba

logger = logging.getLogger("astrbot:db.py")

class MemoryOps:
    """记忆相关数据库操作与净化逻辑。"""
    
    def __init__(self, plugin):
        self.plugin = plugin

    def insert_memory(
        self,
        canonical_id: str,
        adapter: str,
        adapter_user: str,
        memory: str,
        score: float,
        memory_type: str,
        importance: float,
        confidence: float,
        source_channel: str = "default",
        persona_id: str = "",
        scope: str = "user",
    ) -> int:
        normalized = self.plugin._normalize_text(memory)
        mhash = hashlib.sha256(
            f"{persona_id}:{scope}:{normalized}".encode("utf-8")
        ).hexdigest()
        now = self.plugin._now()
        memory_type_safe = self.plugin._safe_memory_type(memory_type)
        tokenized_memory = " ".join(jieba.cut_for_search(memory))
        with self.plugin._db() as conn:
            row = conn.execute(
                "SELECT id, reinforce_count FROM memories WHERE canonical_user_id=? AND memory_hash=?",
                (canonical_id, mhash),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE memories
                    SET score=?, memory_type=?, importance=MAX(importance, ?), confidence=MAX(confidence, ?),
                        reinforce_count=?, last_seen_at=?, updated_at=?, tokenized_memory=?
                    WHERE id=?
                    """,
                    (
                        self.plugin._clamp01(score),
                        memory_type_safe,
                        self.plugin._clamp01(importance),
                        self.plugin._clamp01(confidence),
                        int(row["reinforce_count"]) + 1,
                        now,
                        now,
                        tokenized_memory,
                        int(row["id"]),
                    ),
                )
                return int(row["id"])

            new_words = set(self.plugin._retrieval_mgr.tokenize(normalized))
            candidate_rows = conn.execute(
                """
                SELECT id, memory FROM memories
                WHERE canonical_user_id=? AND memory_type=? AND is_active=1 AND is_pinned=0
                ORDER BY created_at DESC
                LIMIT 15
                """,
                (canonical_id, memory_type_safe),
            ).fetchall()

            deactivated = 0
            for cand in candidate_rows:
                cand_words = set(self.plugin._retrieval_mgr.tokenize(str(cand["memory"])))
                overlap = len(new_words.intersection(cand_words))
                if overlap >= max(1, min(len(new_words), len(cand_words)) * 0.5):
                    conn.execute(
                        """
                        UPDATE memories SET is_active=0, updated_at=? WHERE id=?
                        """,
                        (now, int(cand["id"])),
                    )
                    deactivated += 1

            cur = conn.execute(
                """
                INSERT INTO memories(
                    canonical_user_id, source_adapter, source_user_id, source_channel, memory_type,
                    memory, tokenized_memory, memory_hash, score, importance, confidence, reinforce_count, is_active,
                    last_seen_at, created_at, updated_at, persona_id, scope
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    adapter,
                    adapter_user,
                    source_channel,
                    memory_type_safe,
                    memory,
                    tokenized_memory,
                    mhash,
                    self.plugin._clamp01(score),
                    self.plugin._clamp01(importance),
                    self.plugin._clamp01(confidence),
                    1,
                    1,
                    now,
                    now,
                    now,
                    persona_id,
                    scope,
                ),
            )
            new_id = int(cur.lastrowid or 0)

            if deactivated > 0:
                self.plugin._memory_logger.log_memory_event(
                    canonical_user_id=canonical_id,
                    event_type="create_with_conflict",
                    payload={
                        "new_memory_id": new_id,
                        "memory_type": memory_type_safe,
                        "deactivated_count": deactivated,
                    },
                    conn=conn,
                )
            else:
                self.plugin._memory_logger.log_memory_event(
                    canonical_user_id=canonical_id,
                    event_type="create",
                    payload={
                        "memory_id": new_id,
                        "memory_type": memory_type_safe,
                    },
                    conn=conn,
                )

            return new_id

    async def distill_rows_with_llm(
        self, rows: list
    ) -> tuple:
        """用 LLM 对一批对话行进行结构化蒸馏，失败时回退到规则蒸馏。"""
        transcript_lines = []
        for row in rows:
            role = str(row["role"])
            content = str(row["content"])
            transcript_lines.append(f"{role}: {content}")

        transcript = "\n".join(transcript_lines)
        enable_style = self.plugin._cfg.enable_style_distill
        persona_profile = self.plugin._cfg.persona_profile if enable_style else ""
        prompt = self.plugin._distill_mgr.build_distill_prompt(transcript, persona_profile, enable_style=enable_style)

        chat_provider_id = await self.plugin._distill_mgr.resolve_distill_provider_id(rows, self.plugin.context)
        chat_model_id = await self.plugin._distill_mgr.resolve_distill_model_id(rows)
        if not chat_provider_id:
            # 无法确定 provider 时，回退到规则蒸馏。
            return (
                [
                    {
                        "memory": self.plugin._distill_mgr.distill_text(transcript),
                        "memory_type": "fact",
                        "importance": 0.55,
                        "confidence": 0.50,
                        "score": 0.60,
                    }
                ],
                -1,
                -1,
            )

        try:
            llm_generate_kwargs = {
                "chat_provider_id": chat_provider_id,
                "prompt": prompt,
            }
            if chat_model_id:
                llm_generate_kwargs["model_id"] = chat_model_id

            llm_resp = await self.plugin.context.llm_generate(**llm_generate_kwargs)
            completion_text = self.plugin._normalize_text(
                getattr(llm_resp, "completion_text", "") or ""
            )
            completion_text = self.plugin._strip_think_tags(completion_text)
            parsed = self.plugin._parse_llm_json_memories(completion_text)

            usage = getattr(llm_resp, "usage", None)
            if usage is not None:
                tok_in = int(getattr(usage, "input_other", 0) or 0) + int(
                    getattr(usage, "input_cached", 0) or 0
                )
                tok_out = int(getattr(usage, "output", 0) or 0)
            else:
                tok_in, tok_out = -1, -1

            if parsed:
                return parsed, tok_in, tok_out
        except Exception as e:
            logger.warning("[tmemory] llm distill failed, fallback to rule: %s", e)

        return (
            [
                {
                    "memory": self.plugin._distill_mgr.distill_text(transcript),
                    "memory_type": "fact",
                    "importance": 0.55,
                    "confidence": 0.50,
                    "score": 0.60,
                }
            ],
            -1,
            -1,
        )

    async def run_distill_cycle(
        self,
        force: bool = False,
        trigger: str = "manual"
    ) -> tuple[int, int]:
        """执行一轮蒸馏，记录历史，单用户失败不中断整轮。"""
        import time
        started_at = self.plugin._now()
        t0 = time.time()
        min_required = 1 if force else self.plugin._cfg.distill_min_batch_count
        pending_users = self.plugin._pending_distill_users(
            limit=(100 if force else 20), min_batch_count=min_required
        )
        processed_users = 0
        total_memories = 0
        failed_users = 0
        errors = []
        cycle_tok_in = -1
        cycle_tok_out = -1

        now_ts = time.time()
        for canonical_id in pending_users:
            try:
                # 用户级节流：force 模式跳过冷却检查
                if (not force) and self.plugin._cfg.distill_user_throttle_sec > 0:
                    last_ts = self.plugin._user_last_distilled_ts.get(canonical_id, 0.0)
                    if now_ts - last_ts < self.plugin._cfg.distill_user_throttle_sec:
                        continue  # 冷却中，跳过本用户

                rows = self.plugin._fetch_pending_rows(canonical_id, self.plugin._cfg.distill_batch_limit)
                if (not force) and len(rows) < self.plugin._cfg.distill_min_batch_count:
                    continue

                # 预过滤：在送入 LLM 前过滤掉低信息量行，减少 token 消耗
                rows_for_llm = self.plugin._prefilter_distill_rows(rows)
                if not rows_for_llm:
                    # 所有行均为低信息量，直接标记为已蒸馏跳过
                    self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                    self.plugin._distill_skipped_rows += len(rows)
                    processed_users += 1
                    self.plugin._user_last_distilled_ts[canonical_id] = now_ts
                    continue

                if not any(str(r.get("role", "")) == "user" for r in rows_for_llm):
                    # assistant-only 片段不能证明用户风格，避免误归因给用户。
                    self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                    self.plugin._distill_skipped_rows += len(rows)
                    processed_users += 1
                    self.plugin._user_last_distilled_ts[canonical_id] = now_ts
                    continue

                skipped = len(rows) - len(rows_for_llm)
                self.plugin._distill_skipped_rows += skipped

                llm_items, tok_in, tok_out = await self.plugin._distill_rows_with_llm(rows_for_llm)

                # 累加 token 计数（-1 表示 provider 未返回，跳过累加）
                if tok_in >= 0:
                    cycle_tok_in = max(cycle_tok_in, 0) + tok_in
                if tok_out >= 0:
                    cycle_tok_out = max(cycle_tok_out, 0) + tok_out

                if not llm_items:
                    self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                    self.plugin._user_last_distilled_ts[canonical_id] = now_ts
                    continue

                valid_items = self.plugin._validate_distill_output(llm_items)
                has_style = False
                for item in valid_items:
                    mem_text = self.plugin._sanitize_text(
                        self.plugin._normalize_text(str(item.get("memory", "")))
                    )
                    if not mem_text:
                        continue
                    memory_type = str(item.get("memory_type", "fact"))
                    if memory_type == "style":
                        has_style = True
                    row_scope = str(rows[0].get("scope", "user"))
                    row_persona = str(rows[0].get("persona_id", ""))
                    new_id = self.plugin._insert_memory(
                        canonical_id=canonical_id,
                        adapter=str(rows[0]["source_adapter"]),
                        adapter_user=str(rows[0]["source_user_id"]),
                        memory=mem_text,
                        score=self.plugin._clamp01(item.get("score", 0.7)),
                        memory_type=memory_type,
                        importance=self.plugin._clamp01(item.get("importance", 0.6)),
                        confidence=self.plugin._clamp01(item.get("confidence", 0.7)),
                        source_channel="scheduled_distill",
                        scope=row_scope,
                        persona_id=row_persona,
                    )
                    if self.plugin._vec_available and new_id:
                        await self.plugin._upsert_vector(new_id, mem_text)
                    total_memories += 1

                # 风格蒸馏 v3: 产出 style 记忆后自动创建人格档案
                if has_style and self.plugin._cfg.enable_style_distill:
                    try:
                        source_adapter = str(rows[0].get("source_adapter", ""))
                        # 将 style 记忆写入临时档案（供用户合并/另存）
                        for item in valid_items:
                            if str(item.get("memory_type", "")) == "style":
                                mem_text = self.plugin._sanitize_text(
                                    self.plugin._normalize_text(str(item.get("memory", "")))
                                )
                                if mem_text:
                                    self.plugin._style_mgr.insert_temp_profile(
                                        source_user=canonical_id,
                                        source_adapter=source_adapter,
                                        memory_text=mem_text,
                                        memory_type="style",
                                        score=self.plugin._clamp01(item.get("score", 0.7)),
                                        importance=self.plugin._clamp01(item.get("importance", 0.6)),
                                        confidence=self.plugin._clamp01(item.get("confidence", 0.7)),
                                        conversation_context=str(rows[0].get("content", ""))[:500],
                                    )
                        self.plugin._style_mgr.auto_create_profile_if_ready(
                            canonical_id, source_adapter
                        )
                    except Exception as _te:
                        logger.warning("[tmemory] temp profile insert failed: %s", _te)

                self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                self.plugin._optimize_context(canonical_id)
                processed_users += 1
                self.plugin._user_last_distilled_ts[canonical_id] = now_ts
            except Exception as e:
                failed_users += 1
                errors.append(f"{canonical_id}: {type(e).__name__}: {e}")
                logger.warning(
                    "[tmemory] distill failed for user %s: %s", canonical_id, e
                )

        # 记录蒸馏历史
        duration = round(time.time() - t0, 2)
        cycle_tok_total = (
            cycle_tok_in + cycle_tok_out
            if cycle_tok_in >= 0 and cycle_tok_out >= 0
            else -1
        )
        self.plugin._record_distill_history(
            started_at=started_at,
            trigger=trigger,
            users_processed=processed_users,
            memories_created=total_memories,
            users_failed=failed_users,
            errors=errors,
            duration=duration,
            tokens_input=cycle_tok_in,
            tokens_output=cycle_tok_out,
            tokens_total=cycle_tok_total,
        )

        # 顺便执行记忆衰减
        self.plugin._decay_stale_memories()

        return processed_users, total_memories
    async def manual_purify_memories(
        self,
        event,
        canonical_id: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
    ) -> dict:
        rows = self.plugin._list_memories_for_purify(
            canonical_id, limit=limit, include_pinned=include_pinned
        )
        if not rows:
            return {"updates": 0, "adds": 0, "deletes": 0, "note": "no memories"}

        operations = await self.plugin._llm_purify_operations(
            event, rows, mode, extra_instruction
        )
        updates = operations.get("updates", []) if isinstance(operations, dict) else []
        adds = operations.get("adds", []) if isinstance(operations, dict) else []
        deletes = operations.get("deletes", []) if isinstance(operations, dict) else []
        note = str(operations.get("note", "")) if isinstance(operations, dict) else ""

        pinned_ids = {int(r["id"]) for r in rows if int(r["is_pinned"]) == 1}
        if not include_pinned:
            updates = [u for u in updates if int(u.get("id", 0)) not in pinned_ids]
            deletes = [d for d in deletes if int(d) not in pinned_ids]

        if dry_run:
            return {
                "updates": len(updates),
                "adds": len(adds),
                "deletes": len(deletes),
                "note": f"dry_run preview. {note}",
            }

        applied_updates = applied_adds = applied_deletes = 0

        for upd in updates:
            try:
                mem_id = int(upd.get("id", 0))
                if not mem_id:
                    continue
                memory = self.plugin._sanitize_text(
                    self.plugin._normalize_text(str(upd.get("memory", "")))
                )
                if not memory:
                    continue
                self.plugin._update_memory_full(
                    mem_id,
                    memory=memory,
                    memory_type=self.plugin._safe_memory_type(upd.get("memory_type", "fact")),
                    score=self.plugin._clamp01(upd.get("score", 0.7)),
                    importance=self.plugin._clamp01(upd.get("importance", 0.6)),
                    confidence=self.plugin._clamp01(upd.get("confidence", 0.7)),
                )
                if self.plugin._vec_available:
                    await self.plugin._upsert_vector(mem_id, memory)
                applied_updates += 1
            except Exception as e:
                logger.debug("[tmemory] apply update failed: %s", e)

        for add in adds:
            try:
                memory = self.plugin._sanitize_text(
                    self.plugin._normalize_text(str(add.get("memory", "")))
                )
                if not memory:
                    continue
                new_id = self.plugin._insert_memory(
                    canonical_id=canonical_id,
                    adapter="manual_purify",
                    adapter_user=canonical_id,
                    memory=memory,
                    score=self.plugin._clamp01(add.get("score", 0.7)),
                    memory_type=self.plugin._safe_memory_type(add.get("memory_type", "fact")),
                    importance=self.plugin._clamp01(add.get("importance", 0.6)),
                    confidence=self.plugin._clamp01(add.get("confidence", 0.7)),
                    source_channel="manual_purify",
                )
                if self.plugin._vec_available and new_id:
                    await self.plugin._upsert_vector(new_id, memory)
                applied_adds += 1
            except Exception as e:
                logger.debug("[tmemory] apply add failed: %s", e)

        for d in deletes:
            try:
                mem_id = int(d)
                if self.plugin._delete_memory(mem_id):
                    applied_deletes += 1
            except Exception as e:
                logger.debug("[tmemory] apply delete failed: %s", e)

        return {
            "updates": applied_updates,
            "adds": applied_adds,
            "deletes": applied_deletes,
            "note": note,
        }


# =========================================================================
# Standalone helpers (called as module-level functions, not MemoryOps methods)
# =========================================================================

def log_memory_event(
    plugin,
    canonical_user_id: str,
    event_type: str,
    payload: Dict[str, object],
    conn=None,
):
    """记录记忆相关事件到审计日志 memory_events。"""
    row = (
        canonical_user_id,
        event_type,
        json.dumps(payload, ensure_ascii=False),
        plugin._now(),
    )
    sql = (
        "INSERT INTO memory_events(canonical_user_id, event_type, payload_json, created_at)"
        " VALUES(?, ?, ?, ?)"
    )
    if conn is not None:
        conn.execute(sql, row)
    else:
        with plugin._db() as _conn:
            _conn.execute(sql, row)


def update_memory_full(
    plugin,
    memory_id: int,
    memory: str,
    memory_type: str,
    score: float,
    importance: float,
    confidence: float,
) -> None:
    """更新记忆全字段(memory/tokenized_memory/hash/type/scores)。"""
    now = plugin._now()
    mhash = hashlib.sha256(plugin._normalize_text(memory).encode("utf-8")).hexdigest()
    tokenized_memory = " ".join(jieba.cut_for_search(memory))
    with plugin._db() as conn:
        conn.execute(
            """
            UPDATE memories
            SET memory=?, tokenized_memory=?, memory_hash=?, memory_type=?, score=?, importance=?, confidence=?, updated_at=?
            WHERE id=?
            """,
            (memory, tokenized_memory, mhash, memory_type, score, importance, confidence, now, memory_id),
        )
