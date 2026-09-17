"""Send port — 主动消息发送（Proactive）的上游封装 + 能力探测。

Plan TMEAAA-379 T2 (B2) / 破坏性变更 BC-3：新增主动推送能力，默认关闭
（``proactive_enabled=false``）。本模块是领域侧唯一的主动发送出口。

上游路径 / 引入版本 / 降级策略：
- ``astrbot.core.star.context.Context.send_message(unified_msg_origin, message_chain)``
  （公共 API，4.16–4.28 稳定）
- 消息部件 ``astrbot.api.message_components.Plain(text=...)``（公共，4.16–4.28 稳定）
- 降级策略：Context 缺失 / 无 ``send_message`` / 调用抛异常 → 返回 ``False``，
  调用方禁用主动推送并告警，绝不影响插件启动与被动能力。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("astrbot")

SEND_MESSAGE_MIN_VERSION = "4.16"
SEND_FALLBACK_HINT = (
    "当前 AstrBot 不支持主动消息推送（Context.send_message 缺失），"
    "已禁用主动记忆功能并降级为被动模式。"
)


@dataclass(frozen=True)
class SendCapability:
    """一次探测得到的主动推送能力快照。"""

    available: bool
    reason: str
    context_present: bool
    method: str
    min_version: str = SEND_MESSAGE_MIN_VERSION

    def to_dict(self) -> Dict[str, object]:
        return {
            "available": self.available,
            "reason": self.reason,
            "context_present": self.context_present,
            "method": self.method,
            "send_min_version": self.min_version,
        }


def _resolve_context(source: Any) -> Optional[Any]:
    """从 plugin / Context 解析 Context（失败返回 None，不抛异常）。"""
    if source is None:
        return None
    try:
        ctx = getattr(source, "context", None)
        if ctx is not None:
            return ctx
    except Exception:
        return None
    return source


def probe_send_capability(source: Any) -> SendCapability:
    """探测主动推送能力（不抛异常）。

    上游路径: ``Context.send_message``（公共）
    引入版本: 4.16–4.28 保留
    降级策略: 缺失返回 available=False + 具体 reason，调用方禁用并告警
    """
    ctx = _resolve_context(source)
    if ctx is None:
        return SendCapability(False, "context_unavailable", False, "")
    if not callable(getattr(ctx, "send_message", None)):
        return SendCapability(
            False, "context_send_message_missing", True, ""
        )
    return SendCapability(True, "", True, "send_message")


def build_message_chain(parts: Sequence[Any]) -> Any:
    """把领域侧 ``[{"type": "plain", "text": ...}]`` 转成上游消息链。

    上游路径: ``astrbot.api.message_components.Plain``（公共）
    降级策略: import 失败时退回纯文本列表（仍可被 send_message 接受或触发父层降级）
    """
    texts: List[str] = []
    for part in parts or []:
        if isinstance(part, dict):
            text = part.get("text")
        else:
            text = part
        if text:
            texts.append(str(text))
    try:
        from astrbot.api.message_components import Plain  # type: ignore

        components = [Plain(text=t) for t in texts]
    except Exception:
        return texts
    try:
        from astrbot.api.message_components import MessageChain  # type: ignore

        return MessageChain(components)
    except Exception:
        return components


async def send_message(source: Any, umo: str, chain: Sequence[Any]) -> bool:
    """主动发送消息链；失败返回 ``False``（不抛异常）。

    上游路径: ``Context.send_message(unified_msg_origin, message_chain)``（公共）
    引入版本: 4.16–4.28 保留
    降级策略: 能力缺失 / 参数为空 / 调用异常 → ``False``，调用方记录审计并跳过。
    """
    ctx = _resolve_context(source)
    if ctx is None or not umo:
        return False
    fn = getattr(ctx, "send_message", None)
    if not callable(fn):
        return False

    payload: Any = chain
    if isinstance(chain, (list, tuple)) and chain and isinstance(chain[0], dict):
        payload = build_message_chain(chain)
    try:
        await fn(umo, payload)
        return True
    except Exception as e:
        logger.warning("[tmemory] proactive send_message failed: %s", e)
        return False


__all__ = [
    "SEND_FALLBACK_HINT",
    "SEND_MESSAGE_MIN_VERSION",
    "SendCapability",
    "build_message_chain",
    "probe_send_capability",
    "send_message",
]
