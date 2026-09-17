"""Event port — 仅此模块内省 AstrMessageEvent 的内部/鸭子类型字段。

上游路径: astrbot.api.event.AstrMessageEvent（公共） + astrbot.core.platform.MessageType、
astrbot.core.platform.platform_metadata.PlatformMetadata（内部）。
引入版本: PlatformMetadata / MessageType 路径在 4.16–4.28 未变；MessageType.FRIEND_MESSAGE 恒存在。
降级策略: 内部 import 失败时退回鸭子类型判定（get_group_id / str(val)），绝不抛异常。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("astrbot")


def get_unified_msg_origin(event: Any) -> str:
    """事件 UMO（``platform:message_type:session_id``）。

    上游路径: AstrMessageEvent.unified_msg_origin（公共属性）
    引入版本: 4.16 前即存在，4.28.1 保留
    降级策略: getattr 兜底，任何异常返回 ""
    """
    try:
        return str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:
        return ""


def get_platform_str(val: Any) -> str:
    """把平台字段安全转成字符串，避免 PlatformMetadata dataclass repr 泄漏为适配器名。

    上游路径: astrbot.core.platform.platform_metadata.PlatformMetadata（内部）
    引入版本: 4.16–4.28 路径与内容未变
    降级策略: import/异常时退回 ``str(val)``
    """
    try:
        from astrbot.core.platform.platform_metadata import PlatformMetadata  # type: ignore

        if isinstance(val, PlatformMetadata):
            return val.id or val.name
    except Exception:
        pass
    return str(val)


def get_adapter_name(event: Any) -> str:
    """适配器名（先方法后属性，混入/命令绑定用）。

    上游路径: AstrMessageEvent.get_platform_name / get_adapter_name / get_client_name（公共）
    引入版本: 4.16–4.28 可用
    降级策略: 依次 getattr；平台字段经 get_platform_str；全部缺失返回 "unknown_adapter"
    """
    for name in ("get_platform_name", "get_adapter_name", "get_client_name"):
        fn = getattr(event, name, None)
        if callable(fn):
            try:
                val = fn()
                if val:
                    return str(val)
            except Exception:
                pass

    for attr in ("platform_name", "adapter_name", "adapter", "platform"):
        val = getattr(event, attr, None)
        if val:
            return get_platform_str(val)

    return "unknown_adapter"


def get_identity_adapter_name(event: Any) -> str:
    """身份绑定用适配器名（只读属性，保持历史行为）。

    上游路径: AstrMessageEvent.adapter_name / platform / platform_id（公共属性）
    引入版本: 4.16–4.28 未变
    降级策略: 依次 getattr；全部缺失返回 "unknown"
    """
    for attr in ("adapter_name", "platform", "platform_id"):
        val = getattr(event, attr, None)
        if val:
            return get_platform_str(val)
    return "unknown"


def get_adapter_user_id(event: Any) -> str:
    """适配器侧用户 ID（多字段兜底）。

    上游路径: AstrMessageEvent.get_sender_id / get_user_id / get_sender_name（公共）
    引入版本: 4.16–4.28 可用（4.28 起内省方法改 getattr 兜底）
    降级策略: 依次尝试；全部缺失返回 "unknown_user"
    """
    for name in ("get_sender_id", "get_user_id"):
        fn = getattr(event, name, None)
        if callable(fn):
            try:
                val = fn()
                if val:
                    return str(val)
            except Exception:
                pass

    sender_name = getattr(event, "get_sender_name", None)
    if callable(sender_name):
        try:
            val = sender_name()
            if val:
                return str(val)
        except Exception:
            pass

    return "unknown_user"


def _message_type() -> Optional[Any]:
    """MessageType 枚举，不可用时返回 None。

    上游路径: astrbot.core.platform.MessageType（内部）
    引入版本: 4.16–4.28 未变
    降级策略: import/异常返回 None，调用方退回 get_group_id 判定
    """
    try:
        from astrbot.core.platform import MessageType  # type: ignore

        return MessageType
    except Exception:
        return None


def get_memory_scope(event: Any, memory_scope: str) -> str:
    """记忆作用域：``user`` 或 ``private`` / ``group:{gid}``。

    上游路径: MessageType.FRIEND_MESSAGE + get_message_type / get_group_id（公共）
    引入版本: 4.16–4.28 未变
    降级策略: 任一异常返回 "private"（保守起见不跨群共享）
    """
    if memory_scope != "session":
        return "user"
    try:
        mtype = _message_type()
        if mtype is not None and event.get_message_type() == mtype.FRIEND_MESSAGE:
            return "private"
        gid = event.get_group_id()
        return f"group:{gid}" if gid else "private"
    except Exception:
        return "private"


def is_group_event(event: Any) -> bool:
    """是否群消息。

    上游路径: MessageType.FRIEND_MESSAGE + get_message_type / get_group_id（公共）
    引入版本: 4.16–4.28 未变
    降级策略: MessageType 不可用或异常时退回 ``bool(get_group_id())``
    """
    try:
        mtype = _message_type()
        if mtype is None:
            raise RuntimeError("MessageType unavailable")
        return event.get_message_type() != mtype.FRIEND_MESSAGE
    except Exception:
        try:
            return bool(event.get_group_id())
        except Exception:
            return False


def get_current_persona(event: Any) -> str:
    """同步读取当前会话 persona_id（最后兜底路径）。

    上游路径: AstrMessageEvent._extras["conversation"]（私有字段）→ 公共 conversation 属性
    引入版本: 4.16–4.28 均存在 _extras，但属内部实现，需集中此处以便上游漂移时单点修复
    降级策略: 私有字段缺失退回 getattr(event, "conversation")，再失败返回 ""
    """
    try:
        extras = getattr(event, "_extras", {}) or {}
        conv = extras.get("conversation") or getattr(event, "conversation", None)
        if conv:
            persona = getattr(conv, "persona_id", None)
            if persona:
                return str(persona)
    except Exception:
        pass
    return ""
