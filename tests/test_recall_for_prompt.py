"""Tests for the public read-only recall API ``recall_for_prompt`` (TMEAAA-396).

Covers:
- private session includes ``scope='private'`` memories
- group session excludes private memories unless ``private_memory_in_group``
- persona isolation (persona_id + generic '')
- exception path returns ``[]`` (never raises)
- disabled / uninitialized paths return ``[]``
- entry truncation to <=200 chars
"""

import pytest

UMO_PRIVATE = "qq:FriendMessage:42"
UMO_GROUP = "qq:GroupMessage:1000"


def _seed_umo_cache(plugin, umo, canonical_id, persona_id=""):
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO conversation_cache"
            "(canonical_user_id, role, content, unified_msg_origin, persona_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (canonical_id, "user", "hello", umo, persona_id, plugin._now()),
        )


def _seed_memory(
    plugin, canonical_id, text, scope="user", persona_id="", memory_type="fact"
):
    return plugin._insert_memory(
        canonical_id=canonical_id,
        adapter="qq",
        adapter_user="42",
        memory=text,
        score=0.9,
        memory_type=memory_type,
        importance=0.9,
        confidence=0.9,
        scope=scope,
        persona_id=persona_id,
    )


@pytest.mark.asyncio
async def test_private_recall_includes_private_memory(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE, "qq:42")
    _seed_memory(plugin, "qq:42", "用户喜欢喝咖啡（冰美式）", memory_type="fact")
    _seed_memory(plugin, "qq:42", "用户私密偏好是黑咖啡", scope="private", memory_type="preference")

    result = await plugin.recall_for_prompt(
        UMO_PRIVATE, "咖啡", session_type="private"
    )
    joined = "\n".join(result)
    assert "冰美式" in joined
    assert "黑咖啡" in joined


@pytest.mark.asyncio
async def test_group_recall_excludes_private_memory(plugin):
    _seed_umo_cache(plugin, UMO_GROUP, "qq:42")
    _seed_memory(plugin, "qq:42", "用户喜欢喝咖啡（冰美式）", memory_type="fact")
    _seed_memory(plugin, "qq:42", "用户私密偏好是黑咖啡", scope="private", memory_type="preference")

    result = await plugin.recall_for_prompt(UMO_GROUP, "咖啡", session_type="group")
    joined = "\n".join(result)
    assert "冰美式" in joined
    assert "黑咖啡" not in joined


@pytest.mark.asyncio
async def test_group_recall_includes_private_when_configured(plugin):
    plugin._cfg.private_memory_in_group = True
    _seed_umo_cache(plugin, UMO_GROUP, "qq:42")
    _seed_memory(plugin, "qq:42", "用户私密偏好是黑咖啡", scope="private", memory_type="preference")

    result = await plugin.recall_for_prompt(UMO_GROUP, "咖啡", session_type="group")
    assert any("黑咖啡" in r for r in result)


@pytest.mark.asyncio
async def test_persona_isolation(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE, "qq:42", persona_id="p1")
    _seed_memory(plugin, "qq:42", "宠物记录：用户的狗叫旺财", persona_id="p1", memory_type="fact")
    _seed_memory(plugin, "qq:42", "宠物记录：用户更喜欢猫咪", persona_id="p2", memory_type="preference")
    _seed_memory(plugin, "qq:42", "宠物记录：用户想领养宠物", persona_id="", memory_type="task")

    result = await plugin.recall_for_prompt(UMO_PRIVATE, "宠物")
    joined = "\n".join(result)
    assert "旺财" in joined
    assert "领养" in joined
    assert "猫咪" not in joined


@pytest.mark.asyncio
async def test_recall_does_not_mutate_memory(plugin):
    """只读契约：召回不得写 reinforce_count / attention_score。"""
    _seed_umo_cache(plugin, UMO_PRIVATE, "qq:42")
    mid = _seed_memory(plugin, "qq:42", "用户喜欢喝咖啡（冰美式）", memory_type="fact")

    with plugin._db() as conn:
        before = conn.execute(
            "SELECT reinforce_count, attention_score FROM memories WHERE id=?", (mid,)
        ).fetchone()

    result = await plugin.recall_for_prompt(UMO_PRIVATE, "咖啡")
    assert result

    with plugin._db() as conn:
        after = conn.execute(
            "SELECT reinforce_count, attention_score FROM memories WHERE id=?", (mid,)
        ).fetchone()
    assert int(after["reinforce_count"]) == int(before["reinforce_count"])
    assert float(after["attention_score"]) == float(before["attention_score"])


@pytest.mark.asyncio
async def test_exception_returns_empty_list(plugin, monkeypatch):
    _seed_umo_cache(plugin, UMO_PRIVATE, "qq:42")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "_retrieve_memories", _boom)
    result = await plugin.recall_for_prompt(UMO_PRIVATE, "咖啡")
    assert result == []


@pytest.mark.asyncio
async def test_disabled_mode_returns_empty_list(plugin):
    plugin._cfg.memory_mode = "distill_only"
    result = await plugin.recall_for_prompt(UMO_PRIVATE, "咖啡")
    assert result == []


@pytest.mark.asyncio
async def test_unresolved_umo_returns_empty_list(plugin):
    result = await plugin.recall_for_prompt("qq:FriendMessage:99999", "咖啡")
    assert result == []


@pytest.mark.asyncio
async def test_empty_query_returns_empty_list(plugin):
    assert await plugin.recall_for_prompt(UMO_PRIVATE, "") == []


@pytest.mark.asyncio
async def test_items_truncated_to_200_chars(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE, "qq:42")
    _seed_memory(plugin, "qq:42", "长" * 500)

    result = await plugin.recall_for_prompt(UMO_PRIVATE, "长")
    assert result
    assert all(len(item) <= 200 for item in result)


@pytest.mark.asyncio
async def test_returns_plain_text_list(plugin):
    _seed_umo_cache(plugin, UMO_PRIVATE, "qq:42")
    _seed_memory(plugin, "qq:42", "用户喜欢喝咖啡（冰美式）", memory_type="fact")

    result = await plugin.recall_for_prompt(UMO_PRIVATE, "咖啡")
    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)
