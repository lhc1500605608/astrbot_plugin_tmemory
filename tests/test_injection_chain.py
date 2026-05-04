"""Tests for the retrieval → injection chain.

Covers the bug where `_retrieve_memories` returned [] when query was non-empty
but FTS + vector search both produced zero hits, even though memories existed.
Expected: fall back to score-based retrieval so injection always has candidates.
"""

import sys

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
    even when FTS has no hits, so on_llm_request can inject profile items.
    """
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                "u4",
                "fact",
                "用户是一名软件工程师",
                "用户是一名软件工程师",
                0.8,
                0.7,
                0.5,
                now,
                now,
            ),
        )

    block = await plugin._build_knowledge_injection("u4", "XYZNOTFOUND", limit=5)
    assert block != "", (
        "_build_knowledge_injection returned empty string when profile items exist "
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


# ──────────────────────────────────────────────────────────────────────────────
# Prefix-cache-friendly injection position tests  (TMEAAA-205)
# ──────────────────────────────────────────────────────────────────────────────


class DummyReq:
    """Minimal ProviderRequest stub for injection-position unit tests."""

    def __init__(self, prompt: str = "", system_prompt: str = ""):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.extra_user_content_parts = []


class DummyEvent:
    def __init__(self, message_str: str):
        self.message_str = message_str
        self.adapter_name = "qq"

    def get_sender_id(self):
        return "42"

    def get_group_id(self):
        return None


# ── system_prompt mode (default) ──────────────────────────────────────────────


def test_inject_system_prompt_keeps_static_prefix_first(plugin):
    """Static system prompt stays at position 0; memory block appended after."""
    plugin._cfg.inject_position = "system_prompt"
    req = DummyReq(prompt="你好", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (preference) 用户喜欢咖啡")

    assert req.system_prompt.startswith("你是AI助手。"), (
        "Static system prompt must remain at the start"
    )
    idx = req.system_prompt.find("[用户记忆]")
    assert idx > len("你是AI助手。"), (
        "Memory block must appear after static system prompt"
    )


def test_inject_system_prompt_does_not_modify_user_prompt(plugin):
    """system_prompt injection must not touch req.prompt."""
    plugin._cfg.inject_position = "system_prompt"
    req = DummyReq(prompt="今天天气怎么样？", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户在北京")

    assert req.prompt == "今天天气怎么样？", "User prompt must be unchanged"
    assert "[用户记忆]" in req.system_prompt


# ── slot mode ─────────────────────────────────────────────────────────────────


def test_inject_slot_absent_appends_to_suffix(plugin):
    """When slot marker is absent, memory appends after static prefix."""
    plugin._cfg.inject_position = "slot"
    plugin._cfg.inject_slot_marker = "{{tmemory}}"
    req = DummyReq(system_prompt="你是AI助手，请用中文回复。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户是工程师")

    assert req.system_prompt.startswith("你是AI助手，请用中文回复。"), (
        "Static prefix must stay at start when slot marker is absent"
    )
    idx = req.system_prompt.find("[用户记忆]")
    assert idx > len("你是AI助手，请用中文回复。"), (
        "Memory block must appear after static prefix"
    )


def test_inject_slot_at_end_preserves_prefix(plugin):
    """Slot marker at end: memory goes after static prefix (cache-friendly)."""
    plugin._cfg.inject_position = "slot"
    plugin._cfg.inject_slot_marker = "{{tmemory}}"
    req = DummyReq(system_prompt="你是AI助手。\n{{tmemory}}")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (preference) 用户喜欢绿茶")

    assert req.system_prompt.startswith("你是AI助手。"), (
        "Static prefix must remain at start when slot is at end"
    )
    assert req.system_prompt.endswith("[用户记忆]\n- (preference) 用户喜欢绿茶"), (
        "Memory block must be at the end"
    )
    assert "{{tmemory}}" not in req.system_prompt, "Slot marker must be replaced"


def test_inject_slot_at_start_allows_user_explicit_choice(plugin):
    """Slot marker at start: user explicitly chose to break prefix cache."""
    plugin._cfg.inject_position = "slot"
    plugin._cfg.inject_slot_marker = "{{tmemory}}"
    req = DummyReq(system_prompt="{{tmemory}}\n你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户是学生")

    assert req.system_prompt.startswith("[用户记忆]"), (
        "When slot is at start, memory goes first (user's explicit choice)"
    )
    assert "你是AI助手。" in req.system_prompt
    assert "{{tmemory}}" not in req.system_prompt


# ── user_message modes (unchanged) ────────────────────────────────────────────


def test_inject_user_message_before_prepends_to_prompt(plugin):
    """user_message_before: memory block prepended to user prompt."""
    plugin._cfg.inject_position = "user_message_before"
    req = DummyReq(prompt="今天天气怎么样？", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户在北京")

    assert req.prompt.startswith("[用户记忆]"), (
        "Memory must be prepended to user prompt"
    )
    assert "今天天气怎么样？" in req.prompt
    assert req.system_prompt == "你是AI助手。", "System prompt must be unchanged"


def test_inject_user_message_after_appends_to_prompt(plugin):
    """user_message_after: memory block appended to user prompt."""
    plugin._cfg.inject_position = "user_message_after"
    req = DummyReq(prompt="今天天气怎么样？", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户在北京")

    assert req.prompt.startswith("今天天气怎么样？"), "User prompt must stay first"
    assert "[用户记忆]" in req.prompt
    assert req.prompt.endswith("[用户记忆]\n- (fact) 用户在北京"), (
        "Memory must be appended to user prompt"
    )
    assert req.system_prompt == "你是AI助手。", "System prompt must be unchanged"


# ── extra_user_temp mode ───────────────────────────────────────────────────────


def test_inject_extra_user_temp_appends_to_extra_parts(plugin):
    """Memory block is appended to req.extra_user_content_parts, not prompt/system."""
    plugin._cfg.inject_position = "extra_user_temp"
    req = DummyReq(prompt="今天天气怎么样？", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户在北京")

    assert len(req.extra_user_content_parts) == 1, (
        "Memory block must be appended to extra_user_content_parts"
    )
    part = req.extra_user_content_parts[0]
    assert "[用户记忆]" in part.text
    assert "用户在北京" in part.text


def test_inject_extra_user_temp_preserves_prompt_and_system(plugin):
    """extra_user_temp must not modify req.prompt or req.system_prompt."""
    plugin._cfg.inject_position = "extra_user_temp"
    req = DummyReq(prompt="你好", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (preference) 用户喜欢咖啡")

    assert req.prompt == "你好", "User prompt must be unchanged"
    assert req.system_prompt == "你是AI助手。", "System prompt must be unchanged"


def test_inject_extra_user_temp_part_has_temp_marking(plugin):
    """The appended part must carry a temporary marker so it is not saved to history."""
    plugin._cfg.inject_position = "extra_user_temp"
    req = DummyReq(prompt="测试", system_prompt="你是AI助手。")

    plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户是工程师")

    part = req.extra_user_content_parts[0]
    assert getattr(part, "_temp", False) is True, (
        "Part must be marked as temporary (mark_as_temp was called)"
    )


def test_inject_extra_user_temp_without_mark_as_temp_falls_back_to_system_prompt(
    plugin,
):
    """When mark_as_temp() is not available, fall back to system_prompt injection.

    Do NOT silently append to extra_user_content_parts as a regular part —
    that would violate the "don't write to history" semantic.
    """
    plugin._cfg.inject_position = "extra_user_temp"
    req = DummyReq(prompt="今天天气怎么样？", system_prompt="你是AI助手。")

    # Monkeypatch TextPart to a version WITHOUT mark_as_temp
    msg_mod = sys.modules["astrbot.core.agent.message"]
    original_text_part = msg_mod.TextPart

    class TextPartNoMark:
        def __init__(self, text: str):
            self.text = text
            self.type = "text"
            self._temp = False

    msg_mod.TextPart = TextPartNoMark

    try:
        plugin._inject_block_by_position(req, "[用户记忆]\n- (fact) 用户在北京")

        # Must NOT use extra_user_content_parts (no temp marking available)
        assert len(req.extra_user_content_parts) == 0, (
            "extra_user_content_parts must be empty when mark_as_temp is unavailable"
        )

        # Must fall back to system_prompt injection
        assert "[用户记忆]" in req.system_prompt, (
            "Memory block must be injected into system_prompt as fallback"
        )
        assert req.system_prompt.startswith("你是AI助手。"), (
            "Static system prompt must remain at the start in fallback"
        )

        # User prompt must be unchanged
        assert req.prompt == "今天天气怎么样？", "User prompt must be unchanged"
    finally:
        msg_mod.TextPart = original_text_part


# ── End-to-end: on_llm_request integration ────────────────────────────────────


@pytest.mark.asyncio
async def test_on_llm_request_injects_profile_after_static_prefix(plugin):
    """Full chain: on_llm_request appends profile block after static system_prompt."""
    # Insert a profile item for the test identity
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                "qq:42",
                "preference",
                "用户喜欢喝咖啡",
                "用户喜欢喝咖啡",
                0.8,
                0.7,
                0.5,
                now,
                now,
            ),
        )

    plugin._cfg.inject_position = "system_prompt"
    plugin._cfg.enable_memory_injection = True
    static_system = "你是AI助手，请用中文回复。"

    req = DummyReq(prompt="今天喝什么？", system_prompt=static_system)
    await plugin.on_llm_request(DummyEvent("今天喝什么？"), req)

    assert req.system_prompt.startswith(static_system), (
        "Static system prompt must remain at the start after full injection chain"
    )
    idx = req.system_prompt.find("[用户画像·偏好]")
    assert idx >= len(static_system), (
        "Profile block must appear after static system prompt"
    )
    assert req.prompt == "今天喝什么？", "User prompt must be unchanged"


# ──────────────────────────────────────────────────────────────────────────────
# Dual-channel summary separation tests  (TMEAAA-198)
# ──────────────────────────────────────────────────────────────────────────────


def _insert_typed_memory(
    plugin, canonical_id: str, memory: str, memory_type: str, score: float = 0.8
) -> int:
    return plugin._insert_memory(
        canonical_id=canonical_id,
        adapter="qq",
        adapter_user="42",
        memory=memory,
        score=score,
        memory_type=memory_type,
        importance=0.7,
        confidence=0.8,
    )


@pytest.mark.asyncio
async def test_style_memory_gets_persona_channel(plugin):
    """Style-type memories are automatically tagged as persona channel."""
    mid = _insert_typed_memory(plugin, "u-dc1", "用户沟通风格随意", memory_type="style")
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT summary_channel FROM memories WHERE id=?", (mid,)
        ).fetchone()
    assert row["summary_channel"] == "persona"


@pytest.mark.asyncio
async def test_fact_memory_gets_canonical_channel(plugin):
    """Non-style memories are automatically tagged as canonical channel."""
    mid = _insert_typed_memory(
        plugin, "u-dc2", "用户喜欢喝咖啡", memory_type="preference"
    )
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT summary_channel FROM memories WHERE id=?", (mid,)
        ).fetchone()
    assert row["summary_channel"] == "canonical"


@pytest.mark.asyncio
async def test_canonical_retrieval_excludes_persona_memories(plugin):
    """Retrieval with summary_channel='canonical' excludes style/persona memories."""
    _insert_typed_memory(plugin, "u-dc3", "用户喜欢爬山", memory_type="preference")
    _insert_typed_memory(
        plugin, "u-dc3", "用户沟通风格随意，常用'哈哈'", memory_type="style"
    )

    results = await plugin._retrieve_memories(
        "u-dc3", "爬山", limit=5, summary_channel="canonical"
    )
    memories = [r["memory"] for r in results]
    assert "用户喜欢爬山" in memories
    assert not any("风格" in m for m in memories), (
        "Persona channel memories must not appear in canonical search results"
    )


@pytest.mark.asyncio
async def test_persona_retrieval_only_returns_style_memories(plugin):
    """Retrieval with summary_channel='persona' only returns style memories."""
    _insert_typed_memory(plugin, "u-dc4", "用户是一名工程师", memory_type="fact")
    _insert_typed_memory(plugin, "u-dc4", "用户喜欢用emoji表达", memory_type="style")

    results = await plugin._retrieve_memories(
        "u-dc4", "", limit=5, summary_channel="persona"
    )
    memories = [r["memory"] for r in results]
    assert all(r["memory_type"] == "style" for r in results), (
        "Persona channel retrieval must only return style-type memories"
    )
    assert len(memories) >= 1


@pytest.mark.asyncio
async def test_build_knowledge_injection_dual_channel(plugin):
    """_build_knowledge_injection groups profile items by facet."""
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                "u-dc5",
                "preference",
                "用户喜欢喝绿茶",
                "用户喜欢喝绿茶",
                0.8,
                0.7,
                0.5,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                "u-dc5",
                "style",
                "用户沟通风格简短，常用'好的'回复",
                "用户沟通风格简短，常用'好的'回复",
                0.7,
                0.6,
                0.5,
                now,
                now,
            ),
        )

    block = await plugin._build_knowledge_injection("u-dc5", "绿茶", limit=5)
    assert "[用户画像·偏好]" in block, "Preference facet must appear in injection"
    assert "用户喜欢喝绿茶" in block
    assert "[用户画像·风格指导]" in block, "Style facet must appear in injection"
    assert "好的" in block


@pytest.mark.asyncio
async def test_build_knowledge_injection_style_only_no_query_pollution(plugin):
    """Style profile items are injected without query matching."""
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                "u-dc6",
                "style",
                "用户常用'哈哈'和emoji表达情绪",
                "用户常用'哈哈'和emoji表达情绪",
                0.7,
                0.6,
                0.5,
                now,
                now,
            ),
        )

    block = await plugin._build_knowledge_injection("u-dc6", "今天吃什么", limit=5)
    assert "[用户画像·风格指导]" in block
    # No preference/fact items exist for this user
    assert "[用户画像·偏好]" not in block
    assert "[用户画像·事实]" not in block


@pytest.mark.asyncio
async def test_build_knowledge_injection_no_memories_returns_empty(plugin):
    """No memories across either channel → empty injection block."""
    block = await plugin._build_knowledge_injection("u-dc-none", "anything", limit=5)
    assert block == ""
