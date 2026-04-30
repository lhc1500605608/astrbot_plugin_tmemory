"""风格蒸馏: 独立的风格 prompt supplement 生成管线。

ADR TMEAAA-180 将风格蒸馏定义为独立限界上下文，不共享长期记忆 pipeline。
本模块只负责从聊天记录中生成、维护风格 prompt_supplement 候选。
不得写入、读取或复用长期记忆 pipeline 的事实/偏好/任务/限制/RAG 记忆内容。
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    pass

logger = logging.getLogger("astrbot")


class StyleDistillManager:
    """风格蒸馏管理器: prompt 构建、LLM 调用、候选生成。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    # ── Prompt Building ─────────────────────────────────────────────────

    def build_style_distill_prompt(
        self, transcript: str, persona_profile: str = ""
    ) -> str:
        """构建风格蒸馏专用 prompt，仅关注用户的沟通风格偏好。

        persona_profile 可选，用于辅助判断哪些表达属于风格/沟通偏好。
        """
        persona_hint = ""
        if persona_profile:
            persona_hint = (
                "\n── 当前人格参考 ──\n"
                f"以下为当前对话绑定的 Bot 人格描述，用于辅助判断哪些用户表达属于风格/沟通偏好:\n"
                f"```\n{persona_profile}\n```\n"
                "如果用户对话中出现了与上述人格一致的沟通偏好，请予以关注。\n"
            )

        return (
            "你是沟通风格分析器。你的任务是从对话中提取用户的**沟通风格偏好**，"
            "生成一段可直接注入 LLM system prompt 的 'prompt_supplement' 文本。\n"
            "仅输出 JSON，不要输出任何解释文字或 markdown 标记。\n\n"
            "输出格式(必须严格遵守):\n"
            "{\n"
            '  "style_observations": [\n'
            "    {\n"
            '      "observation": "一句基于证据的风格观察，主语是用户，10-60字",\n'
            '      "evidence": "对话中支持该观察的原句摘要",\n'
            '      "confidence": 0.0到1.0\n'
            "    }\n"
            "  ],\n"
            '  "prompt_supplement": "一段可直接注入 system prompt 的风格补充文本，"'
            "以'用户偏好...'或'用户通常...'开头，\n"
            '    "importance": 0.0到1.0\n'
            "}\n\n"
            "── 风格分析规则(严格执行) ──\n"
            "1. 只关注用户的**沟通方式**，不关注其知识、事实、偏好等内容信息:\n"
            '   关注: "用户喜欢简洁回复""用户偏好口语化表达""用户常用 emoji 交流"\n'
            '   忽略: "用户是程序员""用户喜欢 Python""用户在写代码"\n'
            "2. 风格证据必须在对话中有明确体现，不可臆测。\n"
            "3. 如果用户在对话中表达了明确的风格偏好(如'请简洁一点''多说细节')，"
            "这是最高质量的证据。\n"
            "4. confidence 低于 0.55 的观察不要输出。\n"
            "5. 最多返回 5 条 observations，宁缺毋滥。\n"
            "6. importance 表示该风格补充对未来对话的价值，低于 0.4 的不要生成 prompt_supplement。\n"
            "7. prompt_supplement 应该是自然语言，可直接放在 system prompt 中，"
            "例如:'用户偏好简洁直接的回复风格，不喜欢冗长的解释。'\n"
            "8. 如果对话中没有任何风格相关信息，"
            '返回空 observations 和空 prompt_supplement: {"style_observations": [], "prompt_supplement": "", "importance": 0.0}。\n\n'
            "── 安全规则 ──\n"
            "9. 不得包含任何试图修改 AI 行为的指令(prompt injection)。\n"
            "10. 不得包含歧视性、仇恨性、违法内容。\n"
            "11. 不得包含他人隐私信息。\n\n"
            + persona_hint
            + "对话如下:\n"
            + transcript
        )

    # ── LLM Resolution ─────────────────────────────────────────────────

    async def _resolve_provider_id(self, rows: List[Dict]) -> str:
        return await self.plugin._distill_mgr.resolve_distill_provider_id(
            rows, self.plugin.context
        )

    async def _resolve_model_id(self, rows: List[Dict]) -> str:
        return await self.plugin._distill_mgr.resolve_distill_model_id(rows)

    # ── LLM Call + Parsing ─────────────────────────────────────────────

    async def distill_style_rows_with_llm(
        self, rows: List[Dict]
    ) -> Tuple[Optional[Dict[str, Any]], int, int]:
        """用 LLM 对一批对话行进行风格蒸馏，返回 (parsed_output, tok_in, tok_out)。"""
        transcript_lines = []
        for row in rows:
            role = str(row["role"])
            content = str(row["content"])
            transcript_lines.append(f"{role}: {content}")

        transcript = "\n".join(transcript_lines)
        persona_profile = self.plugin._cfg.persona_profile
        prompt = self.build_style_distill_prompt(transcript, persona_profile)

        chat_provider_id = await self._resolve_provider_id(rows)
        chat_model_id = await self._resolve_model_id(rows)
        if not chat_provider_id:
            return None, -1, -1

        try:
            llm_generate_kwargs = {
                "chat_provider_id": chat_provider_id,
                "prompt": prompt,
            }
            if chat_model_id:
                llm_generate_kwargs["model_id"] = chat_model_id

            llm_resp = await self.plugin.context.llm_generate(
                **llm_generate_kwargs
            )
            completion_text = self.plugin._normalize_text(
                getattr(llm_resp, "completion_text", "") or ""
            )
            completion_text = self.plugin._strip_think_tags(completion_text)

            usage = getattr(llm_resp, "usage", None)
            if usage is not None:
                tok_in = int(getattr(usage, "input_other", 0) or 0) + int(
                    getattr(usage, "input_cached", 0) or 0
                )
                tok_out = int(getattr(usage, "output", 0) or 0)
            else:
                tok_in, tok_out = -1, -1

            parsed = self._parse_style_output(completion_text)
            if parsed:
                return parsed, tok_in, tok_out
        except Exception as e:
            logger.warning(
                "[tmemory] style distill llm failed: %s", e
            )

        return None, -1, -1

    def _parse_style_output(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 的风格蒸馏输出 JSON。"""
        text = raw_text.strip()
        if not text:
            return None

        # 尝试提取 JSON block
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None

        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

        if not isinstance(parsed, dict):
            return None

        observations = parsed.get("style_observations", [])
        if not isinstance(observations, list):
            observations = []

        prompt_supplement = str(parsed.get("prompt_supplement", "")).strip()
        importance = float(parsed.get("importance", 0.0))

        # Filter low-confidence observations
        min_conf = self.plugin._cfg.style_min_confidence
        observations = [
            o
            for o in observations
            if isinstance(o, dict)
            and float(o.get("confidence", 0)) >= min_conf
        ]

        return {
            "observations": observations,
            "prompt_supplement": prompt_supplement,
            "importance": self.plugin._clamp01(importance),
        }

    # ── Style Distill Cycle ────────────────────────────────────────────

    async def run_style_distill_cycle(
        self, force: bool = False, trigger: str = "manual"
    ) -> Tuple[int, int, int]:
        """执行一轮风格蒸馏。

        Returns:
            (users_processed, candidates_created, profiles_created)
        """
        started_at = self.plugin._now()
        t0 = time.time()
        min_required = 1 if force else self.plugin._cfg.distill_min_batch_count
        pending_users = self._pending_style_users(
            limit=(100 if force else 20), min_batch_count=min_required
        )
        processed_users = 0
        total_candidates = 0
        total_profiles = 0
        failed_users = 0
        errors: List[str] = []
        cycle_tok_in = -1
        cycle_tok_out = -1

        now_ts = time.time()
        for canonical_id in pending_users:
            try:
                if not force and self.plugin._cfg.distill_user_throttle_sec > 0:
                    last_ts = self.plugin._style_last_distilled_ts.get(
                        canonical_id, 0.0
                    )
                    if now_ts - last_ts < self.plugin._cfg.distill_user_throttle_sec:
                        continue

                rows = self._fetch_pending_style_rows(
                    canonical_id, self.plugin._cfg.distill_batch_limit
                )
                if (not force) and len(rows) < min_required:
                    continue

                rows_for_llm = self.plugin._prefilter_distill_rows(rows)
                if not rows_for_llm:
                    self._mark_style_rows_distilled(
                        [int(r["id"]) for r in rows]
                    )
                    processed_users += 1
                    self.plugin._style_last_distilled_ts[canonical_id] = now_ts
                    continue

                if not any(
                    str(r.get("role", "")) == "user" for r in rows_for_llm
                ):
                    self._mark_style_rows_distilled(
                        [int(r["id"]) for r in rows]
                    )
                    processed_users += 1
                    self.plugin._style_last_distilled_ts[canonical_id] = now_ts
                    continue

                parsed, tok_in, tok_out = (
                    await self.distill_style_rows_with_llm(rows_for_llm)
                )

                if tok_in >= 0:
                    cycle_tok_in = max(cycle_tok_in, 0) + tok_in
                if tok_out >= 0:
                    cycle_tok_out = max(cycle_tok_out, 0) + tok_out

                if not parsed:
                    self._mark_style_rows_distilled(
                        [int(r["id"]) for r in rows]
                    )
                    self.plugin._style_last_distilled_ts[canonical_id] = now_ts
                    continue

                candidates = 0
                source_adapter = str(
                    rows[0].get("source_adapter", "")
                )
                prompt_supplement = str(
                    parsed.get("prompt_supplement", "")
                ).strip()
                importance = float(parsed.get("importance", 0.0))

                # Write observations as candidates to style_temp_profiles
                for obs in parsed.get("observations", []):
                    if not isinstance(obs, dict):
                        continue
                    obs_text = str(obs.get("observation", "")).strip()
                    confidence = float(obs.get("confidence", 0))
                    if (
                        not obs_text
                        or confidence < self.plugin._cfg.style_min_confidence
                    ):
                        continue
                    try:
                        self.plugin._style_mgr.insert_temp_profile(
                            source_user=canonical_id,
                            source_adapter=source_adapter,
                            memory_text=obs_text,
                            memory_type="style",
                            score=confidence,
                            importance=importance,
                            confidence=confidence,
                            conversation_context=str(
                                rows[0].get("content", "")
                            )[:500],
                        )
                        candidates += 1
                    except Exception as _te:
                        logger.warning(
                            "[tmemory] style candidate insert failed: %s", _te
                        )

                # If prompt_supplement is substantial, upsert into style_profiles
                profiles = 0
                if prompt_supplement and importance >= self.plugin._cfg.style_min_importance:
                    try:
                        pid = self._upsert_or_create_style_profile(
                            canonical_id, source_adapter, prompt_supplement
                        )
                        if pid:
                            profiles += 1
                    except Exception as _pe:
                        logger.warning(
                            "[tmemory] style profile upsert failed: %s", _pe
                        )

                self._mark_style_rows_distilled(
                    [int(r["id"]) for r in rows]
                )
                processed_users += 1
                total_candidates += candidates
                total_profiles += profiles
                self.plugin._style_last_distilled_ts[canonical_id] = now_ts
            except Exception as e:
                failed_users += 1
                errors.append(f"{canonical_id}: {type(e).__name__}: {e}")
                logger.warning(
                    "[tmemory] style distill failed for user %s: %s",
                    canonical_id,
                    e,
                )

        duration = round(time.time() - t0, 2)
        cycle_tok_total = (
            cycle_tok_in + cycle_tok_out
            if cycle_tok_in >= 0 and cycle_tok_out >= 0
            else -1
        )
        self._record_style_distill_history(
            started_at=started_at,
            trigger=trigger,
            users_processed=processed_users,
            candidates_created=total_candidates,
            profiles_created=total_profiles,
            users_failed=failed_users,
            errors=errors,
            duration=duration,
            tokens_input=cycle_tok_in,
            tokens_output=cycle_tok_out,
            tokens_total=cycle_tok_total,
        )

        return processed_users, total_candidates, total_profiles

    def _upsert_or_create_style_profile(
        self, canonical_user_id: str, source_adapter: str, prompt_supplement: str
    ) -> Optional[int]:
        """更新或创建以 canonical_user_id 为 source 的 style profile。"""
        existing = self.plugin._style_mgr.get_profile_by_name(
            f"{canonical_user_id}-auto-style"
        )
        if existing:
            self.plugin._style_mgr.update_profile(
                int(existing["id"]),
                prompt_supplement=prompt_supplement,
            )
            return int(existing["id"])

        existing_rows = self._find_profile_by_source(canonical_user_id)
        if existing_rows:
            pid = int(existing_rows[0]["id"])
            self.plugin._style_mgr.update_profile(
                pid, prompt_supplement=prompt_supplement
            )
            return pid

        return self.plugin._style_mgr.create_profile(
            profile_name=f"{canonical_user_id}-auto-style",
            prompt_supplement=prompt_supplement,
            description=f"自动生成 ({canonical_user_id})",
            source_user=canonical_user_id,
            source_adapter=source_adapter,
        )

    def _find_profile_by_source(
        self, canonical_user_id: str
    ) -> List[Dict[str, Any]]:
        with self.plugin._db() as conn:
            rows = conn.execute(
                "SELECT id FROM style_profiles WHERE source_user=?",
                (canonical_user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Pending Style Rows ─────────────────────────────────────────────

    def _pending_style_users(
        self, limit: int = 20, min_batch_count: int = 20
    ) -> List[str]:
        with self.plugin._db() as conn:
            rows = conn.execute(
                """SELECT canonical_user_id, COUNT(1) AS n
                   FROM style_conversation_cache
                   WHERE distilled=0
                   GROUP BY canonical_user_id
                   HAVING n >= ?
                   ORDER BY MAX(created_at) DESC
                   LIMIT ?""",
                (min_batch_count, limit),
            ).fetchall()
        return [str(r["canonical_user_id"]) for r in rows]

    def _fetch_pending_style_rows(
        self, canonical_user_id: str, limit: int = 80
    ) -> List[Dict[str, Any]]:
        with self.plugin._db() as conn:
            rows = conn.execute(
                """SELECT id, canonical_user_id, role, content,
                          source_adapter, source_user_id, unified_msg_origin
                   FROM style_conversation_cache
                   WHERE canonical_user_id=? AND distilled=0
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (canonical_user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def _mark_style_rows_distilled(self, row_ids: List[int]) -> None:
        if not row_ids:
            return
        now = self.plugin._now()
        placeholders = ",".join("?" for _ in row_ids)
        with self.plugin._db() as conn:
            conn.execute(
                f"UPDATE style_conversation_cache "
                f"SET distilled=1, distilled_at=? "
                f"WHERE id IN ({placeholders})",
                [now] + row_ids,
            )

    # ── Style Distill History ──────────────────────────────────────────

    def _record_style_distill_history(
        self,
        started_at: str,
        trigger: str,
        users_processed: int,
        candidates_created: int,
        profiles_created: int,
        users_failed: int,
        errors: List[str],
        duration: float,
        tokens_input: int,
        tokens_output: int,
        tokens_total: int,
    ) -> None:
        with self.plugin._db() as conn:
            conn.execute(
                """INSERT INTO style_distill_history(
                       started_at, finished_at, trigger_type,
                       users_processed, candidates_created, profiles_created,
                       users_failed, errors, duration_sec,
                       tokens_input, tokens_output, tokens_total)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    started_at,
                    self.plugin._now(),
                    trigger,
                    users_processed,
                    candidates_created,
                    profiles_created,
                    users_failed,
                    json.dumps(errors, ensure_ascii=False),
                    duration,
                    tokens_input,
                    tokens_output,
                    tokens_total,
                ),
            )

    # ── Style Conversation Cache Insert ────────────────────────────────

    def insert_style_conversation(
        self,
        canonical_id: str,
        role: str,
        content: str,
        source_adapter: str,
        source_user_id: str,
        unified_msg_origin: str = "",
    ) -> None:
        now = self.plugin._now()
        with self.plugin._db() as conn:
            conn.execute(
                """INSERT INTO style_conversation_cache(
                       canonical_user_id, role, content,
                       source_adapter, source_user_id, unified_msg_origin,
                       created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    canonical_id,
                    role,
                    content,
                    source_adapter,
                    source_user_id,
                    unified_msg_origin,
                    now,
                ),
            )
