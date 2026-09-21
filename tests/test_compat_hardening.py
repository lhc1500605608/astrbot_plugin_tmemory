"""Phase 1 兼容硬化测试（TMEAAA-356 / Plan TMEAAA-354 Phase 1）。

覆盖：
- adapters/version.py 能力探测（mark_as_temp / plugin pages / session hooks）
- extra_user_temp 不可用时显式告警 + 计数（不再静默降级）
- WebUI 配置保存优先 save_config_async()，保留同步兜底
- _conf_schema.json 敏感字段 secret 掩码 + extra_user_temp UI 提示
- persona 优先 conversation_manager，事件私有字段仅最后兜底并打点
"""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]


class _DummyReq:
    def __init__(self, prompt: str = "", system_prompt: str = ""):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.extra_user_content_parts = []


class _DummyEvent:
    def __init__(self, extras=None, conversation=None, umo="qq:FriendMessage:1"):
        self.unified_msg_origin = umo
        self._extras = extras or {}
        if conversation is not None:
            self.conversation = conversation


class _FakeConversation:
    def __init__(self, persona_id: str):
        self.persona_id = persona_id


class _FakeConvMgr:
    def __init__(self, persona_id: str):
        self.persona_id = persona_id

    async def get_curr_conversation_id(self, umo: str):
        return "cid-1" if self.persona_id else None

    async def get_conversation(self, umo: str, cid: str):
        return _FakeConversation(self.persona_id)


# ── adapters/version.py 能力探测 ──────────────────────────────────────────────


def test_capabilities_snapshot_exposes_required_flags(plugin_module):
    from astrbot_plugin_tmemory.adapters import version

    version.reset_capability_cache()
    try:
        caps = version.get_capabilities()
        # conftest stub 模拟 AstrBot 4.23.2：无 mark_as_temp；plugin pages /
        # session hooks 也不在 stub 内。断言 False 以证明 stub 不伪造上游能力。
        assert caps.has_mark_as_temp is False
        assert caps.has_plugin_pages is False
        assert caps.has_session_hooks is False
        data = caps.to_dict()
        for key in (
            "version",
            "has_mark_as_temp",
            "has_plugin_pages",
            "has_session_hooks",
            "extra_user_temp_available",
            "extra_user_temp_min_version",
        ):
            assert key in data
        assert data["extra_user_temp_available"] is False
    finally:
        version.reset_capability_cache()


def test_capability_probe_detects_4_28_mark_as_temp(plugin_module, mark_as_temp_support):
    """显式安装 4.28 形状 TextPart 后，能力探测必须翻转为 True。"""
    from astrbot_plugin_tmemory.adapters import version

    assert version.has_mark_as_temp() is True
    assert version.extra_user_temp_available() is True


def test_capability_probe_detects_missing_mark_as_temp(plugin_module):
    from astrbot_plugin_tmemory.adapters import version

    msg_mod = sys.modules["astrbot.core.agent.message"]
    original = msg_mod.TextPart

    class _NoMarkTextPart:
        def __init__(self, text: str):
            self.text = text

    msg_mod.TextPart = _NoMarkTextPart
    try:
        version.reset_capability_cache()
        assert version.has_mark_as_temp() is False
        assert version.extra_user_temp_available() is False
        assert "4.28" in version.extra_user_temp_hint()
    finally:
        msg_mod.TextPart = original
        version.reset_capability_cache()


def _install_plugin_pages_context(monkeypatch) -> None:
    """让 _probe_plugin_pages 的 Context.register_web_api 探测命中。"""
    star_pkg = types.ModuleType("astrbot.core.star")
    star_pkg.__path__ = []
    context_mod = types.ModuleType("astrbot.core.star.context")

    class _ContextWithWebApi:
        def register_web_api(self, *args, **kwargs):
            return None

    context_mod.Context = _ContextWithWebApi
    star_pkg.context = context_mod
    monkeypatch.setitem(sys.modules, "astrbot.core.star", star_pkg)
    monkeypatch.setitem(sys.modules, "astrbot.core.star.context", context_mod)


class _UnboundRequestProxy:
    """模拟真实 ``PluginRequestProxy``：handler 外访问属性抛 RuntimeError。"""

    @property
    def query(self):
        raise RuntimeError(
            "astrbot.api.web.request is only available inside a plugin Web API handler."
        )

    async def json(self, default=None):
        return default


