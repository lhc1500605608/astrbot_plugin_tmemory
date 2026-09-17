"""Version/capability port — AstrBot 版本与能力探测的唯一入口。

Plan TMEAAA-354 Phase 1：用一个集中式能力快照替代散落的 ``getattr``/duck-typing
探测，使「上游能力缺失」成为可观测事件而非静默降级。

上游路径 / 引入版本 / 降级策略：
- ``astrbot.__version__`` / ``astrbot.core.config.default.VERSION``（4.16–4.28 稳定）
  → 探测失败返回 ``""``，仅用于日志与 UI 展示，不影响能力判定。
- ``astrbot.core.agent.message.TextPart.mark_as_temp``（4.28 新增）
  → 缺失时 ``extra_user_temp`` 不可用，调用方回退 ``system_prompt`` 并告警。
- ``astrbot.core.star.context.Context.register_web_api``（4.28 新增 plugin pages）
  → 缺失时插件页桥接不可用（Phase 3 使用）。
- ``astrbot.api.web.{json_response,error_response,request}``（plugin pages 运行时 SDK）
  → 与 ``register_web_api`` 一起构成 plugin pages 契约。仅探测 ``register_web_api``
    会在 AstrBot 4.23.2 误判（该符号已存在，但 bridge handler 请求时 import
    ``astrbot.api.web`` 失败 → 全路由 500）。
- ``astrbot.core.conversation_mgr.ConversationManager.register_on_session_deleted``
  （4.28 新增 session hook）→ 缺失时会话删除观测不可用（Phase 4 使用）。
- ``astrbot.api.event.filter.on_agent_begin`` / ``on_agent_done``（4.28 新增
  agent hooks）→ 缺失时 Agent 运行观测不可用（Phase 4 使用）。

能力判定以**运行时特性探测**为准（不依赖版本号比较），结果缓存；测试可调用
``reset_capability_cache()`` 清空缓存。
"""

from __future__ import annotations

import functools
import importlib
import logging
from dataclasses import dataclass
from typing import Any, Dict

logger = logging.getLogger("astrbot")

# 各能力首次引入的 AstrBot 版本（用于日志 / UI 提示文案）。
MARK_AS_TEMP_MIN_VERSION = "4.28"
PLUGIN_PAGES_MIN_VERSION = "4.28"
PLUGIN_PAGES_WEB_MODULE = "astrbot.api.web"
SESSION_HOOKS_MIN_VERSION = "4.28"
AGENT_HOOKS_MIN_VERSION = "4.28"

_EXTRA_USER_TEMP_HINT = (
    "记忆注入位置 extra_user_temp 需要 AstrBot >= {min_version}"
    "（当前版本缺少 TextPart.mark_as_temp），将回退到 system_prompt 注入。"
).format(min_version=MARK_AS_TEMP_MIN_VERSION)


def get_astrbot_version() -> str:
    """返回 AstrBot 版本字符串；探测失败返回空串。

    上游路径: ``astrbot.__version__`` / ``astrbot.core.config.default.VERSION``
              / ``importlib.metadata.version("astrbot")``
    引入版本: 4.16–4.28 均存在
    降级策略: 任一失败继续尝试下一来源，全部失败返回 ``""``（不抛异常）
    """
    for module_name, attr in (
        ("astrbot", "__version__"),
        ("astrbot.core.config", "VERSION"),
        ("astrbot.core.config", "ASTRBOT_VERSION"),
        ("astrbot.core.config.default", "VERSION"),
        ("astrbot.core.config.default", "ASTRBOT_VERSION"),
    ):
        try:
            value = getattr(importlib.import_module(module_name), attr, None)
        except Exception:
            continue
        if value:
            return str(value)

    try:
        from importlib.metadata import version as _dist_version  # type: ignore

        for dist_name in ("astrbot", "AstrBot"):
            try:
                return str(_dist_version(dist_name))
            except Exception:
                continue
    except Exception:
        pass
    return ""


