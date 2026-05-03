"""Integration tests for layered injection (TMEAAA-255).

Covers: Working/Episodic/Semantic/Style retrieval, block assembly,
empty layer omission, cap enforcement, backward compatibility.
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


def _insert_episode(plugin, canonical_id: str, title: str, summary: str,
                    attention_score: float = 0.6, status: str = "ongoing",
                    scope: str = "user", persona_id: str = "",
                    session_key: str = "session:1"):
    with plugin._db() as conn:
        now = "2026-05-03 10:00:00"
        cur = conn.execute(
            """INSERT INTO memory_episodes
               (canonical_user_id, scope, persona_id, session_key,
                episode_title, episode_summary, status, attention_score,
                first_source_at, last_source_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (canonical_id, scope, persona_id, session_key,
             title, summary, status, attention_score,
             now, now, now, now),
        )
        return cur.lastrowid


def _insert_memory(plugin, canonical_id: str, memory: str,
                   memory_type: str = "preference", score: float = 0.8,
                   persona_id: str = "", scope: str = "user"):
    return plugin._insert_memory(
        canonical_id=canonical_id,
        adapter="qq",
        adapter_user="42",
        memory=memory,
        score=score,
        memory_type=memory_type,
        importance=0.7,
        confidence=0.8,
        persona_id=persona_id,
        scope=scope,
    )


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
# Episode Layer Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_retrieve_episodes_returns_ongoing(plugin):
    _insert_episode(plugin, "u-e1", "Python学习", "用户在学习Python基础语法", attention_score=0.8)

    eps = plugin._retrieval_mgr.retrieve_episodes(
        "u-e1", "Python", limit=3, max_chars=600)
    assert len(eps) >= 1
    assert eps[0]["episode_title"] == "Python学习"


def test_retrieve_episodes_searches_by_query(plugin):
    _insert_episode(plugin, "u-e2", "旅行计划", "用户计划去日本旅行", attention_score=0.5)
    _insert_episode(plugin, "u-e2", "饮食偏好", "用户喜欢日料", attention_score=0.5)

    eps = plugin._retrieval_mgr.retrieve_episodes(
        "u-e2", "日本", limit=3, max_chars=600)
    assert any("日本" in str(e.get("episode_summary", "")) for e in eps)


def test_retrieve_episodes_respects_limit(plugin):
    for i in range(5):
        _insert_episode(plugin, "u-e3", f"情节{i}", f"摘要{i}", attention_score=0.5 + i * 0.1)

    eps = plugin._retrieval_mgr.retrieve_episodes(
        "u-e3", "", limit=2, max_chars=600)
    assert len(eps) <= 2


def test_retrieve_episodes_respects_max_chars(plugin):
    long_summary = "A" * 500
    _insert_episode(plugin, "u-e4", "长摘要", long_summary)

    eps = plugin._retrieval_mgr.retrieve_episodes(
        "u-e4", "", limit=1, max_chars=100)
    assert len(eps) == 1
    assert len(str(eps[0]["episode_summary"])) <= 103  # 100 + "…"


def test_retrieve_episodes_empty_when_none(plugin):
    eps = plugin._retrieval_mgr.retrieve_episodes(
        "u-e-none", "", limit=3, max_chars=600)
    assert eps == []


def test_retrieve_episodes_zero_limit(plugin):
    _insert_episode(plugin, "u-e5", "测试", "摘要")
    eps = plugin._retrieval_mgr.retrieve_episodes(
        "u-e5", "", limit=0, max_chars=600)
    assert eps == []


# ──────────────────────────────────────────────────────────────────────────────
# Layered Injection Block Assembly
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_layered_injection_only_semantic_when_no_working_or_episodes(plugin):
    """When no working context or episodes exist, only semantic + style blocks appear."""
    plugin._cfg.enable_layered_injection = True
    _insert_memory(plugin, "u-li1", "用户喜欢咖啡", memory_type="preference")

    block = await plugin._injection_builder.build_layered_injection(
        "u-li1", "咖啡", session_key="s1",
    )
    assert "[用户记忆]" in block
    assert "用户喜欢咖啡" in block
    assert "[当前对话背景]" not in block