def _install_api_web(monkeypatch, request_value=None) -> None:
    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.json_response = lambda data=None, **kwargs: data
    web_mod.error_response = lambda message, **kwargs: message
    web_mod.request = (
        request_value
        if request_value is not None
        else types.SimpleNamespace(query={}, json=lambda **kwargs: None)
    )
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web_mod)
    import astrbot

    monkeypatch.setattr(astrbot.api, "web", web_mod, raising=False)


def test_plugin_pages_probe_rejects_4_23_2_shape(plugin_module, monkeypatch):
    """回归 TMEAAA-369：4.23.2 有 register_web_api，但缺 astrbot.api.web。

    只探测 register_web_api 会误判 plugin pages 可用 → bridge 注册后请求期 500。
    """
    from astrbot_plugin_tmemory.adapters import version

    _install_plugin_pages_context(monkeypatch)
    monkeypatch.delitem(sys.modules, "astrbot.api.web", raising=False)
    import astrbot

    monkeypatch.delattr(astrbot.api, "web", raising=False)

    version.reset_capability_cache()
    try:
        assert version._probe_plugin_web_module() is False
        assert version.has_plugin_pages() is False
        assert version.get_capabilities().to_dict()["has_plugin_pages"] is False
    finally:
        version.reset_capability_cache()


def test_plugin_pages_probe_accepts_unbound_request_proxy(plugin_module, monkeypatch):
    """回归：真实 PluginRequestProxy 在加载期访问 ``query`` 会抛 RuntimeError。

    该异常表示「请求上下文未绑定」，不是能力缺失；探测必须退化为类型级校验并
    判定为可用，否则 4.28 也会误判 plugin_pages=False（bridge 不注册）。
    """
    from astrbot_plugin_tmemory.adapters import version

    _install_plugin_pages_context(monkeypatch)
    _install_api_web(monkeypatch, request_value=_UnboundRequestProxy())

    version.reset_capability_cache()
    try:
        assert version.plugin_web_request_contract_ok(_UnboundRequestProxy()) is True
        assert version._probe_plugin_web_module() is True
        assert version.has_plugin_pages() is True
    finally:
        version.reset_capability_cache()


def test_plugin_web_request_contract_rejects_incomplete_proxy(plugin_module):
    from astrbot_plugin_tmemory.adapters import version

    assert version.plugin_web_request_contract_ok(None) is False
    assert version.plugin_web_request_contract_ok(object()) is False


def test_plugin_pages_probe_accepts_4_28_shape(plugin_module, monkeypatch):
    from astrbot_plugin_tmemory.adapters import version

    _install_plugin_pages_context(monkeypatch)
    _install_api_web(monkeypatch)

    version.reset_capability_cache()
    try:
        assert version._probe_plugin_web_module() is True
        assert version.has_plugin_pages() is True
    finally:
        version.reset_capability_cache()


def test_inject_extra_user_temp_gated_by_capability(plugin_module, monkeypatch):
    from astrbot_plugin_tmemory.adapters import llm, version

    monkeypatch.setattr(version, "has_mark_as_temp", lambda: False)
    req = _DummyReq()
    assert llm.inject_extra_user_temp(req, "[用户记忆]") is False
    assert req.extra_user_content_parts == []


# ── extra_user_temp 不再静默降级 ───────────────────────────────────────────────


def test_extra_user_temp_unavailable_warns_and_counts(plugin, monkeypatch, caplog):
    from astrbot_plugin_tmemory.adapters import version

    monkeypatch.setattr(version, "has_mark_as_temp", lambda: False)
    plugin._extra_user_temp_fallback_count = 0
    plugin._cfg.inject_position = "extra_user_temp"
    req = _DummyReq(prompt="你好", system_prompt="你是AI助手。")

    with caplog.at_level(logging.WARNING, logger="astrbot"):
        plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户在北京")

    assert req.extra_user_content_parts == []
    assert req.system_prompt.startswith("你是AI助手。")
    assert "[用户记忆]" in req.system_prompt
    assert plugin._extra_user_temp_fallback_count == 1
    assert any("4.28" in r.getMessage() for r in caplog.records), (
        "不可用时的降级必须是显式告警（含版本要求），而非静默"
    )


