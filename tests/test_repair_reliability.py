import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def seed(plugin):
    plugin._cfg.profile_extraction_min_messages = 1
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "provider"
    plugin._insert_conversation_sync("u", "user", "用户偏好使用Python语言", "bot1", "u", "bot1:FriendMessage:u", "bot1|private", "a")


@pytest.mark.asyncio
async def test_failed_extraction_retained_and_retry_commits_once(plugin):
    seed(plugin)
    plugin.context = SimpleNamespace(llm_generate=AsyncMock(side_effect=RuntimeError("temporary")))
    assert await plugin._extract_profiles() == 0
    with plugin._db() as conn:
        row = conn.execute("SELECT distilled,processing_status,retry_count,next_retry_at FROM conversation_cache").fetchone()
        assert tuple(row)[:3] == (0, "retry", 1)
        conn.execute("UPDATE conversation_cache SET next_retry_at=0")
    plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text='{"profile_items":[{"facet_type":"preference","content":"用户偏好使用Python语言"}]}', usage=SimpleNamespace(input_other=20, input_cached=5, output=10)))
    assert await plugin._extract_profiles() == 1
    assert await plugin._extract_profiles() == 0
    with plugin._db() as conn:
        assert tuple(conn.execute("SELECT distilled,processing_status FROM conversation_cache").fetchone()) == (1, "success")
        assert conn.execute("SELECT COUNT(*) FROM profile_item_evidence").fetchone()[0] == 1
        assert conn.execute("SELECT tokens_total FROM llm_usage WHERE status='settled'").fetchone()[0] == 35


@pytest.mark.asyncio
async def test_retry_stops_after_three_retries(plugin):
    seed(plugin)
    plugin.context = SimpleNamespace(llm_generate=AsyncMock(side_effect=RuntimeError()))
    for _ in range(5):
        await plugin._extract_profiles()
        with plugin._db() as conn:
            conn.execute("UPDATE conversation_cache SET next_retry_at=0")
    assert plugin.context.llm_generate.await_count == 4
    with plugin._db() as conn:
        assert conn.execute("SELECT processing_status FROM conversation_cache").fetchone()[0] == "failed"


@pytest.mark.asyncio
async def test_finite_budget_blocks_concurrent_calls(plugin):
    from astrbot_plugin_tmemory.adapters.llm import generate
    from astrbot_plugin_tmemory.core.runtime_reliability import BudgetExceeded
    entered, resume = asyncio.Event(), asyncio.Event()
    async def provider(**kwargs):
        entered.set()
        await resume.wait()
        return SimpleNamespace(completion_text="ok", usage=SimpleNamespace(input_other=10, input_cached=0, output=5))
    plugin._cfg.daily_token_budget = 1000
    plugin.context = SimpleNamespace(llm_generate=provider)
    first = asyncio.create_task(generate(plugin, chat_provider_id="p", prompt="hello"))
    await entered.wait()
    with pytest.raises(BudgetExceeded):
        await generate(plugin, chat_provider_id="p", prompt="other")
    resume.set()
    await first
    from astrbot_plugin_tmemory.core.distill_validator import get_daily_token_usage
    assert get_daily_token_usage(plugin) == 15


def test_capacity_keeps_pinned_and_review_pending(plugin):
    from astrbot_plugin_tmemory.core.profile_ops import ProfileItemOps
    from astrbot_plugin_tmemory.core.runtime_reliability import retain
    ops = ProfileItemOps(plugin)
    ids = [ops.upsert_profile_item("u", "fact", "", f"用户长期关注不同领域{i}的知识", .8, .8) for i in range(5)]
    with plugin._db() as conn:
        conn.execute("UPDATE profile_items SET is_pinned=1 WHERE id=?", (ids[0],))
        conn.execute("UPDATE profile_items SET review_status='pending' WHERE id=?", (ids[1],))
    plugin._cfg.profile_max_items_per_user = 2
    retain(plugin, "u")
    with plugin._db() as conn:
        assert conn.execute("SELECT status FROM profile_items WHERE id=?", (ids[0],)).fetchone()[0] == "active"
        assert conn.execute("SELECT status FROM profile_items WHERE id=?", (ids[1],)).fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM profile_items WHERE status='active' AND review_status='approved'").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_cancelled_gateway_settles_reservation(plugin):
    from astrbot_plugin_tmemory.adapters.llm import generate
    entered = asyncio.Event()
    async def provider(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    plugin.context = SimpleNamespace(llm_generate=provider)
    call = asyncio.create_task(generate(plugin, chat_provider_id="p", prompt="test"))
    await entered.wait()
    for request in list(plugin._inflight_llm_tasks):
        request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not plugin._inflight_llm_tasks
    with plugin._db() as conn:
        assert conn.execute("SELECT status FROM llm_usage").fetchone()[0] == "failed"
