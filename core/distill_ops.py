"""MemoryOps distillation mixin.

Extracted from ``core/memory_ops.py`` per ADR-009 (module boundary split).
Physical move only — no behavior change. ``MemoryOps`` inherits this mixin so
the public object and method surface stay identical.
"""

import logging
from typing import Dict, List, Tuple

from .distill_errors import (
    DistillErrorCategory,
    DistillErrorRecord,
    classify_llm_error,
    errors_to_json,
    make_empty_result_record,
    make_fallback_record,
)
from .style_analyzer import get_style_analyzer

logger = logging.getLogger("astrbot:db.py")


class DistillOpsMixin:
    async def distill_rows_with_llm(
        self, rows: list
    ) -> Tuple[List[Dict[str, object]], int, int, List[DistillErrorRecord]]:
        """用 LLM 对一批对话行进行结构化蒸馏，失败时回退到规则蒸馏。

        B3 降本：相同 transcript + 模型命中 ``distill_prompt_cache`` 时直接复用产出，
        不再调用 LLM、token 记为 0（缓存只影响成本，不改变对外结果）。

        Returns:
            (memories, tokens_input, tokens_output, errors)
            errors 为结构化错误记录列表，替代旧版静默回退。
        """
        from . import prompt_cache as _prompt_cache
        from .attribution import filter_assistant_attributed

        username = str(rows[0].get("canonical_user_id", "")) if rows else ""
        user_lines: list[str] = []
        assistant_lines: list[str] = []
        user_transcript_lines: list[str] = []
        for row in rows:
            role = str(row["role"])
            content = str(row["content"])
            if role == "assistant":
                assistant_lines.append(f"- {content}")
            else:
                # user / summary / anything else is user-attributable material.
                label = "" if role == "user" else f"[{role}] "
                user_lines.append(f"- {label}{content}")
                user_transcript_lines.append(f"{role}: {content}")

        transcript = (
            "【用户发言（唯一可作为用户画像依据）】\n"
            + "\n".join(user_lines)
        )
        if assistant_lines:
            transcript += (
                "\n【助手发言（仅作上下文参考，禁止作为用户画像依据）】\n"
                + "\n".join(assistant_lines)
            )

        # 规则蒸馏回退只能使用用户发言，避免把助手内容写进用户记忆（TMEAAA-457）。
        user_transcript = "\n".join(user_transcript_lines)

        chat_provider_id = await self.plugin._distill_mgr.resolve_distill_provider_id(rows, self.plugin.context)
        chat_model_id = await self.plugin._distill_mgr.resolve_distill_model_id(rows)
        if not chat_provider_id:
            # 无法确定 provider 时，回退到规则蒸馏（结构化记录，不再静默）。
            fallback_err = make_fallback_record(
                pipeline="flat_distill",
                user_id=username,
                reason="无法确定 LLM provider，回退到规则蒸馏",
            )
            fallback_err.log()
            return (
                filter_assistant_attributed(
                    [
                        {
                            "memory": self.plugin._distill_mgr.distill_text(user_transcript),
                            "memory_type": "fact",
                            "importance": 0.55,
                            "confidence": 0.50,
                            "score": 0.60,
                        }
                    ],
                    rows,
                ),
                -1,
                -1,
                [fallback_err],
            )

        cache_enabled = bool(getattr(self.plugin._cfg, "distill_prompt_cache", True))
        cache_key = _prompt_cache.compute_cache_key(transcript, chat_model_id or chat_provider_id)
        if cache_enabled:
            cached_items = _prompt_cache.get_cached_distill_items(self.plugin, cache_key)
            if cached_items is not None:
                self.plugin._distill_prompt_cache_hits = (
                    getattr(self.plugin, "_distill_prompt_cache_hits", 0) + 1
                )
                return filter_assistant_attributed(cached_items, rows), 0, 0, []

        style_analysis = get_style_analyzer().analyze(rows)
        style_context = get_style_analyzer().build_style_context(style_analysis)
        prompt = self.plugin._distill_mgr.build_distill_prompt(transcript, style_context)

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
                parsed = filter_assistant_attributed(parsed, rows)
                if cache_enabled:
                    _prompt_cache.store_distill_items(
                        self.plugin, cache_key, transcript, parsed, chat_model_id
                    )
                return parsed, tok_in, tok_out, []
            else:
                # LLM 返回了内容但无法解析为有效记忆
                empty_err = make_empty_result_record(
                    pipeline="flat_distill",
                    user_id=username,
                )
                empty_err.log()
        except Exception as e:
            # 结构化分类替代裸 except Exception
            err = classify_llm_error(
                e,
                pipeline="flat_distill",
                user_id=username,
                context_message="LLM 蒸馏调用失败，回退到规则蒸馏",
            )
            err.log()

        # 规则蒸馏回退（新增：记录回退原因）
        fallback_err = make_fallback_record(
            pipeline="flat_distill",
            user_id=username,
            reason="LLM 蒸馏未产生有效结果，使用规则回退",
        )
        fallback_err.log()
        return (
            filter_assistant_attributed(
                [
                    {
                        "memory": self.plugin._distill_mgr.distill_text(user_transcript),
                        "memory_type": "fact",
                        "importance": 0.55,
                        "confidence": 0.50,
                        "score": 0.60,
                    }
                ],
                rows,
            ),
            -1,
            -1,
            [fallback_err],
        )

    def _classify_distill_rows(self, rows: list) -> Tuple[str, bool]:
        """规则分级门控判定。

        Returns:
            (decision, force_rule_only)
            - decision: ``"simple"``（规则处理，跳过 LLM）或 ``"complex"``（升级 LLM）
            - force_rule_only: 当日 token 预算已耗尽时为 True，复杂样本也暂不调用 LLM
        """
        from . import distill_validator as _distill_validator
        from .distill_gating import classify_batch

        cfg = self.plugin._cfg
        rule_gating = bool(getattr(cfg, "distill_rule_gating", False))
        budget_exceeded = _distill_validator.is_token_budget_exceeded(self.plugin)
        if not rule_gating and not budget_exceeded:
            return "complex", False
        decision = classify_batch(
            rows, min_chars=int(getattr(cfg, "distill_rule_gate_min_chars", 40) or 40)
        )
        return decision, budget_exceeded

    async def run_distill_cycle(
        self,
        force: bool = False,
        trigger: str = "manual"
    ) -> tuple[int, int, List[DistillErrorRecord]]:
        """执行一轮蒸馏，记录历史，单用户失败不中断整轮。

        Returns:
            (users_processed, memories_created, structured_errors)
        """
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
        all_errors: List[DistillErrorRecord] = []
        cycle_tok_in = -1
        cycle_tok_out = -1
        rule_gated_before = getattr(self.plugin, "_distill_rule_gated_batches", 0)
        cache_hits_before = getattr(self.plugin, "_distill_prompt_cache_hits", 0)

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

                # ── 规则分级门控（Plan TMEAAA-379 B3 / BC-4）──
                # 命中长期记忆信号的 complex 样本升级 LLM；无信号的 simple 样本由规则
                # 直接判定（零 LLM 调用）。预算耗尽时强制走规则分级，避免超支。
                decision, force_rule_only = self._classify_distill_rows(rows_for_llm)
                if decision == "simple":
                    self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                    self.plugin._distill_rule_gated_batches = (
                        getattr(self.plugin, "_distill_rule_gated_batches", 0) + 1
                    )
                    self.plugin._distill_rule_gated_rows = (
                        getattr(self.plugin, "_distill_rule_gated_rows", 0) + len(rows_for_llm)
                    )
                    processed_users += 1
                    self.plugin._user_last_distilled_ts[canonical_id] = now_ts
                    continue
                if force_rule_only:
                    # 复杂样本需要 LLM，但当日预算已耗尽：保留待预算恢复后重试，避免丢数据。
                    self.plugin._distill_rule_deferred_batches = (
                        getattr(self.plugin, "_distill_rule_deferred_batches", 0) + 1
                    )
                    continue

                llm_items, tok_in, tok_out, distill_errors = await self.plugin._distill_rows_with_llm(rows_for_llm)

                # 收集结构化错误
                if distill_errors:
                    all_errors.extend(distill_errors)
                    if any(
                        e.category in (DistillErrorCategory.PROVIDER_FAILURE, DistillErrorCategory.PARSE_FAILURE)
                        for e in distill_errors
                    ):
                        failed_users += 1

                # 累加 token 计数（-1 表示 provider 未返回，跳过累加）
                if tok_in >= 0:
                    cycle_tok_in = max(cycle_tok_in, 0) + tok_in
                if tok_out >= 0:
                    cycle_tok_out = max(cycle_tok_out, 0) + tok_out

                if not llm_items:
                    self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                    self.plugin._user_last_distilled_ts[canonical_id] = now_ts
                    processed_users += 1
                    continue

                valid_items = self.plugin._validate_distill_output(llm_items)
                if not valid_items:
                    from .distill_errors import make_validation_failure_record
                    vf_err = make_validation_failure_record(
                        pipeline="flat_distill",
                        user_id=canonical_id,
                        reason=f"LLM 返回 {len(llm_items)} 条，全部未通过校验",
                    )
                    vf_err.log()
                    all_errors.append(vf_err)
                    self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                    self.plugin._user_last_distilled_ts[canonical_id] = now_ts
                    processed_users += 1
                    continue

                for item in valid_items:
                    memory_type = str(item.get("memory_type", "fact"))
                    mem_text = self.plugin._sanitize_text(
                        self.plugin._normalize_text(str(item.get("memory", "")))
                    )
                    if not mem_text:
                        continue
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

                self.plugin._mark_rows_distilled([int(r["id"]) for r in rows])
                self.plugin._optimize_context(canonical_id)
                processed_users += 1
                self.plugin._user_last_distilled_ts[canonical_id] = now_ts
            except Exception as e:
                # 整用户处理异常（不属于 LLM 调用的异常）
                failed_users += 1
                user_err = classify_llm_error(
                    e,
                    pipeline="flat_distill",
                    user_id=canonical_id,
                    context_message="用户蒸馏流程异常",
                )
                user_err.log()
                all_errors.append(user_err)
                logger.warning(
                    "[tmemory] distill failed for user %s: %s", canonical_id, e
                )

        # 记录蒸馏历史（errors 字段序列化为 JSON）
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
            errors=errors_to_json(all_errors),
            duration=duration,
            tokens_input=cycle_tok_in,
            tokens_output=cycle_tok_out,
            tokens_total=cycle_tok_total,
            rule_gated_batches=getattr(self.plugin, "_distill_rule_gated_batches", 0) - rule_gated_before,
            prompt_cache_hits=getattr(self.plugin, "_distill_prompt_cache_hits", 0) - cache_hits_before,
        )

        # 顺便执行记忆衰减
        self.plugin._decay_stale_memories()

        return processed_users, total_memories, all_errors
