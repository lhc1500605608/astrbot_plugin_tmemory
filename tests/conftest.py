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

    # Minimal stubs for astrbot.core.agent.message (TextPart / ContentPart / Message)
    core_mod = types.ModuleType("astrbot.core")
    agent_mod = types.ModuleType("astrbot.core.agent")
    message_mod = types.ModuleType("astrbot.core.agent.message")

    class _StubContentPart:
        pass

    class _StubTextPart:
        """AstrBot 4.23.2 契约：``TextPart`` 没有 ``mark_as_temp``。

        ``mark_as_temp`` 是 4.28 才引入的 API。stub 必须与真实契约一致，
        否则 ``adapters/version.py`` 的能力探测会在 stub 环境误报
        ``has_mark_as_temp=True``，掩盖 4.16/4.23.2 的 system_prompt 降级路径
        （TMEAAA-361 AC3）。需要验证 4.28 路径的测试请声明
        ``mark_as_temp_support`` fixture 显式安装。
        """

        def __init__(self, text: str):
            self.text = text
            self.type = "text"
            self._temp = False

    class _StubTextPartWithTemp(_StubTextPart):
        """AstrBot >=4.28 契约：``TextPart.mark_as_temp`` 可用。"""

        def mark_as_temp(self):
            self._temp = True
            return self

    class _StubMessage:
        _no_save: bool = False

    message_mod.ContentPart = _StubContentPart
    message_mod.TextPart = _StubTextPart
    message_mod.Message = _StubMessage
    # 供 mark_as_temp_support fixture 引用（4.28 形状，默认不安装）。
    message_mod.stub_text_part_with_temp = _StubTextPartWithTemp

    astrbot_mod.api = api_mod
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.provider"] = provider_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.agent"] = agent_mod
    sys.modules["astrbot.core.agent.message"] = message_mod


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
    return _load_package_module("web.legacy_server", "web/legacy_server.py")


@pytest.fixture(scope="session")
def bridge_module():
    _install_astrbot_stubs()
    return _load_package_module("web.bridge", "web/bridge.py")


@pytest.fixture()
def mark_as_temp_support(plugin_module):
    """安装 AstrBot >=4.28 形状的 ``TextPart`` 并重置能力缓存。

    默认 stub 模拟 4.16/4.23.2（无 ``mark_as_temp``）。只有显式声明本 fixture
    的测试才依赖 4.28 契约，默认环境始终走真实降级路径。
    """
    from astrbot_plugin_tmemory.adapters import version

    message_mod = sys.modules["astrbot.core.agent.message"]
    with_temp = message_mod.stub_text_part_with_temp
    original = message_mod.TextPart
    message_mod.TextPart = with_temp
    version.reset_capability_cache()
    try:
        yield with_temp
    finally:
        message_mod.TextPart = original
        version.reset_capability_cache()


@pytest.fixture()
def plugin(tmp_path, monkeypatch, plugin_module):
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={})
    instance._init_db()
    instance._migrate_schema()
    yield instance
    instance._close_db()


@pytest.fixture()
def admin_svc(plugin):
    """AdminService instance for testing profile API methods."""
    from astrbot_plugin_tmemory.core.admin_service import AdminService
    return AdminService(plugin)


@pytest.fixture()
def seeded_profile_items(plugin, admin_svc):
    """Seed 4 profile items with different facet_types for a test user."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps
    ops = ProfileItemOps(plugin)
    ops.upsert_profile_item("test-user", "preference", "偏好Python", "用户偏好使用Python", 0.9, 0.8)
    ops.upsert_profile_item("test-user", "preference", "偏好VS Code", "用户偏好使用VS Code", 0.85, 0.7)
    ops.upsert_profile_item("test-user", "fact", "职业", "用户是一名软件工程师", 0.95, 0.9)
    ops.upsert_profile_item("test-user", "fact", "已归档事实", "users obsolete fact", 0.5, 0.4)
    # Archive the last one manually
    with plugin._db() as conn:
        items = conn.execute(
            "SELECT id FROM profile_items WHERE canonical_user_id='test-user' ORDER BY id"
        ).fetchall()
    ops.archive_item(items[-1]["id"])
    return items  # return ids


@pytest.fixture()
def seeded_profile_items_with_evidence(plugin, admin_svc):
    """Seed a profile item with evidence from conversation_cache."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps
    ops = ProfileItemOps(plugin)
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            ("evidence-user", "user", "I like Python", now),
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    item_id = ops.upsert_profile_item("evidence-user", "preference", "偏好Python", "用户偏好Python", 0.9, 0.8)
    ops.add_evidence(item_id, "evidence-user", [cid], "user", "conversation")
    return item_id
