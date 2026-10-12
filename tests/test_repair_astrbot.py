import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_model_override_uses_framework_parameter(plugin):
    from astrbot_plugin_tmemory.adapters.llm import generate
    plugin.context = SimpleNamespace(llm_generate=AsyncMock(return_value="response"))
    assert await generate(plugin, chat_provider_id="provider", prompt="test", model_id="chosen") == "response"
    assert plugin.context.llm_generate.await_args.kwargs == dict(chat_provider_id="provider", prompt="test", model="chosen")


@pytest.mark.asyncio
async def test_send_false_is_failure(plugin):
    from astrbot_plugin_tmemory.adapters.send import send_message
    ctx = SimpleNamespace(send_message=AsyncMock(return_value=False))
    assert await send_message(ctx, "bot:FriendMessage:1", []) is False


@pytest.mark.asyncio
async def test_reload_callbacks_do_not_retain_or_call_old_plugins(plugin, plugin_module):
    import gc
    import weakref
    callbacks = []
    ctx = SimpleNamespace(conversation_manager=SimpleNamespace(register_on_session_deleted=callbacks.append))
    refs = []
    for _ in range(3):
        instance = plugin_module.TMemoryPlugin(context=ctx, config={})
        instance._on_session_deleted = AsyncMock()
        assert instance._register_session_lifecycle_hooks()
        ref = weakref.ref(instance)
        await instance.terminate()
        for cb in callbacks:
            await cb("bot:FriendMessage:1")
        instance._on_session_deleted.assert_not_awaited()
        refs.append(ref)
        del instance
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_message_chain_uses_event_api(plugin, monkeypatch):
    from astrbot_plugin_tmemory.adapters.send import build_message_chain
    import types
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = lambda text: ("plain", text)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)
    event = sys.modules["astrbot.api.event"]
    class Chain:
        def __init__(self, components): self.chain = components
    monkeypatch.setattr(event, "MessageChain", Chain, raising=False)
    result = build_message_chain([{"type": "plain", "text": "hello"}])
    assert isinstance(result, Chain)
    assert result.chain == [("plain", "hello")]
