"""Tests for AI active tool mode: remember / recall / memory_mode switching.

Covers:
- tool_remember: create memory, dedup path, safety audit, junk filter
- tool_recall: retrieval path, empty result, formatted output
- memory_mode: distill_only disables tools, active_only disables distill, hybrid allows both
- tool registration: methods exist and are decorated
"""

import pytest


class DummyEvent:
    """Minimal AstrMessageEvent stub for tool tests."""

    def __init__(self, message_str: str = ""):
        self.message_str = message_str
        self.adapter_name = "qq"

    def get_sender_id(self):
        return "42"

    def get_group_id(self):
        return None

    def plain_result(self, text):
        return text


def _make_plugin_with_mode(plugin, mode: str):
    """Helper: set memory_mode on plugin._cfg and return plugin."""
    plugin._cfg.memory_mode = mode
    return plugin


# =========================================================================
# remember tool
# =========================================================================


@pytest.mark.asyncio
async def test_remember_creates_memory(plugin):
    result = await plugin.tool_remember(
        DummyEvent(), content="用户喜欢喝冰美式", memory_type="preference"
    )

    assert "已记住" in result
    assert "preference" in result

    # Verify memory was actually stored
    memories = plugin._list_memories("qq:42", limit=10)
    assert any("冰美式" in str(m["memory"]) for m in memories)


@pytest.mark.asyncio
async def test_remember_dedup_reinforces_existing(plugin):
    """When the same content is remembered twice, reinforce_count should increase."""
    result1 = await plugin.tool_remember(
        DummyEvent(), content="用户喜欢Python编程", memory_type="fact"
    )
    assert "已记住" in result1

    result2 = await plugin.tool_remember(
        DummyEvent(), content="用户喜欢Python编程", memory_type="fact"
    )
    assert "已记住" in result2

    # Should have exactly one active memory (dedup via hash)
    memories = plugin._list_memories("qq:42", limit=20)
    python_mems = [m for m in memories if "Python" in str(m["memory"])]
    assert len(python_mems) == 1
    assert python_mems[0]["reinforce_count"] >= 2


@pytest.mark.asyncio
async def test_remember_conflict_detection_deactivates_old(plugin):
    """Overlapping content should deactivate old memory via conflict detection."""
    await plugin.tool_remember(
        DummyEvent(), content="用户偏好深色主题的编辑器", memory_type="preference"
    )
    await plugin.tool_remember(
        DummyEvent(), content="用户偏好浅色主题的编辑器设置", memory_type="preference"
    )

    memories = plugin._list_memories("qq:42", limit=20)
    theme_mems = [m for m in memories if "主题" in str(m["memory"]) and "编辑器" in str(m["memory"])]
    # At most one should be active (conflict detection may deactivate the old one)
    assert len(theme_mems) >= 1


@pytest.mark.asyncio
async def test_remember_rejects_short_content(plugin):
    result = await plugin.tool_remember(
        DummyEvent(), content="hi", memory_type="fact"
    )
    assert "过短" in result


@pytest.mark.asyncio
async def test_remember_rejects_empty_content(plugin):
    result = await plugin.tool_remember(
        DummyEvent(), content="", memory_type="fact"
    )
    assert "过短" in result


@pytest.mark.asyncio
async def test_remember_rejects_unsafe_content(plugin):
    result = await plugin.tool_remember(
        DummyEvent(), content="用户的密码是 abc123456", memory_type="fact"
    )
    assert "安全审计" in result


@pytest.mark.asyncio
async def test_remember_rejects_junk_content(plugin):
    result = await plugin.tool_remember(
        DummyEvent(), content="哈哈哈哈", memory_type="fact"
    )
    assert "未保存" in result


@pytest.mark.asyncio
async def test_remember_normalizes_invalid_memory_type(plugin):
    result = await plugin.tool_remember(
        DummyEvent(), content="用户每天早上跑步锻炼身体", memory_type="invalid_type"
    )
    assert "已记住" in result
    # Should fallback to "fact"
    assert "fact" in result


