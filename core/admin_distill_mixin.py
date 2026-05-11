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
        """返回日 token 预算消耗信息。"""
        from .distill_validator import get_daily_token_usage
        budget = max(0, getattr(self._cfg, 'daily_token_budget', 0))
        used = get_daily_token_usage(self._plugin)
        return {
            "budget": budget,
            "used": used,
            "remaining": max(0, budget - used) if budget > 0 else -1,
            "pct": round(used / budget * 100, 1) if budget > 0 else 0.0,
            "unlimited": budget <= 0,
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
