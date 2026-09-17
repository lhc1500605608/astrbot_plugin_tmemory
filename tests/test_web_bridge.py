"""T5 Phase 3a 契约测试：Plugin Pages bridge 后端（web/bridge.py）。

覆盖：
- 路由表与 legacy 全部能力对齐（登录改由 Dashboard 鉴权 → /session）
- 通过 context.register_web_api 注册，路由带插件名前缀
- 无自签 JWT
- 记忆 CRUD / 画像 / 蒸馏 / 身份 / 配置 / 导出契约
- legacy 回滚开关（webui_legacy_enabled）默认关闭
"""

from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeQuery:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None, *args, **kwargs):
        return self._values.get(key, default)


class FakeRequest:
    def __init__(self, json_body=None, query=None, username="admin"):
        self._json = {} if json_body is None else json_body
        self.query = FakeQuery(query)
        self.username = username

    async def json(self, default=None):
        return self._json


class FakeContext:
    def __init__(self):
        self.registered = []

    def register_web_api(self, route, view_handler, methods, desc):
        self.registered.append((route, view_handler, list(methods), desc))


def run(coro):
    return asyncio.run(coro)


def _legacy_route_signatures():
    source = (ROOT / "web" / "legacy_server.py").read_text(encoding="utf-8")
    pairs = re.findall(r'add_(get|post|patch)\("(/api/[^"]*)"', source)
    signatures = set()
    for method, path in pairs:
        if path == "/api/login":
            continue
        normalized = re.sub(r"\{(\w+)\}", r"<\1>", path[len("/api") :])
        signatures.add((method.upper(), normalized))
    return signatures


# ── 路由契约 ────────────────────────────────────────────────────────────────


def test_route_table_covers_every_legacy_capability(bridge_module):
    bridge_sigs = {
        (method, route.path)
        for route in bridge_module.PluginPagesBridge.ROUTES
        for method in route.methods
    }
    bridge_without_session = {sig for sig in bridge_sigs if sig != ("GET", "/session")}
    legacy_sigs = _legacy_route_signatures()

    assert legacy_sigs <= bridge_without_session
    # 唯一新增：bridge SDK 只有 apiGet/apiPost，配置更新需额外暴露 POST。
    assert bridge_without_session - legacy_sigs == {("POST", "/config")}
    assert ("GET", "/session") in bridge_sigs
    assert ("POST", "/login") not in bridge_sigs
    assert all(method == method.upper() for method, _ in bridge_sigs)


def test_register_uses_plugin_pages_api_with_plugin_prefix(
    bridge_module, plugin, monkeypatch
):
    _install_fake_web(monkeypatch, FakeRequest())
    bridge = bridge_module.PluginPagesBridge(plugin)
    context = FakeContext()

    count = bridge.register(context)

    assert count == len(bridge_module.PluginPagesBridge.ROUTES)
    assert len(context.registered) == count

    expected_routes = {
        f"/{bridge_module.PLUGIN_NAME}{route.path}"
        for route in bridge_module.PluginPagesBridge.ROUTES
    }
    registered_routes = {entry[0] for entry in context.registered}
    assert registered_routes == expected_routes
    assert all(callable(entry[1]) for entry in context.registered)
    assert all(entry[3] for entry in context.registered)

    by_route = {entry[0]: entry for entry in context.registered}
    config_route = by_route[f"/{bridge_module.PLUGIN_NAME}/config"]
    assert config_route[2] == ["POST", "PATCH"]
    assert by_route[f"/{bridge_module.PLUGIN_NAME}/profile/items/<id>/evidence"][2] == [
        "GET"
    ]


def test_registered_view_receives_path_params(bridge_module, plugin, monkeypatch):
    _install_fake_web(monkeypatch, FakeRequest(query={"user": "u1"}))
    bridge = bridge_module.PluginPagesBridge(plugin)
    context = FakeContext()
    bridge.register(context)
    by_route = {entry[0]: entry for entry in context.registered}
    view = by_route[f"/{bridge_module.PLUGIN_NAME}/profile/items/<id>/evidence"][1]

    response = run(view(id="7"))
    assert response["kind"] == "json"
    assert response["status"] == 200
    assert response["data"] == {"evidence": []}


