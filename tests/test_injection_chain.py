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


# ──────────────────────────────────────────────────────────────────────────────
# Prefix-cache-friendly injection position tests  (TMEAAA-205)
# ──────────────────────────────────────────────────────────────────────────────


class DummyReq:
    """Minimal ProviderRequest stub for injection-position unit tests."""
    def __init__(self, prompt: str = "", system_prompt: str = ""):
        self.prompt = prompt
        self.system_prompt = system_prompt


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

    assert req.prompt.startswith("[用户记忆]"), "Memory must be prepended to user prompt"
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


# ── End-to-end: on_llm_request integration ────────────────────────────────────

@pytest.mark.asyncio
async def test_on_llm_request_injects_memory_after_static_prefix(plugin):
    """Full chain: on_llm_request appends memory after static system_prompt."""
    from tests.test_active_tool_mode_regression import DummyEvent

    # Insert a memory for the test identity
    _insert_memory(plugin, "qq:42", "用户喜欢喝咖啡")

    plugin._cfg.inject_position = "system_prompt"
    plugin._cfg.enable_memory_injection = True
    static_system = "你是AI助手，请用中文回复。"

    req = DummyReq(prompt="今天喝什么？", system_prompt=static_system)
    await plugin.on_llm_request(DummyEvent("今天喝什么？"), req)

    assert req.system_prompt.startswith(static_system), (
        "Static system prompt must remain at the start after full injection chain"
    )
    idx = req.system_prompt.find("[用户记忆]")
    assert idx >= len(static_system), (
        "Memory block must appear after static system prompt"
    )
    assert req.prompt == "今天喝什么？", "User prompt must be unchanged"

