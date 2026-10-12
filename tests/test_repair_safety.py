import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_batch_isolation(plugin):
    from astrbot_plugin_tmemory.core.data_safety import first_isolated_batch
    base = dict(canonical_user_id="u", source_adapter="bot1", scope="private", persona_id="p", unified_msg_origin="s")
    rows = [base, dict(base, scope="group:g"), dict(base, persona_id="q"), dict(base, source_adapter="bot2"), dict(base, unified_msg_origin="t")]
    assert first_isolated_batch(rows) == [base]


def test_unsafe_profile_is_quarantined(plugin):
    from astrbot_plugin_tmemory.core.profile_ops import ProfileItemOps
    assert ProfileItemOps(plugin).upsert_profile_item("u", "fact", "", "sk-" + "a" * 32, .8, .8) == 0
    with plugin._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_items").fetchone()[0] == 0
        text = conn.execute("SELECT content FROM memory_quarantine").fetchone()[0]
        assert "sk-" not in text


@pytest.mark.asyncio
async def test_purge_blocks_inflight_writer(plugin):
    from astrbot_plugin_tmemory.core.data_safety import generation_guard
    from astrbot_plugin_tmemory.core.maintenance import purge_user_data
    from astrbot_plugin_tmemory.core.profile_ops import ProfileItemOps
    ready, resume = asyncio.Event(), asyncio.Event()

    async def writer():
        with generation_guard(plugin, "u"):
            ready.set()
            await resume.wait()
            ProfileItemOps(plugin).upsert_profile_item("u", "fact", "", "用户偏好使用Python", .8, .8)

    task = asyncio.create_task(writer())
    await ready.wait()
    purge_user_data(plugin, "u")
    resume.set()
    with pytest.raises(RuntimeError, match="purged"):
        await task
    with plugin._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] == 0


def test_nested_transaction_rolls_back(plugin):
    with pytest.raises(ValueError):
        with plugin._db() as conn:
            conn.execute("INSERT INTO user_generations VALUES('u',1)")
            with plugin._db() as nested:
                nested.execute("INSERT INTO user_generations VALUES('v',1)")
            raise ValueError("abort")
    with plugin._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM user_generations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_purify_cannot_modify_other_user(plugin):
    from astrbot_plugin_tmemory.core.maintenance import run_memory_purify
    a = plugin._insert_memory("u1", "bot", "u1", "用户偏好使用Python", .8, "preference", .8, .8)
    b = plugin._insert_memory("u2", "bot", "u2", "用户偏好使用Rust语言", .8, "preference", .8, .8)
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET is_pinned=1 WHERE id=?", (b,))
    plugin._cfg.purify_min_score = 0
    plugin._cfg.distill_provider_id = "provider"
    plugin.context = SimpleNamespace(llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text=f'{{"deactivate":[{b}]}}')))
    await run_memory_purify(plugin)
    with plugin._db() as conn:
        assert conn.execute("SELECT is_active FROM memories WHERE id=?", (b,)).fetchone()[0] == 1