def _probe_mark_as_temp() -> bool:
    """上游路径: astrbot.core.agent.message.TextPart.mark_as_temp（4.28 新增）。"""
    try:
        from astrbot.core.agent.message import TextPart  # type: ignore

        return callable(getattr(TextPart, "mark_as_temp", None))
    except Exception:
        return False


def plugin_web_request_contract_ok(request_proxy: Any) -> bool:
    """校验 ``astrbot.api.web.request`` 代理的 duck-typed 契约。

    真实实现 ``PluginRequestProxy`` 在 handler 外访问属性会抛 ``RuntimeError``
    （contextvar 未绑定）。这种「未绑定请求上下文」不是能力缺失，因此捕获后
    退化为类型级校验，避免在插件加载期误判 plugin pages 不可用。
    """
    if request_proxy is None:
        return False
    try:
        if not hasattr(request_proxy, "query"):
            return False
        return callable(getattr(request_proxy, "json", None))
    except RuntimeError:
        proxy_cls = type(request_proxy)
        return hasattr(proxy_cls, "query") and callable(
            getattr(proxy_cls, "json", None)
        )


def _probe_plugin_web_module() -> bool:
    """``astrbot.api.web`` 运行时契约是否可用（bridge handler 的硬依赖）。

    上游路径: ``astrbot.api.web.{json_response,error_response,request}``
    引入版本: plugin pages 同批次（>=4.28；4.23.2 无此模块）
    降级策略: 缺失/契约不全返回 ``False``（不抛异常）
    """
    try:
        web_mod = importlib.import_module("astrbot.api.web")
    except Exception:
        return False

    if not callable(getattr(web_mod, "json_response", None)):
        return False
    if not callable(getattr(web_mod, "error_response", None)):
        return False

    return plugin_web_request_contract_ok(getattr(web_mod, "request", None))


def _probe_plugin_pages() -> bool:
    """plugin pages 契约：``Context.register_web_api`` + ``astrbot.api.web``。

    AstrBot 4.23.2 同时缺少 ``astrbot.api.web``，但 ``Context.register_web_api``
    已存在；只探测前者会注册 bridge 并在请求时 ``ModuleNotFoundError`` → 500。
    """
    try:
        from astrbot.core.star.context import Context  # type: ignore

        if not callable(getattr(Context, "register_web_api", None)):
            return False
    except Exception:
        return False
    return _probe_plugin_web_module()


def _probe_session_hooks() -> bool:
    """上游路径: astrbot.core.conversation_mgr.ConversationManager.

    register_on_session_deleted（4.28 新增）。
    """
    try:
        from astrbot.core.conversation_mgr import (  # type: ignore
            ConversationManager,
        )

        return callable(getattr(ConversationManager, "register_on_session_deleted", None))
    except Exception:
        return False


def _probe_send_message() -> bool:
    """上游路径: astrbot.core.star.context.Context.send_message（公共，4.16+）。

    主动推送（Proactive，Plan TMEAAA-379 B2）的类级能力探测；运行时仍需结合
    实际 Context 实例（见 ``adapters/send.probe_send_capability``）。
    """
    try:
        from astrbot.core.star.context import Context  # type: ignore

        return callable(getattr(Context, "send_message", None))
    except Exception:
        return False


def _probe_agent_hooks() -> bool:
    """上游路径: astrbot.api.event.filter.{on_agent_begin,on_agent_done}（4.28 新增）。"""
    try:
        from astrbot.api.event import filter as _filter  # type: ignore

        return callable(getattr(_filter, "on_agent_begin", None)) and callable(
            getattr(_filter, "on_agent_done", None)
        )
    except Exception:
        return False


