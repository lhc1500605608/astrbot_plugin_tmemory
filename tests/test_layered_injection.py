"""Integration tests for profile-aware injection (TMEAAA-284).

Covers: working context retrieval, profile-item block assembly with facet grouping,
empty facet omission, cap enforcement.
"""

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _insert_conversation(plugin, canonical_id: str, role: str, content: str,
                                session_key: str = "session:1", scope: str = "user",
                                persona_id: str = ""):
    await plugin._insert_conversation(
        canonical_id=canonical_id,
        role=role,
        content=content,
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin=session_key,
        scope=scope,
        persona_id=persona_id,
    )


def _insert_profile_item(plugin, canonical_id: str, content: str,
                         facet_type: str = "preference", confidence: float = 0.8,
                         importance: float = 0.7, stability: float = 0.5,
                         persona_id: str = "", scope: str = "user"):
    now = plugin._now()
    with plugin._db() as conn:
        cur = conn.execute(
            """INSERT INTO profile_items
               (canonical_user_id, facet_type, content, normalized_content,
                status, confidence, importance, stability,
                created_at, updated_at, persona_id, source_scope)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
            (canonical_id, facet_type, content, content,
             confidence, importance, stability,
             now, now, persona_id, scope),
        )
        return cur.lastrowid


class DummyReq:
    def __init__(self, prompt: str = "", system_prompt: str = ""):
        self.prompt = prompt
        self.system_prompt = system_prompt


# ──────────────────────────────────────────────────────────────────────────────
# Working Layer Tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retrieve_working_context_returns_turns_chronological(plugin):
    await _insert_conversation(plugin, "u-w1", "user", "我喜欢咖啡", session_key="s1")
    await _insert_conversation(plugin, "u-w1", "assistant", "好的，记住了", session_key="s1")
    await _insert_conversation(plugin, "u-w1", "user", "再来一杯", session_key="s1")

    turns = plugin._retrieval_mgr.retrieve_working_context("u-w1", "s1", limit=5)
    assert len(turns) == 3
    assert turns[0]["content"] == "我喜欢咖啡"
    assert turns[1]["content"] == "好的，记住了"
    assert turns[2]["content"] == "再来一杯"


@pytest.mark.asyncio
async def test_retrieve_working_context_respects_limit(plugin):
    for i in range(10):
        await _insert_conversation(plugin, "u-w2", "user", f"消息{i}", session_key="s1")

    turns = plugin._retrieval_mgr.retrieve_working_context("u-w2", "s1", limit=3)
    assert len(turns) == 3


def test_retrieve_working_context_empty_session(plugin):
    turns = plugin._retrieval_mgr.retrieve_working_context("u-w3", "nonexistent", limit=5)
    assert turns == []


@pytest.mark.asyncio
async def test_retrieve_working_context_zero_limit(plugin):
    await _insert_conversation(plugin, "u-w4", "user", "hello", session_key="s1")
    turns = plugin._retrieval_mgr.retrieve_working_context("u-w4", "s1", limit=0)
    assert turns == []


@pytest.mark.asyncio
async def test_retrieve_working_context_empty_session_key(plugin):
    await _insert_conversation(plugin, "u-w5", "user", "hello", session_key="s1")
    turns = plugin._retrieval_mgr.retrieve_working_context("u-w5", "", limit=5)
    assert turns == []


# ──────────────────────────────────────────────────────────────────────────────
# Profile Injection Block Assembly
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_profile_injection_only_profile_when_no_working_context(plugin):
    """When no working context exists, only profile blocks appear."""
    _insert_profile_item(plugin, "u-pi1", "用户喜欢咖啡", facet_type="preference")

    block = await plugin._injection_builder.build_profile_injection(
        "u-pi1", "咖啡", session_key="s1",
    )
    assert "[用户画像·偏好]" in block
    assert "用户喜欢咖啡" in block
    assert "[当前对话]" not in block


@pytest.mark.asyncio
async def test_profile_injection_includes_working_context(plugin):
    """Working context appears as [当前对话] before profile blocks."""
    plugin._cfg.inject_working_turns = 5
    await _insert_conversation(plugin, "u-pi2", "user", "今天我想喝咖啡", session_key="s1")
    _insert_profile_item(plugin, "u-pi2", "用户喜欢咖啡", facet_type="preference")

    block = await plugin._injection_builder.build_profile_injection(
        "u-pi2", "咖啡", session_key="s1",
    )
    assert "[当前对话]" in block
    assert "今天我想喝咖啡" in block
    assert "[用户画像·偏好]" in block
    assert "喜欢咖啡" in block


@pytest.mark.asyncio
async def test_profile_injection_groups_multiple_facets(plugin):
    """Profile items are grouped by facet type with correct headings."""
    _insert_profile_item(plugin, "u-pi3", "用户是程序员", facet_type="fact")
    _insert_profile_item(plugin, "u-pi3", "用户喜欢安静的环境", facet_type="preference")
    _insert_profile_item(plugin, "u-pi3", "用户不吃辣", facet_type="restriction")

    block = await plugin._injection_builder.build_profile_injection(
        "u-pi3", "", session_key="s1",
    )
    assert "[用户画像·限制]" in block
    assert "[用户画像·偏好]" in block
    assert "[用户画像·事实]" in block
    assert "不吃辣" in block
    assert "安静" in block
    assert "程序员" in block


@pytest.mark.asyncio
async def test_profile_injection_empty_when_no_data(plugin):
    """No profile items and no working context → empty block."""
    block = await plugin._injection_builder.build_profile_injection(
        "u-none", "query", session_key="s1",
    )
    assert block == ""


@pytest.mark.asyncio
async def test_profile_injection_respects_inject_max_chars(plugin):
    """Total block truncated when inject_max_chars > 0."""
    plugin._cfg.inject_max_chars = 60
    for i in range(5):
        _insert_profile_item(plugin, "u-pi4", f"用户记忆条目{i}", facet_type="preference")

    block = await plugin._injection_builder.build_profile_injection(
        "u-pi4", "条目", session_key="s1",
    )
    assert len(block) <= 63  # 60 + "…"


# ──────────────────────────────────────────────────────────────────────────────
# Backward Compat: build_layered_injection alias
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_layered_injection_alias_works(plugin):
    """build_layered_injection delegates to build_profile_injection."""
    _insert_profile_item(plugin, "u-bc1", "用户喜欢跑步", facet_type="preference")

    block = await plugin._injection_builder.build_layered_injection(
        "u-bc1", "跑步", session_key="s1",
    )
    assert "[用户画像·偏好]" in block
    assert "用户喜欢跑步" in block


# ──────────────────────────────────────────────────────────────────────────────
# InjectionBuilder.inject_block_by_position (static method, both paths)
# ──────────────────────────────────────────────────────────────────────────────

def test_injection_builder_position_system_prompt():
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder

    req = DummyReq(prompt="你好", system_prompt="你是AI助手。")
    InjectionBuilder.inject_block_by_position(req, "[BLOCK]", "system_prompt", "")
    assert req.system_prompt.startswith("你是AI助手。")
    assert "[BLOCK]" in req.system_prompt


def test_injection_builder_position_slot():
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder

    req = DummyReq(system_prompt="你是AI助手。{{slot}}")
    InjectionBuilder.inject_block_by_position(req, "[BLOCK]", "slot", "{{slot}}")
    assert "{{slot}}" not in req.system_prompt
    assert "[BLOCK]" in req.system_prompt


def test_injection_builder_position_user_before():
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder

    req = DummyReq(prompt="今天天气如何？")
    InjectionBuilder.inject_block_by_position(req, "[BLOCK]", "user_message_before", "")
    assert req.prompt.startswith("[BLOCK]")


def test_injection_builder_position_user_after():
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder

    req = DummyReq(prompt="今天天气如何？")
    InjectionBuilder.inject_block_by_position(req, "[BLOCK]", "user_message_after", "")
    assert req.prompt.endswith("[BLOCK]")
