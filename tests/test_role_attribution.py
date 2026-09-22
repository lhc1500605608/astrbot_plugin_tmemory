"""TMEAAA-457 回归：蒸馏 / 画像形成必须按 role 区分发言者。

验收：用户说 A、助手说 B → 蒸馏后 B 不得出现在用户记忆。
覆盖规则回退、LLM 蒸馏、手动命令、WebUI 手动蒸馏与画像提取路径。
"""

import pytest

from tests.test_distill_integration import _MockContextNoProvider, _MockLLMContext

USER_TEXT = "我平时喜欢喝黑咖啡不加糖"
ASSISTANT_TEXT = "我本人最爱吃三分熟牛排，还喜欢每周去攀岩。"


def _attribution():
    from astrbot_plugin_tmemory.core.attribution import (
        filter_assistant_attributed,
        is_assistant_attributed,
    )

    return filter_assistant_attributed, is_assistant_attributed


def _memories(plugin, user_id):
    return [m["memory"] for m in plugin._list_memories(user_id, 20)]


async def _seed(plugin, user_id):
    await plugin._insert_conversation(user_id, "user", USER_TEXT, "qq", "42", "group:1")
    await plugin._insert_conversation(
        user_id, "assistant", ASSISTANT_TEXT, "qq", "42", "group:1"
    )


# ── 纯函数单测 ──────────────────────────────────────────────────────────────


def test_attribution_unit_verbatim_and_composite(plugin_module):
    filter_assistant_attributed, is_assistant_attributed = _attribution()
    rows = [
        {"role": "user", "content": USER_TEXT},
        {"role": "assistant", "content": ASSISTANT_TEXT},
    ]
    assert is_assistant_attributed("三分熟牛排", ["我平时喜欢喝黑咖啡不加糖"], ["我本人最爱吃三分熟牛排"])
    assert not is_assistant_attributed("三分熟牛排", ["我最爱吃三分熟牛排"], ["我本人最爱吃三分熟牛排"])
    items = [
        {"memory": ASSISTANT_TEXT},
        {"memory": "用户喜欢黑咖啡"},
        {"memory": "记忆: " + USER_TEXT + " " + ASSISTANT_TEXT},
    ]
    kept = filter_assistant_attributed(items, rows)
    assert kept == [{"memory": "用户喜欢黑咖啡"}]


def test_attribution_keeps_everything_without_assistant_rows(plugin_module):
    filter_assistant_attributed, _ = _attribution()
    items = [{"memory": ASSISTANT_TEXT}]
    assert filter_assistant_attributed(items, [{"role": "user", "content": USER_TEXT}]) == items


# ── 自动蒸馏（规则回退） ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_distill_fallback_excludes_assistant_content(plugin):
    plugin.context = _MockContextNoProvider()
    plugin._cfg.distill_fallback_to_rules = True
    await _seed(plugin, "auto-fallback")

    await plugin._run_distill_cycle(force=True, trigger="auto")

    mems = _memories(plugin, "auto-fallback")
    assert mems
    assert any("黑咖啡" in m for m in mems)
    assert all("牛排" not in m and "攀岩" not in m for m in mems)


# ── 自动蒸馏（LLM 返回助手原话） ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_distill_llm_drops_assistant_attributed_memory(plugin):
    plugin.context = _MockLLMContext(
        '{"memories":[{"memory":"' + ASSISTANT_TEXT + '","memory_type":"preference",'
        '"importance":0.9,"confidence":0.9,"score":0.9}]}'
    )
    await _seed(plugin, "auto-llm")

    _users, created, _errs = await plugin._run_distill_cycle(force=True, trigger="auto")

    assert created == 0
    assert _memories(plugin, "auto-llm") == []


@pytest.mark.asyncio
async def test_auto_distill_llm_keeps_user_memory(plugin):
    plugin.context = _MockLLMContext(
        '{"memories":[{"memory":"用户偏好黑咖啡不加糖","memory_type":"preference",'
        '"importance":0.9,"confidence":0.9,"score":0.9}]}'
    )
    await _seed(plugin, "auto-llm-ok")

    _users, created, _errs = await plugin._run_distill_cycle(force=True, trigger="auto")

    assert created == 1
    assert "黑咖啡" in _memories(plugin, "auto-llm-ok")[0]