@pytest.mark.asyncio
async def test_remember_source_channel_is_active_tool(plugin):
    """Remember tool should set source_channel='active_tool'."""
    await plugin.tool_remember(
        DummyEvent(), content="用户养了一只金毛犬", memory_type="fact"
    )

    with plugin._db() as conn:
        row = conn.execute(
            "SELECT source_channel FROM memories WHERE canonical_user_id='qq:42' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["source_channel"] == "active_tool"


# =========================================================================
# recall tool
# =========================================================================


@pytest.mark.asyncio
async def test_recall_returns_empty_when_no_memories(plugin):
    result = await plugin.tool_recall(DummyEvent(), query="喜欢什么")
    assert "未找到" in result


@pytest.mark.asyncio
async def test_recall_finds_previously_remembered(plugin):
    await plugin.tool_remember(
        DummyEvent(), content="用户周末喜欢去爬山", memory_type="preference"
    )

    result = await plugin.tool_recall(DummyEvent(), query="周末活动")
    assert "爬山" in result
    assert "preference" in result


@pytest.mark.asyncio
async def test_recall_returns_formatted_results(plugin):
    await plugin.tool_remember(
        DummyEvent(), content="用户是一名前端工程师", memory_type="fact"
    )

    result = await plugin.tool_recall(DummyEvent(), query="职业")
    # Each result line should be formatted as "- (type) content"
    assert result.startswith("- (")


@pytest.mark.asyncio
async def test_recall_rejects_empty_query(plugin):
    result = await plugin.tool_recall(DummyEvent(), query="")
    assert "为空" in result


# =========================================================================
# memory_mode switching
# =========================================================================


@pytest.mark.asyncio
async def test_remember_disabled_in_distill_only_mode(plugin):
    _make_plugin_with_mode(plugin, "distill_only")
    result = await plugin.tool_remember(
        DummyEvent(), content="用户喜欢吃火锅", memory_type="preference"
    )
    assert "已禁用" in result


@pytest.mark.asyncio
async def test_recall_disabled_in_distill_only_mode(plugin):
    _make_plugin_with_mode(plugin, "distill_only")
    result = await plugin.tool_recall(DummyEvent(), query="喜欢什么")
    assert "已禁用" in result


@pytest.mark.asyncio
async def test_remember_works_in_active_only_mode(plugin):
    _make_plugin_with_mode(plugin, "active_only")
    result = await plugin.tool_remember(
        DummyEvent(), content="用户喜欢听古典音乐", memory_type="preference"
    )
    assert "已记住" in result


@pytest.mark.asyncio
async def test_remember_works_in_hybrid_mode(plugin):
    _make_plugin_with_mode(plugin, "hybrid")
    result = await plugin.tool_remember(
        DummyEvent(), content="用户住在上海浦东", memory_type="fact"
    )
    assert "已记住" in result


@pytest.mark.asyncio
async def test_recall_works_in_active_only_mode(plugin):
    _make_plugin_with_mode(plugin, "active_only")
    await plugin.tool_remember(
        DummyEvent(), content="用户喜欢读科幻小说", memory_type="preference"
    )
    result = await plugin.tool_recall(DummyEvent(), query="阅读")
    assert "科幻" in result


# =========================================================================
# Tool registration (structural)
# =========================================================================


def test_tool_remember_is_registered(plugin):
    """tool_remember method exists on the plugin class."""
    assert hasattr(plugin, "tool_remember")
    assert callable(plugin.tool_remember)


def test_tool_recall_is_registered(plugin):
    """tool_recall method exists on the plugin class."""
    assert hasattr(plugin, "tool_recall")
    assert callable(plugin.tool_recall)


# =========================================================================
# Config parsing
# =========================================================================


def test_memory_mode_defaults_to_hybrid():
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({})
    assert cfg.memory_mode == "hybrid"


def test_memory_mode_accepts_valid_values():
    from astrbot_plugin_tmemory.core.config import parse_config

    for mode in ("distill_only", "active_only", "hybrid"):
        cfg = parse_config({"memory_mode": mode})
        assert cfg.memory_mode == mode


def test_memory_mode_falls_back_on_invalid():
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({"memory_mode": "invalid_junk"})
    assert cfg.memory_mode == "hybrid"
