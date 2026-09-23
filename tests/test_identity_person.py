"""TMEAAA-540: 人物身份权威 —— resolve_person 契约 + bind/list/unbind API。

覆盖：
- ``resolve_person`` 私聊（canonical=Person id）/ 群聊（不跨人聚合）/ fail-closed；
  只读、不隐式建绑定。
- ``AdminService.bind_identity`` / ``unbind_identity`` / ``get_person_identities``
  的幂等、回退自指语义与审计事件。
- 显示名重复建议（默认关，开启只提示、不自动绑定）。
- canonical（Person）一致性：bind/merge 后 resolve_person 跟随。
"""

from __future__ import annotations

import pytest

UMO_PRIVATE = "qq:FriendMessage:42"
UMO_PRIVATE_OTHER = "wx:FriendMessage:7"
UMO_GROUP = "qq:GroupMessage:1000"


def _seed_profile(plugin, canonical_id, display_name=""):
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ProfileItemOps(plugin).upsert_profile_item(
        canonical_id, "fact", "t", "内容", 0.8, 0.8
    )
    if display_name:
        with plugin._db() as conn:
            conn.execute(
                "UPDATE user_profiles SET display_name=? WHERE canonical_user_id=?",
                (display_name, canonical_id),
            )


def _seed_umo_cache(plugin, umo, canonical_id, persona_id=""):
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO conversation_cache"
            "(canonical_user_id, role, content, unified_msg_origin, persona_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (canonical_id, "user", "hello", umo, persona_id, plugin._now()),
        )


# ── resolve_person 契约 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_person_private_falls_back_to_self(plugin):
    """私聊未绑定 → person_id = adapter:user（自成 Person）。"""
    result = await plugin.resolve_person(UMO_PRIVATE)
    assert result == {
        "person_id": "qq:42",
        "adapter": "qq",
        "adapter_user_id": "42",
        "is_group": False,
    }


@pytest.mark.asyncio
async def test_resolve_person_private_returns_canonical_person_id(plugin):
    """私聊绑定后 → person_id 为该 Person 的权威 canonical id。"""
    plugin._identity_mgr.bind_identity("qq", "42", "person-zhang")
    result = await plugin.resolve_person(UMO_PRIVATE)
    assert result["person_id"] == "person-zhang"
    assert result["is_group"] is False
    assert result["adapter"] == "qq"
    assert result["adapter_user_id"] == "42"


@pytest.mark.asyncio
async def test_resolve_person_uses_cache_before_binding(plugin):
    """conversation_cache 中的 canonical 优先于绑定。"""
    _seed_umo_cache(plugin, UMO_PRIVATE, "cached-person")
    plugin._identity_mgr.bind_identity("qq", "42", "person-zhang")
    result = await plugin.resolve_person(UMO_PRIVATE)
    assert result["person_id"] == "cached-person"


@pytest.mark.asyncio
async def test_resolve_person_group_does_not_aggregate(plugin):
    """群聊 → is_group=True 且 person_id=""（即使存在群缓存也不跨人聚合）。"""
    _seed_umo_cache(plugin, UMO_GROUP, "person-zhang")
    plugin._identity_mgr.bind_identity("qq", "1000", "person-zhang")
    result = await plugin.resolve_person(UMO_GROUP)
    assert result["is_group"] is True
    assert result["person_id"] == ""
    assert result["adapter"] == "qq"


@pytest.mark.asyncio
async def test_resolve_person_empty_returns_empty(plugin):
    assert await plugin.resolve_person("") == {}
    assert await plugin.resolve_person("   ") == {}


@pytest.mark.asyncio
async def test_resolve_person_malformed_umo_returns_empty(plugin):
    assert await plugin.resolve_person("just-one-segment") == {}


@pytest.mark.asyncio
async def test_resolve_person_is_read_only(plugin):
    """只读契约：解析私聊不隐式写入 identity_bindings。"""
    await plugin.resolve_person(UMO_PRIVATE_OTHER)
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM identity_bindings WHERE adapter='wx' AND adapter_user_id='7'"
        ).fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_resolve_person_uninitialized_returns_empty(plugin):
    db_mgr = plugin._db_mgr
    plugin._db_mgr = None
    try:
        assert await plugin.resolve_person(UMO_PRIVATE) == {}
    finally:
        plugin._db_mgr = db_mgr


