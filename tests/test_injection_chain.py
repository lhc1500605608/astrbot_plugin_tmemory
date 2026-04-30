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


# ──────────────────────────────────────────────────────────────────────────────
# ADR TMEAAA-180 验收测试: 风格/记忆解耦注入
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_and_style_injection_coexist(plugin):
    """ADR TMEAAA-180: memory 注入与 style 注入可同时生效。"""
    plugin.context = _Context()
    plugin._cfg.enable_memory_injection = True
    plugin._cfg.enable_style_injection = True

    # 插入一条知识记忆（canonical_id 与 identity 解析一致）
    plugin._insert_memory(
        canonical_id="qq:42",
        adapter="qq",
        adapter_user="42",
        memory="用户喜欢吃火锅",
        score=0.8,
        memory_type="preference",
        importance=0.7,
        confidence=0.8,
    )

    # 创建风格档案并绑定
    profile_id = plugin._style_mgr.create_profile(
        "coexist-style", "请用热情的语气回复。", "qa profile"
    )
    plugin._style_mgr.set_binding("qq", "conv-style", profile_id)

    class _Request:
        prompt = "今天吃什么"
        system_prompt = "default persona"

    class _CoexistEvent:
        adapter_name = "qq"
        unified_msg_origin = "group:style"

        def get_sender_id(self):
            return "42"

        def get_group_id(self):
            return None

    req = _Request()
    event = _CoexistEvent()
    await plugin.on_llm_request(event, req)

    # 风格块应追加到 system_prompt 末尾
    assert "[人格档案]" in req.system_prompt
    assert "请用热情的语气回复。" in req.system_prompt
    # 知识记忆块应通过 inject_position 注入（默认 system_prompt，追加）
    assert "[用户记忆]" in req.system_prompt
    assert "用户喜欢吃火锅" in req.system_prompt


@pytest.mark.asyncio
async def test_style_injection_on_unbound_keeps_default_persona(plugin):
    """ADR TMEAAA-180: style_injection=ON 但未绑定时只保留默认人格。"""
    plugin.context = _Context()
    plugin._cfg.enable_style_injection = True

    class _Request:
        prompt = "你好"
        system_prompt = "default persona"

    class _UnboundEvent:
        adapter_name = "qq"
        unified_msg_origin = "group:nobind"

        def get_sender_id(self):
            return "99"

        def get_group_id(self):
            return None

    req = _Request()
    await plugin.on_llm_request(_UnboundEvent(), req)

    assert req.system_prompt == "default persona"
    assert "[人格档案]" not in req.system_prompt


@pytest.mark.asyncio
async def test_style_prompt_excludes_memory_content(plugin):
    """ADR TMEAAA-180: 风格注入块不得包含知识记忆内容。"""
    plugin.context = _Context()
    plugin._cfg.enable_memory_injection = True
    plugin._cfg.enable_style_injection = True

    # 插入一条与风格无关的知识记忆（canonical_id 与 identity 解析一致）
    plugin._insert_memory(
        canonical_id="qq:42",
        adapter="qq",
        adapter_user="42",
        memory="用户是Python后端工程师",
        score=0.8,
        memory_type="fact",
        importance=0.7,
        confidence=0.8,
    )

    profile_id = plugin._style_mgr.create_profile(
        "sep-style", "请用专业术语回复。", "qa profile"
    )
    plugin._style_mgr.set_binding("qq", "conv-style", profile_id)

    class _Request:
        prompt = "写段代码"
        system_prompt = "default persona"

    class _SepEvent:
        adapter_name = "qq"
        unified_msg_origin = "group:style"

        def get_sender_id(self):
            return "42"

        def get_group_id(self):
            return None

    req = _Request()
    await plugin.on_llm_request(_SepEvent(), req)

    style_start = req.system_prompt.find("[人格档案]")
    mem_start = req.system_prompt.find("[用户记忆]")
    assert style_start >= 0
    assert mem_start >= 0

    # 风格块不应包含知识记忆内容
    if style_start < mem_start:
        style_block = req.system_prompt[style_start:mem_start]
    else:
        style_block = req.system_prompt[style_start:]
    assert "Python后端工程师" not in style_block
    assert "用户记忆" not in style_block