def test_register_requires_register_web_api_capability(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    with pytest.raises(RuntimeError):
        bridge.register(object())


def test_register_accepts_unbound_request_proxy(bridge_module, plugin, monkeypatch):
    """回归：真实 PluginRequestProxy 在注册期访问属性抛 RuntimeError。

    注册必须仍然成功（异常代表请求上下文未绑定，而非能力缺失）。
    """

    class _UnboundProxy:
        @property
        def query(self):
            raise RuntimeError("request is only available inside a handler")

        async def json(self, default=None):
            return default

    _install_fake_web(monkeypatch, _UnboundProxy())
    bridge = bridge_module.PluginPagesBridge(plugin)
    context = FakeContext()

    assert bridge.register(context) == len(bridge_module.PluginPagesBridge.ROUTES)


def test_register_rejects_missing_api_web_sdk(bridge_module, plugin, monkeypatch):
    """回归 TMEAAA-369：4.23.2 有 register_web_api 但无 astrbot.api.web。

    注册阶段必须直接拒绝，而不是注册成功后在请求期 ModuleNotFoundError → 500。
    """
    monkeypatch.delitem(sys.modules, "astrbot.api.web", raising=False)
    import astrbot

    monkeypatch.delattr(astrbot.api, "web", raising=False)

    bridge = bridge_module.PluginPagesBridge(plugin)
    with pytest.raises(RuntimeError):
        bridge.register(FakeContext())


def test_bridge_source_has_no_self_signed_jwt(bridge_module):
    source = (ROOT / "web" / "bridge.py").read_text(encoding="utf-8").lower()
    assert "jwt_encode" not in source
    assert "jwt_decode" not in source
    assert "secrets.token" not in source
    assert "import jwt" not in source


def test_build_bridge_payload_envelope(bridge_module):
    body, status = bridge_module.build_bridge_payload({"ok": True}, 200)
    assert body == {"ok": True} and status == 200

    body, status = bridge_module.build_bridge_payload({"error": "bad input"}, 400)
    assert body["status"] == "error"
    assert body["message"] == "bad input"
    assert status == 400


def test_session_reports_dashboard_user_without_token(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    payload, status = run(bridge.session(FakeRequest(username="alice")))

    assert status == 200
    assert payload["username"] == "alice"
    assert payload["auth"] == "dashboard"
    assert "token" not in payload


# ── wrapper 错误信封（无需真实 AstrBot）───────────────────────────────────────


def _install_fake_web(monkeypatch, request_value):
    module = types.ModuleType("astrbot.api.web")

    def json_response(data=None, *, status_code=200, headers=None):
        return {"kind": "json", "data": data, "status": status_code}

    def error_response(message, *, status_code=400, data=None, headers=None):
        return {"kind": "error", "message": message, "status": status_code}

    module.json_response = json_response
    module.error_response = error_response
    module.request = request_value

    monkeypatch.setitem(sys.modules, "astrbot.api.web", module)
    import astrbot

    monkeypatch.setattr(astrbot.api, "web", module, raising=False)
    return module


def test_wrapper_maps_validation_error_to_error_envelope(bridge_module, plugin, monkeypatch):
    _install_fake_web(monkeypatch, FakeRequest(json_body={}))
    bridge = bridge_module.PluginPagesBridge(plugin)
    view = bridge._wrap(bridge.memory_delete)

    response = run(view())
    assert response["kind"] == "error"
    assert response["status"] == 400
    assert "id" in response["message"]


def test_wrapper_maps_success_to_json_envelope(bridge_module, plugin, monkeypatch):
    _install_fake_web(monkeypatch, FakeRequest(json_body={"user": "u1", "memory": "hello"}))
    bridge = bridge_module.PluginPagesBridge(plugin)
    view = bridge._wrap(bridge.memory_add)

    response = run(view())
    assert response["kind"] == "json"
    assert response["status"] == 200
    assert response["data"]["ok"] is True


# ── 记忆 CRUD ───────────────────────────────────────────────────────────────


def test_memory_crud_roundtrip(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)

    add_payload, add_status = run(
        bridge.memory_add(FakeRequest(json_body={"user": "u1", "memory": "偏好 Python"}))
    )
    assert add_status == 200
    memory_id = add_payload["memory_id"]

    listed, _ = run(bridge.memories(FakeRequest(query={"user": "u1"})))
    assert any(item["id"] == memory_id for item in listed["memories"])

    update_payload, update_status = run(
        bridge.memory_update(
            FakeRequest(json_body={"id": memory_id, "memory": "偏好 Python 3"})
        )
    )
    assert update_status == 200 and update_payload["ok"] is True

    pin_payload, _ = run(
        bridge.memory_pin(FakeRequest(json_body={"id": memory_id, "pinned": True}))
    )
    assert pin_payload == {"ok": True, "pinned": True}

    delete_payload, delete_status = run(
        bridge.memory_delete(FakeRequest(json_body={"id": memory_id}))
    )
    assert delete_status == 200 and delete_payload["ok"] is True


def test_memory_add_requires_user_and_memory(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    payload, status = run(bridge.memory_add(FakeRequest(json_body={"user": "u1"})))
    assert status == 400 and "error" in payload


def test_memory_update_rejects_non_positive_id(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    with pytest.raises(ValueError):
        run(bridge.memory_update(FakeRequest(json_body={"id": 0, "memory": "x"})))


def test_memory_merge_requires_two_ids(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    payload, status = run(
        bridge.memory_merge(FakeRequest(json_body={"user": "u1", "ids": [1]}))
    )
    assert status == 400 and "error" in payload


# ── 画像 ────────────────────────────────────────────────────────────────────


def test_profile_read_and_archive_flow(bridge_module, plugin, seeded_profile_items):
    bridge = bridge_module.PluginPagesBridge(plugin)
    item_id = seeded_profile_items[0]["id"]

    summary, _ = run(bridge.profile_summary(FakeRequest(query={"user": "test-user"})))
    assert isinstance(summary, dict)

    items, _ = run(bridge.profile_items(FakeRequest(query={"user": "test-user"})))
    assert items["items"]

    evidence, _ = run(bridge.profile_item_evidence(FakeRequest(), item_id))
    assert "evidence" in evidence

    archived, _ = run(bridge.profile_item_archive(FakeRequest(json_body={"id": item_id})))
    assert archived["ok"] is True


def test_profile_merge_requires_distinct_ids(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    with pytest.raises(ValueError):
        run(
            bridge.profile_items_merge(
                FakeRequest(json_body={"user": "u1", "ids": [1, 1]})
            )
        )


# ── 身份 / 导出 / 清除 ──────────────────────────────────────────────────────


def test_identity_merge_rejects_same_user(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    payload, status = run(
        bridge.identity_merge(
            FakeRequest(json_body={"from_user": "a", "to_user": "a"})
        )
    )
    assert status == 400 and "error" in payload


def test_user_export_and_purge(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    run(bridge.memory_add(FakeRequest(json_body={"user": "u2", "memory": "喜欢喝茶"})))

    exported, export_status = run(bridge.user_export(FakeRequest(json_body={"user": "u2"})))
    assert export_status == 200 and isinstance(exported, dict)

    purged, purge_status = run(bridge.user_purge(FakeRequest(json_body={"user": "u2"})))
    assert purge_status == 200 and purged["ok"] is True


# ── 蒸馏 / 配置 / 能力 ──────────────────────────────────────────────────────


def test_distill_history_and_capabilities_contract(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)

    history, _ = run(bridge.distill_history(FakeRequest()))
    assert "history" in history and "budget" in history

    caps, _ = run(bridge.capabilities(FakeRequest()))
    assert "capabilities" in caps and "warnings" in caps


def test_config_get_returns_plugin_config(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)

    full, status = run(bridge.config_get(FakeRequest()))
    assert status == 200
    assert "memory_mode" in full
    assert "capture_skip_regex" not in full
    assert "platform" not in full

    filtered, _ = run(bridge.config_get(FakeRequest(query={"keys": "memory_mode"})))
    assert set(filtered) == {"memory_mode"}


def test_config_update_validates_and_applies(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)

    with pytest.raises(ValueError):
        run(bridge.config_update(FakeRequest(json_body={"not_a_real_key": 1})))

    payload, status = run(
        bridge.config_update(FakeRequest(json_body={"distill_pause": True}))
    )
    assert status == 200 and payload["status"] == "ok"
    assert plugin._cfg.distill_pause is True


def test_test_conversation_contract(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)

    bad, bad_status = run(bridge.test_conversation(FakeRequest(json_body={})))
    assert bad_status == 400

    good, good_status = run(
        bridge.test_conversation(
            FakeRequest(json_body={"user_id": "u3", "role": "user", "content": "你好"})
        )
    )
    assert good_status == 200 and good["ok"] is True


# ── legacy 回滚开关 ─────────────────────────────────────────────────────────


def test_plugin_registers_bridge_when_plugin_pages_available(
    plugin_module, tmp_path, monkeypatch
):
    from astrbot_plugin_tmemory.adapters import version as version_adapter

    _install_fake_web(monkeypatch, FakeRequest())
    monkeypatch.setattr(version_adapter, "has_plugin_pages", lambda: True)
    monkeypatch.chdir(tmp_path)

    context = FakeContext()
    instance = plugin_module.TMemoryPlugin(context=context, config={})

    assert instance._pages_bridge is not None
    assert len(context.registered) == len(instance._pages_bridge.ROUTES)
    assert all(
        route.startswith("/astrbot_plugin_tmemory/")
        for route, _handler, _methods, _desc in context.registered
    )


def test_legacy_webui_disabled_by_default(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(
        context=None,
        config={"webui_enabled": True, "webui_password": "secret"},
    )
    assert instance._web_server.__class__.__name__ == "_NullWebServer"


def test_legacy_webui_loads_when_flag_enabled(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "webui_enabled": True,
            "webui_legacy_enabled": True,
            "webui_password": "secret",
        },
    )
    assert instance._web_server.__class__.__name__ == "TMemoryWebServer"
    assert instance._web_server.enabled is True


def test_legacy_module_location_and_schema_default():
    assert (ROOT / "web" / "legacy_server.py").exists()
    assert not (ROOT / "web_server.py").exists()

    schema = (ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    assert '"webui_legacy_enabled"' in schema
    assert re.search(
        r'"webui_legacy_enabled":\s*\{[^}]*"default":\s*false', schema, re.DOTALL
    )