def test_extra_user_temp_available_uses_extra_parts_without_counting(
    plugin, mark_as_temp_support
):
    plugin._extra_user_temp_fallback_count = 0
    plugin._cfg.inject_position = "extra_user_temp"
    req = _DummyReq(prompt="你好", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户在北京")

    assert len(req.extra_user_content_parts) == 1
    assert req.system_prompt == "你是AI助手。"
    assert plugin._extra_user_temp_fallback_count == 0


# ── persona：优先 conversation_manager，私有字段仅最后兜底 ─────────────────────


@pytest.mark.asyncio
async def test_persona_prefers_conversation_manager(plugin):
    plugin.context = types.SimpleNamespace(
        conversation_manager=_FakeConvMgr("persona-A")
    )
    plugin._persona_private_fallback_count = 0
    event = _DummyEvent()

    persona = await plugin._get_current_persona_async(event)

    assert persona == "persona-A"
    assert plugin._persona_private_fallback_count == 0, (
        "conversation_manager 命中时不得触碰事件私有字段兜底"
    )


@pytest.mark.asyncio
async def test_persona_private_fallback_counts_when_manager_empty(plugin):
    plugin.context = types.SimpleNamespace(conversation_manager=None)
    plugin._persona_private_fallback_count = 0
    event = _DummyEvent(extras={"conversation": _FakeConversation("persona-B")})

    persona = await plugin._get_current_persona_async(event)

    assert persona == "persona-B"
    assert plugin._persona_private_fallback_count == 1


# ── WebUI 配置保存：save_config_async 优先，同步兜底 ────────────────────────────


class _RecordingConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.async_calls = 0
        self.sync_calls = 0

    async def save_config_async(self):
        self.async_calls += 1

    def save_config(self):
        self.sync_calls += 1


class _SyncOnlyConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sync_calls = 0

    def save_config(self):
        self.sync_calls += 1


async def _api_client(web_module, plugin):
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_username": "admin",
            "webui_password": "secret",
        },
    )
    server._app = web_module.web.Application(middlewares=[server._middleware])
    server._setup_routes()
    client = TestClient(TestServer(server._app))
    await client.start_server()
    token = web_module.jwt_encode({"user": "admin"}, server._jwt_secret, 3600)
    return client, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_config_patch_prefers_async_save(web_module, plugin):
    cfg = _RecordingConfig()
    plugin.config = cfg
    client, headers = await _api_client(web_module, plugin)
    try:
        resp = await client.patch(
            "/api/config", headers=headers, json={"distill_pause": True}
        )
        assert resp.status == 200
        assert cfg.async_calls == 1
        assert cfg.sync_calls == 0, "存在 save_config_async 时不得调用阻塞式同步保存"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_config_patch_falls_back_to_sync_save(web_module, plugin):
    cfg = _SyncOnlyConfig()
    plugin.config = cfg
    client, headers = await _api_client(web_module, plugin)
    try:
        resp = await client.patch(
            "/api/config", headers=headers, json={"distill_pause": True}
        )
        assert resp.status == 200
        assert cfg.sync_calls == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_config_patch_returns_capability_warning(web_module, plugin, monkeypatch):
    from astrbot_plugin_tmemory.adapters import version

    monkeypatch.setattr(version, "extra_user_temp_available", lambda: False)
    plugin.config = _RecordingConfig()
    client, headers = await _api_client(web_module, plugin)
    try:
        resp = await client.patch(
            "/api/config", headers=headers, json={"inject_position": "extra_user_temp"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert body["warnings"] and "4.28" in body["warnings"][0]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_capabilities_endpoint_reports_flags(
    web_module, plugin, mark_as_temp_support
):
    client, headers = await _api_client(web_module, plugin)
    try:
        resp = await client.get("/api/capabilities", headers=headers)
        assert resp.status == 200
        body = await resp.json()
        assert body["capabilities"]["has_mark_as_temp"] is True
        assert body["capabilities"]["extra_user_temp_min_version"] == "4.28"
        assert "warnings" in body
        assert body["runtime"]["extra_user_temp_fallback_count"] == 0
        assert body["runtime"]["persona_private_fallback_count"] == 0
    finally:
        await client.close()


# ── _conf_schema.json：secret 掩码 + UI 提示 ──────────────────────────────────


def test_conf_schema_marks_sensitive_fields_secret():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert (
        schema["webui_settings"]["items"]["webui_password"].get("secret") is True
    ), "WebUI 密码必须标记 secret:true 以防明文泄露"
    assert (
        schema["vector_retrieval"]["items"]["standalone_embedding"]["items"]["embedding_api_key"].get("secret")
        is True
    ), "Embedding API Key 必须标记 secret:true"


def test_conf_schema_inject_position_hint_mentions_version_requirement():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    hint = schema["injection"]["items"]["inject_position"]["hint"]
    assert "4.28" in hint, "inject_position 的 UI 提示需说明 extra_user_temp 依赖 ≥4.28"
