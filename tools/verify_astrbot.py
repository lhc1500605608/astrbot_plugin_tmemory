"""Real AstrBot loader acceptance runner (no Context substitutes, no IM network sends).

Run separately from pytest's stub environment with AstrBot dependencies installed.
python tools/verify_astrbot.py --astrbot-source /checkout/AstrBot --workdir /tmp/qa
"""
from __future__ import annotations
import argparse
import asyncio
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--astrbot-source", type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    work = args.workdir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(work)
    os.environ["ASTRBOT_RELOAD"] = "0"
    os.environ["ASTRBOT_DISABLE_METRICS"] = "1"
    if args.astrbot_source:
        sys.path.insert(0, str(args.astrbot_source.resolve()))
    sys.path.insert(0, str(work))
    os.chdir(work)
    import astrbot.api
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.star.context import Context
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.star.star import star_registry
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.provider.register import register_provider_adapter, llm_tools
    from astrbot.core.provider.provider import Provider
    from astrbot.core.provider.entities import LLMResponse, TokenUsage
    from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
    from astrbot.core.umop_config_router import UmopConfigRouter
    from astrbot.core.persona_mgr import PersonaManager
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.platform.manager import PlatformManager
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

    @register_provider_adapter("tmemory_acceptance", "Local deterministic acceptance Provider")
    class ControlledProvider(Provider):
        def __init__(self):
            super().__init__({"type": "tmemory_acceptance", "id": "qa-provider"}, {})
            self.set_model("shared-model")
            self.calls = []
        def get_current_key(self): return ""
        def set_key(self, key): pass
        async def get_models(self): return ["shared-model", "independent-model"]
        async def text_chat(self, **kwargs):
            self.calls.append(kwargs)
            return LLMResponse(role="assistant", completion_text='{"profile_items":[{"facet_type":"preference","content":"用户偏好使用Python语言","importance":0.8,"confidence":0.9}]}', usage=TokenUsage(input_other=20, input_cached=5, output=10))

    installed = Path(get_astrbot_plugin_path()) / "astrbot_plugin_tmemory"
    shutil.copytree(repo, installed, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "qa-artifacts", "data", "__pycache__", ".pytest_cache", ".codex", ".agents", "tests"))

    async def run():
        await db_helper.initialize()
        router = UmopConfigRouter(sp)
        await router.initialize()
        acm = AstrBotConfigManager(astrbot_config, router, sp)
        if callable(getattr(acm, "initialize", None)):
            await acm.initialize()
        persona = PersonaManager(db_helper, acm)
        await persona.initialize()
        providers = ProviderManager(acm, db_helper, persona)
        controlled = ControlledProvider()
        providers.inst_map["qa-provider"] = controlled
        providers.provider_insts.append(controlled)
        providers.curr_provider_inst = controlled
        queue = asyncio.Queue()
        platforms = PlatformManager(astrbot_config, queue)
        conversations = ConversationManager(db_helper)
        context = Context(event_queue=queue, config=astrbot_config, db=db_helper,
            provider_manager=providers, platform_manager=platforms,
            conversation_manager=conversations, message_history_manager=None,
            persona_manager=persona, astrbot_config_mgr=acm,
            knowledge_base_manager=None, cron_manager=None)
        manager = PluginManager(context, astrbot_config)
        Path(manager.plugin_config_path).mkdir(parents=True, exist_ok=True)
        (Path(manager.plugin_config_path) / "astrbot_plugin_tmemory_config.json").write_text(json.dumps({"webui_enabled":False, "enable_vector_search":False, "proactive_enabled":False}), encoding="utf-8")
        ok, error = await manager.load(specified_dir_name="astrbot_plugin_tmemory")
        assert ok, error
        def instance():
            return next(m.star_cls for m in star_registry if m.root_dir_name == "astrbot_plugin_tmemory")
        plugin = instance()
        assert plugin.context is context and plugin._worker_running
        assert star_handlers_registry.get_handlers_by_module_name(plugin.__module__)
        names = {t.name for t in llm_tools.func_list}
        assert {"remember", "recall"}.issubset(names), names
        package = plugin.__module__.rsplit(".", 1)[0]
        gateway = importlib.import_module(package + ".adapters.llm")
        send = importlib.import_module(package + ".adapters.send")
        await gateway.generate(plugin, chat_provider_id="qa-provider", model="independent-model", prompt="hello")
        assert controlled.calls[-1]["model"] == "independent-model"
        assert "model_id" not in controlled.calls[-1]
        assert controlled.get_model() == "shared-model"
        with plugin._db() as conn:
            assert conn.execute("SELECT tokens_total FROM llm_usage ORDER BY id DESC LIMIT 1").fetchone()[0] == 35
        plugin._cfg.profile_extraction_min_messages = 1
        plugin._cfg.use_independent_consolidation_model = True
        plugin._cfg.consolidation_provider_id = "qa-provider"
        plugin._cfg.consolidation_model_id = "independent-model"
        plugin._insert_conversation_sync("u", "user", "用户偏好使用Python语言", "bot1", "u", "bot1:FriendMessage:u", "bot1|private", "a")
        assert await plugin._extract_profiles() == 1
        assert await plugin._retrieve_memories("u", "Python", 5, scope="bot1|private", persona_id="a")
        assert not await plugin._retrieve_memories("u", "Python", 5, scope="bot1|group:g", persona_id="a")
        assert not await plugin._retrieve_memories("u", "Python", 5, scope="bot1|private", persona_id="b")
        block = await plugin._injection_builder.build_profile_injection("u", "Python", "new-dialogue", scope="bot1|private", persona_id="a")
        assert "Python" in block
        from astrbot.api.event import MessageChain
        assert isinstance(send.build_message_chain([{"type":"plain","text":"hello"}]), MessageChain)
        assert not await send.send_message(plugin, "missing:FriendMessage:u", [{"type":"plain","text":"hello"}])
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from astrbot.api.platform import AstrBotMessage, MessageType, PlatformMetadata, MessageMember
        from astrbot.core.platform.platform import Platform
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        from astrbot.core.platform.sources.telegram.tg_event import TelegramPlatformEvent
        from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
        for name, event_class, adapter_class in (("aiocqhttp", AiocqhttpMessageEvent, AiocqhttpAdapter), ("telegram", TelegramPlatformEvent, TelegramPlatformAdapter)):
            client = SimpleNamespace(send_private_msg=AsyncMock(), send_group_msg=AsyncMock(), send_message=AsyncMock(), send_chat_action=AsyncMock())
            def event_for(instance_id, group=False):
                message = AstrBotMessage()
                message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
                message.sender = MessageMember(user_id="42", nickname="QA")
                message.self_id, message.message_id = "bot", "1"
                message.group_id = "100" if group else ""
                message.message_str = "用户偏好使用Rust语言开发工具"
                from astrbot.api.message_components import Plain
                message.message = [Plain(text=message.message_str)]
                message.raw_message = {}
                return event_class(message.message_str, message, PlatformMetadata(name=name, description="QA", id=instance_id), "100" if group else "42", client)
            private = event_for(name + "-qa1")
            group = event_for(name + "-qa1", True)
            other = event_for(name + "-qa2")
            await conversations.new_conversation(private.unified_msg_origin, persona_id="a")
            await plugin._handle_on_any_message(private)
            result = await plugin.tool_remember(private, "用户偏好使用Rust语言开发工具", "preference")
            assert "p:" in result, result
            await conversations.new_conversation(private.unified_msg_origin, persona_id="a")
            assert "Rust" in await plugin.tool_recall(private, "Rust")
            assert "Rust" not in await plugin.tool_recall(group, "Rust")
            assert "Rust" not in await plugin.tool_recall(other, "Rust")
            await conversations.new_conversation(private.unified_msg_origin, persona_id="b")
            assert "Rust" not in await plugin.tool_recall(private, "Rust")
            adapter = adapter_class.__new__(adapter_class)
            Platform.__init__(adapter, {"id":name + "-qa1"}, queue)
            adapter.metadata = PlatformMetadata(name=name, description="QA", id=name + "-qa1")
            adapter.bot = adapter.client = client
            platforms.platform_insts.append(adapter)
            for event in (private, group):
                assert await send.send_message(plugin, event.unified_msg_origin, [{"type":"plain", "text":"local acceptance"}])
            if name == "aiocqhttp":
                assert client.send_private_msg.await_count == 1 and client.send_group_msg.await_count == 1
                client.send_private_msg.side_effect = RuntimeError("controlled transport failure")
            else:
                assert client.send_message.await_count >= 2
                client.send_message.side_effect = RuntimeError("controlled transport failure")
            assert not await send.send_message(plugin, private.unified_msg_origin, [{"type":"plain", "text":"local acceptance"}])
        old_instances = []
        for _ in range(3):
            old_instances.append(instance())
            ok, error = await manager.reload("astrbot_plugin_tmemory")
            assert ok, error
            assert instance() is not old_instances[-1]
            assert old_instances[-1]._db_mgr._conn is None
            await conversations._trigger_session_deleted("bot1:FriendMessage:u")
            assert all(p._db_mgr._conn is None for p in old_instances)
        metadata = next(m for m in star_registry if m.root_dir_name == "astrbot_plugin_tmemory")
        await manager._terminate_plugin(metadata)
        await conversations._trigger_session_deleted("bot1:FriendMessage:u")
        assert instance()._db_mgr._conn is None
        assert not instance()._worker_running
        report = {"loader": "passed", "context": "real", "reload_cycles":3,
            "tools":sorted(names), "provider_model_and_usage":"passed",
            "extraction_scope_persona_new_dialogue":"passed", "message_chain_and_send_false":"passed",
            "onebot_telegram_real_events_local_transport":"passed", "external_onebot_telegram":"not_verified", "dashboard_browser_auth":"not_verified"}
        (work / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("ACCEPTANCE_OK " + json.dumps(report, ensure_ascii=False), flush=True)
        await db_helper.engine.dispose()
    async def guarded():
        try:
            await run()
        finally:
            await db_helper.engine.dispose()
    asyncio.run(guarded())


if __name__ == "__main__":
    main()
