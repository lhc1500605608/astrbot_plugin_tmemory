"""Tests for the public read-only profile API ``get_profile_for_prompt`` (TMEAAA-522).

Covers:
- normal return shape (facets / summary / highlights / as_of)
- highlights sorted by importance and clipped to <=120 chars, count <= limit
- summary clipped to <=200 chars
- empty profile -> ``{}``
- group privacy isolation (private + cross-persona items excluded)
- uninitialized / disabled / exception -> ``{}`` (never raises)
"""

from datetime import datetime

import pytest

UMO_PRIVATE = "qq:FriendMessage:42"
UMO_GROUP = "qq:GroupMessage:1000"
CANONICAL = "qq:42"


def _seed_umo_cache(plugin, umo, canonical_id=CANONICAL, persona_id=""):
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO conversation_cache"
            "(canonical_user_id, role, content, unified_msg_origin, persona_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (canonical_id, "user", "hello", umo, persona_id, plugin._now()),
        )


def _seed_profile(
    plugin,
    facet,
    title,
    content,
    importance=0.8,
    confidence=0.8,
    source_scope="user",
    persona_id="",
):
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    return ProfileItemOps(plugin).upsert_profile_item(
        CANONICAL,
        facet,
        title,
        content,
        confidence,
        importance,
        source_scope=source_scope,
        persona_id=persona_id,
    )


def _seed_summary(plugin, text):
    with plugin._db() as conn:
        conn.execute(
            "UPDATE user_profiles SET summary_text=? WHERE canonical_user_id=?",
            (text, CANONICAL),
        )


@pytest.mark.asyncio
async def test_returns_expected_shape(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE)
    _seed_profile(plugin, "preference", "咖啡", "用户喜欢喝冰美式", importance=0.9)
    _seed_profile(plugin, "fact", "职业", "用户是一名软件工程师", importance=0.7)
    _seed_summary(plugin, "用户偏好咖啡，是一名软件工程师")

    result = await plugin.get_profile_for_prompt(UMO_PRIVATE)

    assert set(result.keys()) == {"facets", "summary", "highlights", "as_of"}
    assert result["facets"] == {"preference": 1, "fact": 1}
    assert result["summary"] == "用户偏好咖啡，是一名软件工程师"
    assert result["highlights"]
    datetime.fromisoformat(result["as_of"])


@pytest.mark.asyncio
async def test_highlights_sorted_and_clipped(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE)
    _seed_profile(plugin, "preference", "低", "低优先级内容", importance=0.2)
    _seed_profile(plugin, "preference", "高", "高优先级内容", importance=0.95)
    _seed_profile(plugin, "preference", "长", "长" * 300, importance=0.5)

    result = await plugin.get_profile_for_prompt(UMO_PRIVATE, limit=2)

    assert len(result["highlights"]) == 2
    assert "高优先级内容" in result["highlights"][0]
    assert all(len(h) <= 120 for h in result["highlights"])


@pytest.mark.asyncio
async def test_summary_clipped_to_200(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE)
    _seed_profile(plugin, "fact", "x", "事实内容")
    _seed_summary(plugin, "摘" * 500)

    result = await plugin.get_profile_for_prompt(UMO_PRIVATE)
    assert len(result["summary"]) <= 200


@pytest.mark.asyncio
async def test_empty_profile_returns_empty_dict(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE)
    assert await plugin.get_profile_for_prompt(UMO_PRIVATE) == {}


@pytest.mark.asyncio
async def test_group_excludes_private_and_cross_persona(plugin):
    _seed_umo_cache(plugin, UMO_GROUP, persona_id="p1")
    _seed_profile(plugin, "preference", "公开", "用户喜欢咖啡", persona_id="p1")
    _seed_profile(
        plugin, "preference", "私密", "用户私密偏好黑咖啡",
        source_scope="private", persona_id="p1",
    )
    _seed_profile(plugin, "fact", "他人", "另一个 persona 的秘密", persona_id="p2")

    result = await plugin.get_profile_for_prompt(UMO_GROUP, session_type="group")

    joined = "\n".join(result.get("highlights", []))
    assert "用户喜欢咖啡" in joined
    assert "黑咖啡" not in joined
    assert "另一个 persona 的秘密" not in joined


@pytest.mark.asyncio
async def test_private_scope_included_in_private_session(plugin):
    plugin._cfg.memory_scope = "session"
    _seed_umo_cache(plugin, UMO_PRIVATE)
    _seed_profile(
        plugin, "preference", "私密", "用户私密偏好黑咖啡", source_scope="private"
    )

    result = await plugin.get_profile_for_prompt(UMO_PRIVATE, session_type="private")
    assert any("黑咖啡" in h for h in result.get("highlights", []))


@pytest.mark.asyncio
async def test_uninitialized_returns_empty_dict(plugin):
    db_mgr = plugin._db_mgr
    plugin._db_mgr = None
    try:
        assert await plugin.get_profile_for_prompt(UMO_PRIVATE) == {}
    finally:
        plugin._db_mgr = db_mgr


@pytest.mark.asyncio
async def test_disabled_mode_returns_empty_dict(plugin):
    plugin._cfg.memory_mode = "distill_only"
    assert await plugin.get_profile_for_prompt(UMO_PRIVATE) == {}


@pytest.mark.asyncio
async def test_unresolved_umo_returns_empty_dict(plugin):
    _seed_profile(plugin, "fact", "x", "事实内容")
    assert await plugin.get_profile_for_prompt("qq:FriendMessage:99999") == {}


@pytest.mark.asyncio
async def test_exception_returns_empty_dict(plugin, monkeypatch):
    from astrbot_plugin_tmemory.core.admin_service import AdminService

    _seed_umo_cache(plugin, UMO_PRIVATE)
    _seed_profile(plugin, "fact", "x", "事实内容")

    def _boom(self, user):
        raise RuntimeError("boom")

    monkeypatch.setattr(AdminService, "get_profile_summary", _boom)
    assert await plugin.get_profile_for_prompt(UMO_PRIVATE) == {}