@pytest.mark.asyncio
async def test_resolve_person_exception_fails_closed(plugin, monkeypatch):
    def _boom(self, umo):
        raise RuntimeError("boom")

    monkeypatch.setattr(type(plugin), "_resolve_canonical_from_umo", _boom)
    assert await plugin.resolve_person(UMO_PRIVATE) == {}


@pytest.mark.asyncio
async def test_resolve_person_follows_merge(plugin):
    """canonical 一致性：合并 Person 后 resolve_person 跟随目标 Person。"""
    plugin._identity_mgr.bind_identity("qq", "42", "person-a")
    plugin._identity_mgr.merge_identity("person-a", "person-b")
    result = await plugin.resolve_person(UMO_PRIVATE)
    assert result["person_id"] == "person-b"


# ── bind / unbind / list ─────────────────────────────────────────────────────


def test_bind_identity_creates_and_is_idempotent(admin_svc, plugin):
    first = admin_svc.bind_identity("qq", "42", "person-zhang")
    second = admin_svc.bind_identity("qq", "42", "person-zhang")
    assert first["binding_id"] == second["binding_id"]
    assert first["canonical_user_id"] == "person-zhang"
    with plugin._db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM identity_bindings WHERE adapter='qq' AND adapter_user_id='42'"
        ).fetchone()["n"]
    assert n == 1


def test_bind_identity_empty_canonical_creates_new_person(admin_svc):
    result = admin_svc.bind_identity("qq", "99", "")
    assert result["canonical_user_id"] == "qq:99"


def test_bind_identity_requires_adapter_and_user(admin_svc):
    with pytest.raises(ValueError):
        admin_svc.bind_identity("", "42", "person-zhang")
    with pytest.raises(ValueError):
        admin_svc.bind_identity("qq", "", "person-zhang")


def test_bind_identity_writes_audit_event(admin_svc, plugin):
    admin_svc.bind_identity("qq", "42", "person-zhang")
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT event_type FROM memory_events WHERE event_type='bind' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row["event_type"] == "bind"


def test_unbind_identity_reverts_to_self_and_is_idempotent(admin_svc, plugin):
    bound = admin_svc.bind_identity("qq", "42", "person-zhang")
    result = admin_svc.unbind_identity(bound["binding_id"])
    assert result["canonical_user_id"] == "qq:42"
    assert result["old_canonical"] == "person-zhang"

    again = admin_svc.unbind_identity(bound["binding_id"])
    assert again["canonical_user_id"] == "qq:42"
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT canonical_user_id FROM identity_bindings WHERE id=?",
            (bound["binding_id"],),
        ).fetchone()
    assert row["canonical_user_id"] == "qq:42"


def test_unbind_identity_writes_audit_event(admin_svc, plugin):
    bound = admin_svc.bind_identity("qq", "42", "person-zhang")
    admin_svc.unbind_identity(bound["binding_id"])
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT event_type FROM memory_events WHERE event_type='unbind' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None


def test_unbind_identity_missing_raises(admin_svc):
    with pytest.raises(LookupError):
        admin_svc.unbind_identity(999999)


def test_get_person_identities_groups_with_display_name(admin_svc, plugin):
    _seed_profile(plugin, "person-zhang", display_name="张三")
    admin_svc.bind_identity("qq", "42", "person-zhang")
    admin_svc.bind_identity("wx", "7", "person-zhang")
    admin_svc.bind_identity("tg", "9", "")  # self Person, 无画像

    data = admin_svc.get_person_identities()
    persons = {p["person_id"]: p for p in data["persons"]}
    assert persons["person-zhang"]["display_name"] == "张三"
    assert len(persons["person-zhang"]["bindings"]) == 2
    assert persons["tg:9"]["bindings"][0]["adapter_user_id"] == "9"
    assert "suggestions" not in data


def test_get_person_identities_suggestions_only_when_requested(admin_svc, plugin):
    """显示名完全一致 → 提示；关闭时不附带 suggestions，且绝不自动绑定。"""
    _seed_profile(plugin, "qq:42", display_name="张三")
    _seed_profile(plugin, "wx:7", display_name="张三")
    admin_svc.bind_identity("qq", "42", "qq:42")
    admin_svc.bind_identity("wx", "7", "wx:7")

    off = admin_svc.get_person_identities(include_suggestions=False)
    assert "suggestions" not in off

    on = admin_svc.get_person_identities(include_suggestions=True)
    assert on["suggestions"]
    hit = on["suggestions"][0]
    assert hit["display_name"] == "张三"
    assert {hit["person_id"], hit["candidate_person_id"]} == {"qq:42", "wx:7"}

    # 只提示：绑定未被自动改写。
    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT canonical_user_id FROM identity_bindings ORDER BY adapter"
        ).fetchall()
    assert [r["canonical_user_id"] for r in rows] == ["qq:42", "wx:7"]