@dataclass(frozen=True)
class Capabilities:
    """一次探测得到的 AstrBot 能力快照（不可变，可安全缓存）。"""

    version: str
    has_mark_as_temp: bool
    has_plugin_pages: bool
    has_session_hooks: bool
    has_agent_hooks: bool
    has_send_message: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "has_mark_as_temp": self.has_mark_as_temp,
            "has_plugin_pages": self.has_plugin_pages,
            "has_session_hooks": self.has_session_hooks,
            "has_agent_hooks": self.has_agent_hooks,
            "has_send_message": self.has_send_message,
            "extra_user_temp_available": self.has_mark_as_temp,
            "extra_user_temp_min_version": MARK_AS_TEMP_MIN_VERSION,
            "plugin_pages_min_version": PLUGIN_PAGES_MIN_VERSION,
            "session_hooks_min_version": SESSION_HOOKS_MIN_VERSION,
            "agent_hooks_min_version": AGENT_HOOKS_MIN_VERSION,
        }


@functools.lru_cache(maxsize=1)
def _detect_capabilities() -> Capabilities:
    return Capabilities(
        version=get_astrbot_version(),
        has_mark_as_temp=_probe_mark_as_temp(),
        has_plugin_pages=_probe_plugin_pages(),
        has_session_hooks=_probe_session_hooks(),
        has_agent_hooks=_probe_agent_hooks(),
        has_send_message=_probe_send_message(),
    )


def reset_capability_cache() -> None:
    """清空能力探测缓存（测试或多版本运行时切换时使用）。"""
    _detect_capabilities.cache_clear()


def get_capabilities(refresh: bool = False) -> Capabilities:
    """返回能力快照；``refresh=True`` 时强制重新探测。"""
    if refresh:
        reset_capability_cache()
    return _detect_capabilities()


def has_mark_as_temp() -> bool:
    """``TextPart.mark_as_temp`` 是否可用（``extra_user_temp`` 注入的前提）。"""
    return get_capabilities().has_mark_as_temp


def has_plugin_pages() -> bool:
    """插件页（Dashboard 托管页面）桥接能力是否可用。"""
    return get_capabilities().has_plugin_pages


def has_session_hooks() -> bool:
    """会话生命周期钩子（``register_on_session_deleted``）是否可用。"""
    return get_capabilities().has_session_hooks


def has_agent_hooks() -> bool:
    """Agent 运行钩子（``on_agent_begin`` / ``on_agent_done``）是否可用。"""
    return get_capabilities().has_agent_hooks


def has_send_message() -> bool:
    """主动推送能力（``Context.send_message``）是否可用。"""
    return get_capabilities().has_send_message


def extra_user_temp_available() -> bool:
    """``extra_user_temp`` 注入位置在当前 AstrBot 上是否可用。"""
    return has_mark_as_temp()


def extra_user_temp_hint() -> str:
    """面向 UI / 日志的用户可读提示。"""
    return _EXTRA_USER_TEMP_HINT


def log_capabilities(refresh: bool = True) -> Capabilities:
    """探测并记录一行能力快照，返回快照供调用方复用。"""
    caps = get_capabilities(refresh=refresh)
    logger.info(
        "[tmemory] AstrBot %s capabilities: mark_as_temp=%s plugin_pages=%s"
        " session_hooks=%s agent_hooks=%s send_message=%s",
        caps.version or "unknown",
        caps.has_mark_as_temp,
        caps.has_plugin_pages,
        caps.has_session_hooks,
        caps.has_agent_hooks,
        caps.has_send_message,
    )
    return caps


__all__ = [
    "Capabilities",
    "AGENT_HOOKS_MIN_VERSION",
    "MARK_AS_TEMP_MIN_VERSION",
    "PLUGIN_PAGES_MIN_VERSION",
    "PLUGIN_PAGES_WEB_MODULE",
    "SESSION_HOOKS_MIN_VERSION",
    "extra_user_temp_available",
    "extra_user_temp_hint",
    "get_astrbot_version",
    "get_capabilities",
    "has_agent_hooks",
    "has_mark_as_temp",
    "has_plugin_pages",
    "has_send_message",
    "has_session_hooks",
    "log_capabilities",
    "plugin_web_request_contract_ok",
    "reset_capability_cache",
]