# ── 手动路径：/tm_distill_now 与 WebUI trigger_distill ──────────────────────


class _PlainEvent:
    def plain_result(self, text):
        return text


@pytest.mark.asyncio
async def test_manual_command_distill_excludes_assistant_content(plugin):
    plugin.context = _MockContextNoProvider()
    plugin._cfg.distill_fallback_to_rules = True
    await _seed(plugin, "manual-cmd")

    async for _ in plugin._handle_tm_distill_now(_PlainEvent()):
        pass

    mems = _memories(plugin, "manual-cmd")
    assert mems
    assert all("牛排" not in m and "攀岩" not in m for m in mems)


@pytest.mark.asyncio
async def test_webui_manual_distill_excludes_assistant_content(plugin):
    from astrbot_plugin_tmemory.core.admin_service import AdminService

    plugin.context = _MockContextNoProvider()
    plugin._cfg.distill_fallback_to_rules = True
    await _seed(plugin, "manual-web")

    result = await AdminService(plugin).trigger_distill()

    mems = _memories(plugin, "manual-web")
    assert any("黑咖啡" in m for m in mems)
    assert all("牛排" not in m and "攀岩" not in m for m in mems)
    assert result["pending_users_before"] >= 1


# ── 画像提取路径 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_extraction_drops_assistant_attributed_item(plugin):
    plugin.context = _MockLLMContext(
        '{"profile_items":[{"facet_type":"preference","title":"饮食",'
        '"content":"' + ASSISTANT_TEXT + '","importance":0.9,"confidence":0.9}]}'
    )
    plugin._cfg.profile_extraction_enabled = True
    await _seed(plugin, "profile-leak")

    await plugin._run_profile_extraction_cycle(force=True, trigger="auto")

    with plugin._db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM profile_items WHERE canonical_user_id=?",
            ("profile-leak",),
        ).fetchone()["n"]
    assert count == 0


# ── 存量清理：审计并停用误写的助手记忆 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_finds_and_deactivates_legacy_leak(plugin):
    from astrbot_plugin_tmemory.core.maintenance import (
        audit_assistant_attributed_memories,
    )

    plugin._insert_memory(
        canonical_id="legacy",
        adapter="qq",
        adapter_user="42",
        memory=ASSISTANT_TEXT,
        score=0.8,
        memory_type="preference",
        importance=0.7,
        confidence=0.8,
        source_channel="scheduled_distill",
    )
    await plugin._insert_conversation("legacy", "user", USER_TEXT, "qq", "42", "group:1")
    await plugin._insert_conversation("legacy", "assistant", ASSISTANT_TEXT, "qq", "42", "group:1")

    preview = audit_assistant_attributed_memories(plugin, canonical_id="legacy")
    assert len(preview["flagged"]) == 1

    applied = audit_assistant_attributed_memories(
        plugin, canonical_id="legacy", apply=True
    )
    assert applied["deactivated"] == 1
    assert _memories(plugin, "legacy") == []


# ── prompt 结构：助手区块被显式隔离 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_distill_prompt_separates_user_and_assistant_sections(plugin):
    captured = {}

    class _CapturingCtx(_MockLLMContext):
        async def llm_generate(self, **kwargs):
            captured["prompt"] = kwargs["prompt"]
            return await super().llm_generate(**kwargs)

    plugin.context = _CapturingCtx()
    await _seed(plugin, "prompt-shape")
    await plugin._run_distill_cycle(force=True, trigger="auto")

    prompt = captured["prompt"]
    assistant_section = "【助手发言（仅作上下文参考，禁止作为用户画像依据）】"
    assert "【用户发言（唯一可作为用户画像依据）】" in prompt
    assert assistant_section in prompt
    assert prompt.index(ASSISTANT_TEXT) > prompt.index(assistant_section)
    assert prompt.index(USER_TEXT) < prompt.index(assistant_section)
