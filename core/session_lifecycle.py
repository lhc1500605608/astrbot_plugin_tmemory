"""会话生命周期（/new /reset）语义 — Plan TMEAAA-354 Phase 4a。

上游钩子：
- ``ConversationManager.register_on_session_deleted(callback)``（4.28 新增）：
  回调参数为 ``unified_msg_origin``，在整会话删除时触发（经 adapters 接入）。
- ``ConversationManager.get_curr_conversation_id``（4.16+）：用于检测 ``/new``
  ``/reset`` 造成的对话 ID 变化（上游这两条命令走 ``new_conversation``，
  不触发 session-deleted 回调）。
- ``filter.on_agent_begin`` / ``filter.on_agent_done``（4.28 新增）：Agent 运行起止观测。

三态策略 ``session_reset_policy``：
- ``keep``（默认）：仅记录日志，会话缓存与长期记忆全部保留（向后兼容）。
- ``archive``：将本会话的 ``conversation_cache`` 行标记 ``archived_at``（软归档）；
  工作上下文注入不再召回，证据链与蒸馏资格保留。
- ``clear``：删除本会话的 ``conversation_cache`` 行；被 ``profile_item_evidence`` /
  ``episode_sources`` 引用的行保留，避免破坏证据外键。

本模块属 ``core/**``，不得 import ``astrbot*``；上游访问收敛在
``adapters/conversation.py``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from ..adapters import conversation as _conversation_adapter

logger = logging.getLogger("astrbot")

SESSION_RESET_POLICIES = ("keep", "archive", "clear")
DEFAULT_SESSION_RESET_POLICY = "keep"


class SessionLifecycleRuntimeMixin:
    """会话删除 / Agent 运行观测的运行时逻辑。"""

    # ── 配置归一 ────────────────────────────────────────────────────────

    def _normalized_session_reset_policy(self) -> str:
        policy = str(
            getattr(self._cfg, "session_reset_policy", DEFAULT_SESSION_RESET_POLICY)
            or DEFAULT_SESSION_RESET_POLICY
        ).strip().lower()
        return (
            policy if policy in SESSION_RESET_POLICIES else DEFAULT_SESSION_RESET_POLICY
        )

    # ── 钩子注册 ────────────────────────────────────────────────────────

    def _register_session_lifecycle_hooks(self) -> bool:
        """注册会话删除钩子；能力缺失 / 异常时返回 False，不阻断启动。"""
        try:
            registered = _conversation_adapter.register_session_deleted(
                getattr(self, "context", None), self._on_session_deleted
            )
        except Exception as e:
            logger.warning("[tmemory] 会话删除钩子注册异常: %s", e)
            return False

        if registered:
            logger.info(
                "[tmemory] 会话删除钩子已注册（session_reset_policy=%s）",
                self._normalized_session_reset_policy(),
            )
        else:
            logger.info(
                "[tmemory] 会话删除钩子不可用（需 AstrBot >= 4.28 的 "
                "ConversationManager.register_on_session_deleted），整会话删除观测跳过；"
                "/new /reset 仍由对话 ID 变化检测覆盖。"
            )
        return registered

    # ── /new /reset 轮转检测 ────────────────────────────────────────────

    async def _maybe_handle_session_rotation(self, event: Any) -> bool:
        """检测 /new /reset 造成的对话轮转并应用 reset 策略。

        上游 4.28.1 中 ``/new`` / ``/reset`` 走
        ``ConversationManager.new_conversation``（**不**触发
        ``register_on_session_deleted``），因此以「当前对话 ID 变化」作为轮转信号，
        兼容 4.16–4.28 全部版本。首次见到某 UMO 时只建立基线，不误判。
        """
        umo = self._safe_get_unified_msg_origin(event)
        if not umo:
            return False

        try:
            cid = await _conversation_adapter.get_current_conversation_id(
                getattr(self, "context", None), umo
            )
        except Exception:
            return False
        if not cid:
            return False

        sessions = getattr(self, "_session_conv_ids", None)
        if sessions is None:
            sessions = {}
            self._session_conv_ids = sessions
        prev = sessions.get(umo)
        sessions[umo] = cid

        if prev and prev != cid:
            logger.info(
                "[tmemory] 检测到会话轮转（/new /reset）umo=%s cid=%s→%s",
                umo,
                prev[:8],
                cid[:8],
            )
            await self._on_session_deleted(umo)
            return True
        return False

    # ── 会话删除处理 ────────────────────────────────────────────────────

    async def _on_session_deleted(self, unified_msg_origin: str) -> None:
        umo = str(unified_msg_origin or "")
        policy = self._normalized_session_reset_policy()
        try:
            stats = await asyncio.to_thread(self._apply_session_reset_sync, umo, policy)
        except Exception as e:
            logger.warning("[tmemory] 会话删除策略执行失败 umo=%s: %s", umo, e)
            return
        logger.info(
            "[tmemory] 会话删除观测 umo=%s policy=%s archived=%s cleared=%s",
            umo or "(empty)",
            policy,
            stats.get("archived", 0),
            stats.get("cleared", 0),
        )

    def _apply_session_reset_sync(self, umo: str, policy: str) -> Dict[str, int]:
        stats: Dict[str, int] = {"archived": 0, "cleared": 0}
        if not umo or policy == "keep":
            return stats

        with self._db() as conn:
            if policy == "archive":
                cur = conn.execute(
                    "UPDATE conversation_cache SET archived_at=? "
                    "WHERE session_key=? AND archived_at=''",
                    (self._now(), umo),
                )
                stats["archived"] = int(cur.rowcount or 0)
            elif policy == "clear":
                cur = conn.execute(
                    """
                    DELETE FROM conversation_cache
                    WHERE session_key=?
                      AND id NOT IN (
                          SELECT conversation_cache_id FROM profile_item_evidence
                      )
                      AND id NOT IN (
                          SELECT conversation_cache_id FROM episode_sources
                      )
                    """,
                    (umo,),
                )
                stats["cleared"] = int(cur.rowcount or 0)
        return stats

    # ── Agent 运行观测（on_agent_begin / on_agent_done，AstrBot >= 4.28）────

    async def _handle_on_agent_begin(self, event: Any, run_context: Any = None) -> None:
        umo = self._safe_get_unified_msg_origin(event)
        self._agent_begin_count = getattr(self, "_agent_begin_count", 0) + 1
        logger.debug(
            "[tmemory] on_agent_begin umo=%s total=%s",
            umo or "(empty)",
            self._agent_begin_count,
        )

    async def _handle_on_agent_done(
        self, event: Any, run_context: Any = None, response: Any = None
    ) -> None:
        umo = self._safe_get_unified_msg_origin(event)
        self._agent_done_count = getattr(self, "_agent_done_count", 0) + 1
        logger.debug(
            "[tmemory] on_agent_done umo=%s total=%s",
            umo or "(empty)",
            self._agent_done_count,
        )


__all__ = [
    "DEFAULT_SESSION_RESET_POLICY",
    "SESSION_RESET_POLICIES",
    "SessionLifecycleRuntimeMixin",
]
