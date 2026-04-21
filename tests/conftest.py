import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    provider_mod = types.ModuleType("astrbot.api.provider")
    star_mod = types.ModuleType("astrbot.api.star")

    class _DummyFilter:
        class EventMessageType:
            ALL = "all"

        class PermissionType:
            ADMIN = "admin"

        def __getattr__(self, _name):
            def decorator_factory(*_args, **_kwargs):
                def decorator(func):
                    return func

                return decorator

            return decorator_factory

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

    api_mod.logger = logging.getLogger("astrbot")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _DummyFilter()
    provider_mod.LLMResponse = LLMResponse
    provider_mod.ProviderRequest = ProviderRequest
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    astrbot_mod.api = api_mod
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.provider"] = provider_mod
    sys.modules["astrbot.api.star"] = star_mod


def _load_package_module(module_name: str, file_name: str):
    if "astrbot_plugin_tmemory" not in sys.modules:
        package = types.ModuleType("astrbot_plugin_tmemory")
        package.__path__ = [str(ROOT)]
        sys.modules["astrbot_plugin_tmemory"] = package

    full_name = f"astrbot_plugin_tmemory.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(full_name, ROOT / file_name)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin_module():
    _install_astrbot_stubs()
    _load_package_module("hybrid_search", "hybrid_search.py")
    return _load_package_module("main", "main.py")


@pytest.fixture(scope="session")
def web_module():
    _install_astrbot_stubs()
    return _load_package_module("web_server", "web_server.py")


@pytest.fixture()
def plugin(tmp_path, monkeypatch, plugin_module):
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={})
    instance._init_db()
    instance._migrate_schema()
    yield instance
    instance._close_db()
