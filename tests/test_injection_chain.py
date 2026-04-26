"""Tests for the retrieval → injection chain.

Covers the bug where `_retrieve_memories` returned [] when query was non-empty
but FTS + vector search both produced zero hits, even though memories existed.
Expected: fall back to score-based retrieval so injection always has candidates.
"""

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _insert_memory(plugin, canonical_id: str, memory: str, score: float = 0.8) -> int:
    return plugin._insert_memory(
        canonical_id=canonical_id,
        adapter="qq",
        adapter_user="42",
        memory=memory,
        score=score,
        memory_type="preference",
        importance=0.7,
        confidence=0.8,
    )


class _StyleEvent:
    adapter_name = "qq"
    unified_msg_origin = "group:style"

    def get_sender_id(self):
        return "42"

    def get_group_id(self):
        return None


class _ConversationManager:
    async def get_curr_conversation_id(self, _umo):
        return "conv-style"


class _Context:
    conversation_manager = _ConversationManager()


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retrieve_memories_with_empty_query_returns_candidates(plugin):
    """Empty query → score-based fallback always used (existing behaviour)."""
    _insert_memory(plugin, "u1", "用户喜欢跑步")
    results = await plugin._retrieve_memories("u1", "", limit=5)
    assert len(results) >= 1
    assert all("memory" in r for r in results)


@pytest.mark.asyncio
async def test_retrieve_memories_fts_miss_still_returns_results(plugin):
    """Query is non-empty but FTS returns no hits (unique tokens not in index).

    Before the fix: _retrieve_memories returned [] immediately.
    After the fix : falls back to score-based retrieval, returns stored memories.
    """
    _insert_memory(plugin, "u2", "用户偏好早睡早起")

    # Query with tokens unlikely to match stored memory via FTS
    # (gibberish prefix ensures FTS MATCH returns nothing)
    query = "XYZNOTFOUND"
    results = await plugin._retrieve_memories("u2", query, limit=5)
    # Must still return the stored memory via score-based fallback
    assert len(results) >= 1, (
        "Injection chain broken: _retrieve_memories returned [] when FTS found no hits "
        "but memories exist. Expected score-based fallback."
    )
    assert results[0]["memory"] == "用户偏好早睡早起"


@pytest.mark.asyncio
async def test_retrieve_memories_fts_hit_returns_relevant_results(plugin):
    """Query matches FTS — normal hybrid path still works correctly."""
    _insert_memory(plugin, "u3", "用户喜欢喝绿茶")

    results = await plugin._retrieve_memories("u3", "绿茶", limit=5)
    assert len(results) >= 1
    memories = [r["memory"] for r in results]
    assert "用户喜欢喝绿茶" in memories


@pytest.mark.asyncio
async def test_build_injection_block_fts_miss_still_produces_block(plugin):
    """End-to-end: _build_knowledge_injection must produce a non-empty string
    even when FTS has no hits, so on_llm_request can inject memories.
    """
    _insert_memory(plugin, "u4", "用户是一名软件工程师")

    block = await plugin._build_knowledge_injection("u4", "XYZNOTFOUND", limit=5)
    assert block != "", (
        "_build_knowledge_injection returned empty string when memories exist "
        "and FTS missed — injection would be silently skipped."
    )
    assert "用户是一名软件工程师" in block


@pytest.mark.asyncio
async def test_retrieve_memories_no_memories_returns_empty(plugin):
    """No memories for user → always return empty list regardless of query."""
    results = await plugin._retrieve_memories("u-nobody", "anything", limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_retrieve_memories_respects_limit(plugin):
    """Result count must not exceed limit even in fallback path."""
    for i in range(10):
        _insert_memory(plugin, "u5", f"用户记忆条目{i}")

    results = await plugin._retrieve_memories("u5", "XYZNOTFOUND", limit=3)
    assert len(results) <= 3


@pytest.mark.asyncio
async def test_style_injection_returns_empty_without_binding(plugin):
    plugin.context = _Context()
    plugin._cfg.enable_style_injection = True

    block = await plugin._build_style_injection(
        "qq:42", "随便聊聊", _StyleEvent(), scope="user", persona_id=""
    )

    assert block == ""


@pytest.mark.asyncio
async def test_style_injection_uses_bound_prompt_supplement(plugin):
    plugin.context = _Context()
    plugin._cfg.enable_style_injection = True
    profile_id = plugin._style_mgr.create_profile(
        "concise-style", "请用简洁直接的语气回复。", "qa profile"
    )
    plugin._style_mgr.set_binding("qq", "conv-style", profile_id)

    block = await plugin._build_style_injection(
        "qq:42", "随便聊聊", _StyleEvent(), scope="user", persona_id=""
    )

    assert "[人格档案]" in block
    assert "请用简洁直接的语气回复。" in block


@pytest.mark.asyncio
async def test_style_injection_off_keeps_default_persona(plugin):
    plugin.context = _Context()
    plugin._cfg.enable_memory_injection = False
    plugin._cfg.enable_style_injection = False
    profile_id = plugin._style_mgr.create_profile(
        "bound-style", "这段不应注入。", "qa profile"
    )
    plugin._style_mgr.set_binding("qq", "conv-style", profile_id)

    class _Request:
        prompt = "你好"
        system_prompt = "default persona"

    req = _Request()
    await plugin.on_llm_request(_StyleEvent(), req)

    assert req.system_prompt == "default persona"
