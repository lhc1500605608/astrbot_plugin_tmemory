"""Conversation port — 会话/persona 解析的上游封装。"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("astrbot")


async def get_current_conversation_id(context: Any, umo: str) -> str:
    """经 ConversationManager 解析当前会话的对话 ID。

    上游路径: Context.conversation_manager.get_curr_conversation_id
              （ConversationManager，公共 API）
    引入版本: 4.16–4.28 保留
    降级策略: 任一环节失败返回 ""，调用方跳过轮转检测
    """
    if not umo:
        return ""
    try:
        conv_mgr = getattr(context, "conversation_manager", None)
        if not conv_mgr:
            return ""
        cid = await conv_mgr.get_curr_conversation_id(umo)
        return str(cid) if cid else ""
    except Exception:
        return ""


async def get_current_persona_id(context: Any, umo: str) -> str:
    """经 ConversationManager 解析当前会话 persona_id。

    上游路径: Context.conversation_manager.{get_curr_conversation_id, get_conversation}
              （ConversationManager，公共 API）
    引入版本: 4.16–4.28 保留；4.28 新增 register_on_session_deleted（本函数不依赖）
    降级策略: 任一环节失败返回 ""，由调用方退回 event._extras（adapters/event.get_current_persona）
    """
    if not umo:
        return ""
    try:
        conv_mgr = getattr(context, "conversation_manager", None)
        if not conv_mgr:
            return ""
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return ""
        conv = await conv_mgr.get_conversation(umo, cid)
        if conv and getattr(conv, "persona_id", None):
            return str(conv.persona_id)
    except Exception:
        pass
    return ""


def register_session_deleted(
    context: Any, callback: Callable[[str], Awaitable[None]]
) -> bool:
    """在 ConversationManager 上注册会话删除回调。

    上游路径: Context.conversation_manager.
              register_on_session_deleted(callback)（ConversationManager，公共 API）
    引入版本: 4.28 新增（callback 接收 unified_msg_origin，会话被删除时触发）
    降级策略: 能力缺失 / 上下文为空 / 注册异常 → 返回 False，调用方跳过
              （不阻断插件启动；模块级探测见 adapters/version.has_session_hooks）
    """
    try:
        conv_mgr = getattr(context, "conversation_manager", None)
        if conv_mgr is None:
            return False
        register = getattr(conv_mgr, "register_on_session_deleted", None)
        if not callable(register):
            return False
        register(callback)
        return True
    except Exception:
        return False
