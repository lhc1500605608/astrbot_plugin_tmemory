import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _identity_decorator(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


def _install_astrbot_stubs() -> None:
    if "astrbot.api" in sys.modules:
        return

    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    event = ModuleType("astrbot.api.event")
    provider = ModuleType("astrbot.api.provider")
    star = ModuleType("astrbot.api.star")
    core = ModuleType("astrbot.core")
    platform = ModuleType("astrbot.core.platform")

    class _Filter:
        class PermissionType:
            ADMIN = "admin"

        class EventMessageType:
            ALL = "all"

        permission_type = staticmethod(_identity_decorator)
        command = staticmethod(_identity_decorator)
        event_message_type = staticmethod(_identity_decorator)
        on_llm_response = staticmethod(_identity_decorator)
        on_llm_request = staticmethod(_identity_decorator)

    class AstrMessageEvent:
        pass

    class LLMResponse:
        pass

    class ProviderRequest:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    class MessageType:
        FRIEND_MESSAGE = "friend"

    api.logger = logging.getLogger("astrbot-test")
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = _Filter
    provider.LLMResponse = LLMResponse
    provider.ProviderRequest = ProviderRequest
    star.Context = Context
    star.Star = Star
    star.register = register
    platform.MessageType = MessageType

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.provider"] = provider
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.platform"] = platform


_install_astrbot_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def plugin_module():
    return importlib.import_module("astrbot_plugin_tmemory.main")


@pytest.fixture
def web_module():
    return importlib.import_module("astrbot_plugin_tmemory.web_server")


@pytest.fixture
def plugin(tmp_path, plugin_module):
    plugin = plugin_module.TMemoryPlugin(SimpleNamespace(), config={"webui_enabled": False})
    plugin.db_path = str(tmp_path / "tmemory.db")
    plugin._conn = None
    plugin._init_db()
    plugin._migrate_schema()
    yield plugin
    plugin._close_db()