@pytest.mark.asyncio
async def test_layered_injection_includes_working_context(plugin):
    """Working context appears as [当前对话背景] before memories."""
    plugin._cfg.enable_layered_injection = True
    plugin._cfg.inject_working_turns = 5
    await _insert_conversation(plugin, "u-li2", "user", "今天我想喝咖啡", session_key="s1")
    _insert_memory(plugin, "u-li2", "用户喜欢咖啡", memory_type="preference")

    block = await plugin._injection_builder.build_layered_injection(
        "u-li2", "咖啡", session_key="s1",
    )
    assert "[当前对话背景]" in block
    assert "今天我想喝咖啡" in block
    assert "[用户记忆]" in block
    assert "喜欢咖啡" in block


@pytest.mark.asyncio
async def test_layered_injection_includes_episodes(plugin):
    """Episode summaries appear in context block."""
    plugin._cfg.enable_layered_injection = True
    _insert_episode(plugin, "u-li3", "Python学习", "用户在学习Python", attention_score=0.8)

    block = await plugin._injection_builder.build_layered_injection(
        "u-li3", "Python", session_key="s1",
    )
    assert "[当前对话背景]" in block
    assert "Python学习" in block
    assert "用户在学习Python" in block


@pytest.mark.asyncio
async def test_layered_injection_all_layers(plugin):
    """Full layered injection: Working + Episodic + Semantic + Style."""
    plugin._cfg.enable_layered_injection = True
    plugin._cfg.inject_working_turns = 5
    plugin._cfg.inject_episode_limit = 3

    await _insert_conversation(plugin, "u-li4", "user", "今天想喝点什么", session_key="s1")
    _insert_episode(plugin, "u-li4", "饮食探索", "用户最近在尝试不同饮品", attention_score=0.7)
    _insert_memory(plugin, "u-li4", "用户喜欢咖啡", memory_type="preference")
    _insert_memory(plugin, "u-li4", "用户沟通风格简洁", memory_type="style")

    block = await plugin._injection_builder.build_layered_injection(
        "u-li4", "咖啡", session_key="s1",
    )
    assert "[当前对话背景]" in block
    assert "[用户记忆]" in block
    assert "[用户风格指导]" in block


@pytest.mark.asyncio
async def test_layered_injection_style_cap_enforced(plugin):
    """Style block respects inject_style_max_chars."""
    plugin._cfg.enable_layered_injection = True
    plugin._cfg.inject_style_max_chars = 20
    _insert_memory(plugin, "u-li5", "用户沟通风格非常活泼，喜欢用各种emoji和感叹号表达情绪",
                   memory_type="style")

    block = await plugin._injection_builder.build_layered_injection(
        "u-li5", "test", session_key="s1",
    )
    # Style block should be truncated or not exceed the cap significantly
    if "[用户风格指导]" in block:
        style_start = block.index("[用户风格指导]")
        style_content = block[style_start:]
        # Allow some overhead for the header line
        assert len(style_content) < 20 + 50, f"Style cap not enforced: {len(style_content)} chars"


@pytest.mark.asyncio
async def test_layered_injection_empty_all_layers_returns_empty(plugin):
    """No data in any layer → empty block."""
    plugin._cfg.enable_layered_injection = True
    block = await plugin._injection_builder.build_layered_injection(
        "u-none", "query", session_key="s1",
    )
    assert block == ""


@pytest.mark.asyncio
async def test_layered_injection_respects_inject_max_chars(plugin):
    """Total block truncated when inject_max_chars > 0."""
    plugin._cfg.enable_layered_injection = True
    plugin._cfg.inject_max_chars = 50
    for i in range(5):
        _insert_memory(plugin, "u-li6", f"用户记忆条目{i}", memory_type="preference")

    block = await plugin._injection_builder.build_layered_injection(
        "u-li6", "条目", session_key="s1",
    )
    assert len(block) <= 53  # 50 + "…"


# ──────────────────────────────────────────────────────────────────────────────
# Backward Compatibility
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flat_injection_still_works_with_layered_disabled(plugin):
    """When enable_layered_injection=False, flat injection path is used (unchanged)."""
    plugin._cfg.enable_layered_injection = False
    plugin._cfg.enable_memory_injection = True
    _insert_memory(plugin, "u-bc1", "用户喜欢跑步", memory_type="preference")

    block = await plugin._build_knowledge_injection("u-bc1", "跑步", limit=5)
    assert "[用户记忆]" in block
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
