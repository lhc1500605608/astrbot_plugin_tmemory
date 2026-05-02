"""Reproduction tests for TMEAAA-243: WebUI manual distill false empty result."""

import asyncio
import types

import pytest


@pytest.fixture()
def plugin_with_ctx(tmp_path, monkeypatch, plugin_module):
    """Same as test_stability_fixes plugin_with_ctx fixture."""
    monkeypatch.chdir(tmp_path)

    class _MockContext:
        async def llm_generate(self, **kwargs):
            raise NotImplementedError("subclass must override")

        async def get_current_chat_provider_id(self, **kwargs):
            return None

    ctx = _MockContext()
    instance = plugin_module.TMemoryPlugin(context=ctx, config={})
    instance._init_db()
    instance._migrate_schema()
    yield instance, ctx
    instance._close_db()


# ── Scenario A: all rows prefiltered (low-info "嗯"/"哦") ────────────────────

@pytest.mark.asyncio
async def test_distill_all_rows_prefiltered_returns_processed_users_gt_0(
    plugin_with_ctx,
):
    """When all rows are prefiltered by _prefilter_distill_rows,
    processed_users should be > 0 (rows are skipped but the user IS processed)."""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="low-info-user",
        role="user",
        content="嗯",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )
    await plugin._insert_conversation(
        canonical_id="low-info-user",
        role="assistant",
        content="哦",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    # Verify pending shows user
    pending = plugin._pending_distill_users(limit=100, min_batch_count=1)
    assert "low-info-user" in pending, f"Expected low-info-user in pending, got {pending}"

    rows = plugin._fetch_pending_rows("low-info-user", 80)
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"

    filtered = plugin._prefilter_distill_rows(rows)
    # "嗯" and "哦" may or may not be filtered depending on jieba tokenization
    # We just report what happens
    print(f"\nFiltered rows: {len(filtered)} out of {len(rows)}")

    async def fake_llm(**kwargs):
        raise AssertionError("LLM should not be called when all rows prefiltered")

    ctx.llm_generate = fake_llm
    plugin._cfg.distill_provider_id = "mock"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories = await plugin._run_distill_cycle(
        force=True, trigger="test-prefilter-all"
    )

    print(f"processed_users={processed_users}, total_memories={total_memories}")
    # Key assertion: processed_users should be >= 1 even if all rows filtered
    assert processed_users >= 1, (
        f"BUG: processed_users={processed_users} when pending queue was non-empty. "
        f"This is the false-empty scenario."
    )
    assert total_memories == 0


# ── Scenario B: non-empty pending but _pending_distill_users returns empty ───

@pytest.mark.asyncio
async def test_pending_distill_users_matches_admin_get_pending(plugin_with_ctx):
    """_pending_distill_users(min_batch_count=1) should see the same users
    as AdminService.get_pending()."""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="user-a",
        role="user",
        content="我喜欢喝咖啡不加糖",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )
    await plugin._insert_conversation(
        canonical_id="user-b",
        role="user",
        content="明天要开会",
        source_adapter="qq",
        source_user_id="2",
        unified_msg_origin="group:1",
    )

    # Via AdminService.get_pending
    from core.admin_service import AdminService
    admin = AdminService(plugin)
    admin_pending = admin.get_pending()
    admin_users = {p["user"] for p in admin_pending}
    print(f"\nAdminService.get_pending users: {admin_users}")

    # Via _pending_distill_users (force mode equivalent)
    distill_users = set(plugin._pending_distill_users(limit=100, min_batch_count=1))
    print(f"_pending_distill_users users: {distill_users}")

    assert distill_users == admin_users, (
        f"MISMATCH: distill_users={distill_users} vs admin_users={admin_users}"
    )


# ── Scenario C: LLM returns empty memories but provider is configured ───────

