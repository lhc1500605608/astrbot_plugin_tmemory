"""AdminDistillMixin — 蒸馏状态查询、手动蒸馏触发和测试数据注入。

所有方法通过 ``self._db_mgr`` / ``self._cfg`` / ``self._plugin`` 访问共享状态。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from core.db import DatabaseManager
    from core.config import PluginConfig

logger = logging.getLogger("astrbot")


class AdminDistillMixin:
    """蒸馏与运维操作方法组。

    包含蒸馏状态查询、手动蒸馏触发、token 预算检查、测试对话注入和记忆精馏。
    """

    # =====================================================================
    # 只读查询
    # =====================================================================

    def get_pending(self) -> List[Dict[str, Any]]:
        """返回待蒸馏队列详情。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT canonical_user_id, COUNT(*) as cnt, "
                "MIN(created_at) as oldest, MAX(created_at) as newest "
                "FROM conversation_cache WHERE distilled = 0 "
                "GROUP BY canonical_user_id ORDER BY cnt DESC LIMIT 100"
            ).fetchall()
        return [
            {
                "user": str(r["canonical_user_id"]),
                "count": int(r["cnt"]),
                "oldest": str(r["oldest"]),
                "newest": str(r["newest"]),
            }
            for r in rows
        ]

    def get_distill_history(self, limit: int = 30) -> List[Dict]:
        """返回蒸馏历史记录。"""
        from .distill_validator import get_distill_history
        return get_distill_history(self._plugin, limit=limit)

    def get_distill_budget_info(self) -> Dict[str, Any]:
        """返回日 token 预算消耗信息与蒸馏降本统计（B3）。"""
        from .distill_validator import get_daily_token_usage
        from .prompt_cache import prompt_cache_stats
        budget = max(0, getattr(self._cfg, 'daily_token_budget', 0))
        used = get_daily_token_usage(self._plugin)
        return {
            "budget": budget,
            "used": used,
            "remaining": max(0, budget - used) if budget > 0 else -1,
            "pct": round(used / budget * 100, 1) if budget > 0 else 0.0,
            "unlimited": budget <= 0,
            "rule_gating_enabled": bool(getattr(self._cfg, "distill_rule_gating", False)),
            "rule_gated_batches": int(getattr(self._plugin, "_distill_rule_gated_batches", 0) or 0),
            "rule_gated_rows": int(getattr(self._plugin, "_distill_rule_gated_rows", 0) or 0),
            "rule_deferred_batches": int(getattr(self._plugin, "_distill_rule_deferred_batches", 0) or 0),
            "prompt_cache_enabled": bool(getattr(self._cfg, "distill_prompt_cache", True)),
            "prompt_cache_hits": int(getattr(self._plugin, "_distill_prompt_cache_hits", 0) or 0),
            "prompt_cache": prompt_cache_stats(self._plugin),
        }

    # =====================================================================
    # 写操作
    # =====================================================================

    def set_distill_pause(self, pause: bool) -> None:
        """暂停或恢复自动蒸馏。

        写入 ``_cfg.distill_pause``（distill worker 实际检查的位置）。
        """
        self._cfg.distill_pause = pause

    async def insert_test_conversation(
        self,
        user_id: str,
        role: str,
        content: str,
        source_adapter: str = "webui_test",
        source_user_id: str = "",
        unified_msg_origin: str = "",
        scope: str = "user",
        persona_id: str = "",
    ) -> Dict[str, Any]:
        """插入一条测试对话到 conversation_cache，用于 WebUI 模拟链路。

        复用 plugin._insert_conversation 路径，
        仅写入缓存，不触发蒸馏、不注入记忆。
        """
        if not user_id or not content or not role:
            return {"ok": False, "error": "user_id, role, content are required"}
        if role not in ("user", "assistant"):
            return {"ok": False, "error": "role must be user or assistant"}

        await self._plugin._insert_conversation(
            canonical_id=user_id,
            role=role,
            content=content,
            source_adapter=source_adapter or "webui_test",
            source_user_id=source_user_id or user_id,
            unified_msg_origin=unified_msg_origin or f"webui_test:{user_id}",
            scope=scope or "user",
            persona_id=persona_id or "",
        )
        return {"ok": True}

    async def trigger_distill(self) -> Dict[str, Any]:
        """手动触发蒸馏（含 token 预算检查）。"""
        from .distill_validator import is_token_budget_exceeded, get_daily_token_usage
        budget = getattr(self._cfg, 'daily_token_budget', 0)
        exceeded = budget > 0 and is_token_budget_exceeded(self._plugin)

        pending_before = self.count_pending_users()

        if exceeded:
            usage = get_daily_token_usage(self._plugin)
            return {
                "processed_users": 0,
                "total_memories": 0,
                "pending_users_before": pending_before,
                "errors": 0,
                "budget_warning": f"日 token 预算已超（{budget}），跳过蒸馏",
                "budget_used": usage,
            }

        from .memory_ops import MemoryOps
        processed_users, total_memories, errors = await MemoryOps(self._plugin).run_distill_cycle(
            force=True, trigger="manual_web"
        )
        return {
            "processed_users": processed_users,
            "total_memories": total_memories,
            "pending_users_before": pending_before,
            "errors": len(errors),
        }

    async def refine_memories(
        self,
        user: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
        unified_msg_origin: str = "",
    ) -> Dict[str, Any]:
        """LLM 记忆精馏/提纯。"""
        mode = mode or self._cfg.manual_purify_default_mode or "both"
        limit = max(1, min(200, limit or self._cfg.manual_purify_default_limit or 20))

        class _Evt:
            pass
        _Evt.unified_msg_origin = unified_msg_origin  # type: ignore[attr-defined]

        from .memory_ops import MemoryOps
        return await MemoryOps(self._plugin).manual_purify_memories(
            event=_Evt(),  # type: ignore[arg-type]
            canonical_id=user,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra_instruction,
        )

    async def redistill_memory(
        self,
        user: str,
        memory_id: int,
        unified_msg_origin: str = "",
    ) -> dict[str, Any]:
        """对单条记忆重新蒸馏（失败不降级）。

        成功时就地替换该条记忆文本（保留 id、更新 ``updated_at`` 与
        ``source_channel='redistill'``）；失败时返回 ``error`` + ``category``，
        绝不生成规则兜底记忆。

        Raises:
            LookupError: 记忆不存在或不属于该用户（bridge 映射为 404）。
        """
        from .attribution import filter_assistant_attributed
        from .distill_errors import exception_code
        from .style_analyzer import get_style_analyzer

        row = self._fetch_memory_by_id(user, memory_id)
        if not row:
            raise LookupError("memory not found")

        rows, evidence_source = self._collect_redistill_rows(
            user, row, unified_msg_origin
        )

        plugin = self._plugin
        mgr = plugin._distill_mgr
        provider_id = await mgr.resolve_distill_provider_id(rows, plugin.context)
        if not provider_id:
            return {
                "ok": False,
                "error": "无法确定用于重新蒸馏的 LLM provider",
                "category": "no_provider",
            }
        model_id = await mgr.resolve_distill_model_id(rows)

        transcript, _user_transcript = self._build_redistill_transcript(rows)
        style_analyzer = get_style_analyzer()
        style_context = style_analyzer.build_style_context(style_analyzer.analyze(rows))
        prompt = mgr.build_distill_prompt(transcript, style_context)

        try:
            llm_kwargs: dict[str, Any] = {
                "chat_provider_id": provider_id,
                "prompt": prompt,
            }
            if model_id:
                llm_kwargs["model_id"] = model_id
            llm_resp = await plugin.context.llm_generate(**llm_kwargs)
            completion = plugin._strip_think_tags(
                plugin._normalize_text(getattr(llm_resp, "completion_text", "") or "")
            )
            parsed = plugin._parse_llm_json_memories(completion)
        except Exception as exc:  # noqa: BLE001 - 失败不降级，返回结构化错误
            logger.warning("[tmemory] redistill llm failed id=%s: %s", memory_id, exc)
            return {
                "ok": False,
                "error": f"LLM 调用失败: {type(exc).__name__}",
                "category": "llm_error",
                "code": exception_code(exc),
            }

        if not parsed:
            return {
                "ok": False,
                "error": "LLM 未返回可解析的记忆",
                "category": "unparseable",
            }

        parsed = filter_assistant_attributed(parsed, rows)
        valid_items = plugin._validate_distill_output(parsed, _user_transcript)
        if not valid_items:
            return {
                "ok": False,
                "error": "LLM 输出未通过校验",
                "category": "unparseable",
            }

        # 单条重新蒸馏语义：产出多条时取最高分一条替换，其余忽略。
        best = max(valid_items, key=lambda it: float(it.get("score", 0.0) or 0.0))
        new_text = plugin._sanitize_text(
            plugin._normalize_text(str(best.get("memory", "")))
        )
        if not new_text:
            return {
                "ok": False,
                "error": "蒸馏结果为空",
                "category": "unparseable",
            }

        self._update_memory_text(memory_id, new_text)
        with self._db() as conn:
            conn.execute(
                "UPDATE memories SET source_channel='redistill' WHERE id=?",
                (memory_id,),
            )

        if getattr(plugin, "_vec_available", False):
            try:
                await plugin._upsert_vector(memory_id, new_text)
            except Exception as exc:  # noqa: BLE001 - 向量失败仅告警
                logger.warning(
                    "[tmemory] redistill vector upsert failed id=%s: %s", memory_id, exc
                )

        from .memory_ops import log_memory_event
        log_memory_event(
            plugin,
            canonical_user_id=user,
            event_type="redistill",
            payload={
                "memory_id": memory_id,
                "evidence": evidence_source,
                "old_memory": str(row["memory"]),
            },
        )
        return {
            "ok": True,
            "memory_id": memory_id,
            "memory": new_text,
            "memory_type": str(best.get("memory_type", row["memory_type"])),
            "source_channel": "redistill",
            "evidence": evidence_source,
        }

    def _collect_redistill_rows(
        self,
        user: str,
        memory_row: dict[str, Any],
        unified_msg_origin: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """取重新蒸馏的来源证据。

        优先级：该记忆 episode 关联的原文 → 该用户最近的用户发言 →
        对现有记忆文本做一次 LLM 归一化重写（无任何缓存时）。
        """
        episode_id = int(memory_row.get("episode_id", 0) or 0)
        episode_rows: list[dict[str, Any]] = []
        if episode_id:
            with self._db() as conn:
                episode_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT cc.role AS role, cc.content AS content, "
                        "cc.unified_msg_origin AS unified_msg_origin "
                        "FROM episode_sources es "
                        "JOIN conversation_cache cc ON cc.id = es.conversation_cache_id "
                        "WHERE es.episode_id=? AND cc.canonical_user_id=? "
                        "ORDER BY cc.id ASC",
                        (episode_id, user),
                    ).fetchall()
                ]
        if episode_rows:
            return self._tag_redistill_origin(episode_rows, unified_msg_origin), "episode"

        with self._db() as conn:
            recent_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT role, content, unified_msg_origin "
                    "FROM conversation_cache "
                    "WHERE canonical_user_id=? AND role='user' AND archived_at='' "
                    "ORDER BY id DESC LIMIT 40",
                    (user,),
                ).fetchall()
            ]
        if recent_rows:
            return self._tag_redistill_origin(recent_rows, unified_msg_origin), "recent_cache"

        return (
            [
                {
                    "role": "user",
                    "content": str(memory_row.get("memory", "")),
                    "unified_msg_origin": unified_msg_origin,
                }
            ],
            "memory_text",
        )

    @staticmethod
    def _tag_redistill_origin(
        rows: list[dict[str, Any]], unified_msg_origin: str
    ) -> list[dict[str, Any]]:
        for r in rows:
            if not r.get("unified_msg_origin"):
                r["unified_msg_origin"] = unified_msg_origin
        return rows

    @staticmethod
    def _build_redistill_transcript(rows: list[dict[str, Any]]) -> tuple[str, str]:
        user_lines: list[str] = []
        assistant_lines: list[str] = []
        user_transcript_lines: list[str] = []
        for r in rows:
            role = str(r.get("role", "user"))
            content = str(r.get("content", ""))
            if role == "assistant":
                assistant_lines.append(f"- {content}")
            else:
                label = "" if role == "user" else f"[{role}] "
                user_lines.append(f"- {label}{content}")
                user_transcript_lines.append(f"{role}: {content}")
        transcript = "【用户发言（唯一可作为用户画像依据）】\n" + "\n".join(user_lines)
        if assistant_lines:
            transcript += (
                "\n【助手发言（仅作上下文参考，禁止作为用户画像依据）】\n"
                + "\n".join(assistant_lines)
            )
        return transcript, "\n".join(user_transcript_lines)
