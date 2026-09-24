"""adapters — AstrBot 上游边界层（唯一允许 import astrbot* 的领域侧目录）。

分层规则（Plan TMEAAA-354 §3.1，可由 tests/test_boundaries.py 校验）：
- R1: ``core/**`` 禁止 ``import astrbot*``（``TYPE_CHECKING`` 下类型标注除外）。
- R2: 仅 ``main.py`` / ``adapters/**`` / ``web/**`` / ``web_handlers.py``
      允许 import astrbot*；本包是领域逻辑访问上游的唯一入口。
- R3: 每处内部 API 访问标注「上游路径 + 引入版本 + 降级策略」。

facade re-export：core/web 可直接 ``from ..adapters import get_adapter_name``。
"""

from __future__ import annotations

from . import config, conversation, event, llm, provider, send, version

__all__ = [
    "config",
    "conversation",
    "event",
    "llm",
    "provider",
    "send",
    "version",
    "probe_send_capability",
    "send_message",
    "resolve_embedding_provider",
    "resolve_rerank_provider",
    "get_astrbot_data_path",
    "get_current_persona_id",
    "get_current_conversation_id",
    "register_session_deleted",
    "inject_extra_user_temp",
    "get_unified_msg_origin",
    "get_platform_str",
    "get_adapter_name",
    "get_adapter_user_id",
    "get_adapter_user_id_from_umo",
    "get_identity_adapter_name",
    "get_memory_scope",
    "is_group_event",
    "get_current_persona",
]


resolve_embedding_provider = provider.resolve_embedding_provider
resolve_rerank_provider = provider.resolve_rerank_provider
get_astrbot_data_path = config.get_astrbot_data_path
get_current_persona_id = conversation.get_current_persona_id
get_current_conversation_id = conversation.get_current_conversation_id
register_session_deleted = conversation.register_session_deleted
inject_extra_user_temp = llm.inject_extra_user_temp
get_unified_msg_origin = event.get_unified_msg_origin
get_platform_str = event.get_platform_str
get_adapter_name = event.get_adapter_name
get_adapter_user_id = event.get_adapter_user_id
get_adapter_user_id_from_umo = event.get_adapter_user_id_from_umo
get_identity_adapter_name = event.get_identity_adapter_name
get_memory_scope = event.get_memory_scope
is_group_event = event.is_group_event
get_current_persona = event.get_current_persona
probe_send_capability = send.probe_send_capability
send_message = send.send_message