@pytest.mark.asyncio
async def test_distill_llm_returns_empty_json_but_rule_fallback_saves(plugin_with_ctx):
    """When LLM returns empty memories array, rule-based fallback should still
    produce a memory and processed_users should be > 0."""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="empty-llm-user",
        role="user",
        content="我喜欢黑咖啡不加糖，每周三晚上练羽毛球",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
    )

    fake_resp = types.SimpleNamespace(
        completion_text='{"memories": []}',  # LLM returns no memories
        usage=types.SimpleNamespace(input_other=10, input_cached=0, output=2),
    )

    async def fake_llm(**kwargs):
        return fake_resp

    ctx.llm_generate = fake_llm
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories = await plugin._run_distill_cycle(
        force=True, trigger="test-empty-llm"
    )

    print(f"processed_users={processed_users}, total_memories={total_memories}")
    # LLM returned empty, but rule fallback should produce at least 1 memory
    assert processed_users >= 1, (
        f"BUG: processed_users={processed_users} — false empty when LLM returns empty"
    )
    # Rule-based fallback should produce something
    # With meaningful content, distill_text should extract keywords
    assert total_memories >= 1, f"Expected >=1 memories, got {total_memories}"


# ── Scenario D: all valid memories filtered by _validate_distill_output ─────

@pytest.mark.asyncio
async def test_distill_all_memories_invalidated_still_counts_processed_user(
    plugin_with_ctx,
):
    """When all LLM output items fail validation, processed_users should still be > 0."""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="junk-user",
        role="user",
        content="hello world this is a test message with enough content",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    # LLM returns memories that will all fail validation (too short, junk)
    fake_resp = types.SimpleNamespace(
        completion_text='{"memories": [{"memory": "hi", "memory_type": "fact", "importance": 0.8, "confidence": 0.8, "score": 0.8}]}',
        usage=types.SimpleNamespace(input_other=10, input_cached=0, output=5),
    )

    async def fake_llm(**kwargs):
        return fake_resp

    ctx.llm_generate = fake_llm
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories = await plugin._run_distill_cycle(
        force=True, trigger="test-junk-memory"
    )

    print(f"processed_users={processed_users}, total_memories={total_memories}")
    assert processed_users >= 1, (
        f"BUG: processed_users={processed_users} when all memories invalidated"
    )
    assert total_memories == 0  # "hi" is too short (< 6 chars)


# ── Scenario E: AdminService.trigger_distill path ───────────────────────────

@pytest.mark.asyncio
async def test_admin_service_trigger_distill_with_pending_data(plugin_with_ctx):
    """AdminService.trigger_distill() should return non-zero processed_users
    when pending data exists."""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="admin-test-user",
        role="user",
        content="我每天早上七点起床跑步",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
    )

    from core.admin_service import AdminService

    admin = AdminService(plugin)

    # Check pending via AdminService
    pending = admin.get_pending()
    print(f"\nAdminService pending: {pending}")
    assert len(pending) > 0, "Expected pending users"

    # Mock LLM for distill
    fake_resp = types.SimpleNamespace(
        completion_text='{"memories": [{"memory": "用户每天早上七点起床跑步", "memory_type": "preference", "importance": 0.8, "confidence": 0.9, "score": 0.85}]}',
        usage=types.SimpleNamespace(input_other=50, input_cached=0, output=20),
    )

    async def fake_llm(**kwargs):
        return fake_resp

    ctx.llm_generate = fake_llm
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.distill_model_id = "mock-model"
    plugin._cfg.use_independent_distill_model = True

    result = await admin.trigger_distill()
    print(f"trigger_distill result: {result}")

    assert result["processed_users"] >= 1, (
        f"BUG: AdminService trigger_distill processed_users={result['processed_users']}"
    )
    assert result["total_memories"] >= 1, (
        f"BUG: AdminService trigger_distill total_memories={result['total_memories']}"
    )


# ── Scenario F: trigger_distill with no pending ─────────────────────────────

@pytest.mark.asyncio
async def test_admin_service_trigger_distill_with_empty_pending(plugin_with_ctx):
    """AdminService.trigger_distill() should return {0, 0} when no pending data."""
    plugin, ctx = plugin_with_ctx

    from core.admin_service import AdminService
    admin = AdminService(plugin)

    # With no data inserted, pending should be empty
    pending = admin.get_pending()
    print(f"\nEmpty pending: {pending}")
    assert len(pending) == 0

    result = await admin.trigger_distill()
    print(f"trigger_distill result (empty): {result}")

    assert result["processed_users"] == 0
    assert result["total_memories"] == 0
    # This is the CORRECT case for the empty message