def test_get_person_identities_no_suggestion_for_distinct_names(admin_svc, plugin):
    _seed_profile(plugin, "qq:42", display_name="张三")
    _seed_profile(plugin, "wx:7", display_name="李四")
    admin_svc.bind_identity("qq", "42", "qq:42")
    admin_svc.bind_identity("wx", "7", "wx:7")
    assert admin_svc.get_person_identities(include_suggestions=True)["suggestions"] == []


# ── bridge 路由 ──────────────────────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None, *args, **kwargs):
        return self._values.get(key, default)


class _FakeRequest:
    def __init__(self, json_body=None, query=None, username="admin"):
        self._json = {} if json_body is None else json_body
        self.query = _FakeQuery(query)
        self.username = username

    async def json(self, default=None):
        return self._json


def test_bridge_routes_registered(bridge_module):
    routes = {(m, r.path) for r in bridge_module.PluginPagesBridge.ROUTES for m in r.methods}
    assert ("POST", "/identity/bind") in routes
    assert ("GET", "/identity/list") in routes
    assert ("POST", "/identity/unbind") in routes


@pytest.mark.asyncio
async def test_bridge_identity_bind_list_unbind(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)

    payload, status = await bridge.identity_bind(
        _FakeRequest(json_body={"adapter": "qq", "adapter_user_id": "42", "canonical_user_id": "p1"})
    )
    assert status == 200 and payload["ok"] is True
    assert payload["canonical_user_id"] == "p1"

    listed, status = await bridge.identity_list(_FakeRequest())
    assert status == 200
    assert any(p["person_id"] == "p1" for p in listed["persons"])
    assert "suggestions" not in listed

    unbound, status = await bridge.identity_unbind(
        _FakeRequest(json_body={"binding_id": payload["binding_id"]})
    )
    assert status == 200 and unbound["canonical_user_id"] == "qq:42"


@pytest.mark.asyncio
async def test_bridge_identity_bind_requires_fields(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    payload, status = await bridge.identity_bind(_FakeRequest(json_body={"adapter": "qq"}))
    assert status == 400 and "error" in payload


@pytest.mark.asyncio
async def test_bridge_identity_list_gated_by_config(bridge_module, plugin):
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    for canonical in ("qq:42", "wx:7"):
        ProfileItemOps(plugin).upsert_profile_item(canonical, "fact", "t", "c", 0.8, 0.8)
        with plugin._db() as conn:
            conn.execute(
                "UPDATE user_profiles SET display_name='张三' WHERE canonical_user_id=?",
                (canonical,),
            )
    bridge = bridge_module.PluginPagesBridge(plugin)
    bridge.admin().bind_identity("qq", "42", "qq:42")
    bridge.admin().bind_identity("wx", "7", "wx:7")

    off, _ = await bridge.identity_list(_FakeRequest())
    assert "suggestions" not in off

    plugin._cfg.identity_autobind_display_name = True
    on, _ = await bridge.identity_list(_FakeRequest())
    assert on["suggestions"]


@pytest.mark.asyncio
async def test_bridge_identity_unbind_missing_returns_404(
    bridge_module, plugin, monkeypatch
):
    import sys
    import types

    import astrbot

    module = types.ModuleType("astrbot.api.web")
    module.json_response = lambda data=None, *, status_code=200, headers=None: {
        "kind": "json", "data": data, "status": status_code,
    }
    module.error_response = lambda message, *, status_code=400, data=None, headers=None: {
        "kind": "error", "message": message, "status": status_code,
    }
    module.request = _FakeRequest(json_body={"binding_id": 123})
    monkeypatch.setitem(sys.modules, "astrbot.api.web", module)
    monkeypatch.setattr(astrbot.api, "web", module, raising=False)

    bridge = bridge_module.PluginPagesBridge(plugin)
    view = bridge._wrap(bridge.identity_unbind)
    response = await view()
    assert response["kind"] == "error" and response["status"] == 404
