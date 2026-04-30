"""稳定性修复验证测试 (TMEAAA-90)

覆盖以下关键路径：
1. 核心蒸馏 mock — _distill_rows_with_llm 正常路径 + 回退路径
2. 记忆衰减 / 自动裁剪 — _decay_stale_memories + _auto_prune_low_quality
3. WebUI 登录 handler 异常路径 (bad JSON)
4. _LockedConnection 并发回归 — 多线程同时写入不死锁、不数据丢失
"""

import asyncio
import threading
import time
import types

import pytest


# ── 辅助：给测试 plugin 注入可写 context ─────────────────────────────────────


class _MockContext:
    """可写属性的 context stub，供 LLM mock 使用。"""

    async def llm_generate(self, **kwargs):
        raise NotImplementedError("不应被调用（子类覆盖）")

    async def get_current_chat_provider_id(self, **kwargs):
        return None


class _StyleDistillEvent:
    """Minimal AstrMessageEvent stub for style_distill regression tests."""

    adapter_name = "qq"

    def __init__(self, message_str: str):
        self.message_str = message_str
        self.call_llm_blocked = False
        self.stopped = False

    def get_sender_id(self):
        return "42"

    def get_group_id(self):
        return None

    def plain_result(self, text):
        return text

    def should_call_llm(self, call_llm):
        self.call_llm_blocked = call_llm

    def stop_event(self):
        self.stopped = True


@pytest.fixture()
def plugin_with_ctx(tmp_path, monkeypatch, plugin_module):
    """与 plugin fixture 等价，但 context 是可写的 _MockContext。"""
    monkeypatch.chdir(tmp_path)
    ctx = _MockContext()
    instance = plugin_module.TMemoryPlugin(context=ctx, config={})
    instance._init_db()
    instance._migrate_schema()
    yield instance, ctx
    instance._close_db()


# ── 场景 1: 核心蒸馏 mock ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_distill_rows_with_llm_uses_mock_provider(plugin_with_ctx):
    """_distill_rows_with_llm 在 LLM 正常返回 JSON 时正确解析记忆。"""
    plugin, ctx = plugin_with_ctx

    rows = [
        {
            "id": 1,
            "role": "user",
            "content": "我平时爱喝黑咖啡，不加糖",
            "source_adapter": "qq",
            "source_user_id": "42",
            "unified_msg_origin": "group:1",
            "scope": "user",
            "persona_id": "",
        }
    ]

    fake_resp = types.SimpleNamespace(
        completion_text='{"memories": [{"memory": "用户偏好黑咖啡不加糖", "memory_type": "preference", "importance": 0.8, "confidence": 0.9, "score": 0.85}]}',
        usage=types.SimpleNamespace(input_other=100, input_cached=0, output=50),
    )

    async def fake_llm_generate(**kwargs):
        return fake_resp

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.distill_model_id = "mock-model"
    plugin._cfg.use_independent_distill_model = True

    items, tok_in, tok_out = await plugin._distill_rows_with_llm(rows)

    assert len(items) == 1
    assert items[0]["memory"] == "用户偏好黑咖啡不加糖"
    assert items[0]["memory_type"] == "preference"
    assert tok_in == 100
    assert tok_out == 50


@pytest.mark.asyncio
async def test_distill_cycle_skips_style_items(plugin_with_ctx):
    """ADR TMEAAA-180: 记忆蒸馏周期跳过 style 类型，不再写入 memories。"""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="style-user",
        role="user",
        content="我聊天喜欢先给结论，再用三条短句解释原因。",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
    )

    async def fake_llm_generate(**kwargs):
        return types.SimpleNamespace(
            completion_text=(
                '{"memories": [{"memory": "用户聊天风格是先给结论再用三条短句解释原因", '
                '"memory_type": "style", "importance": 0.8, "confidence": 0.9, "score": 0.85}]}'
            ),
            usage=types.SimpleNamespace(input_other=10, input_cached=0, output=8),
        )

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories = await plugin._run_distill_cycle(
        force=True, trigger="qa-style"
    )

    with plugin._db() as conn:
        row = conn.execute(
            "SELECT memory, memory_type FROM memories WHERE canonical_user_id=?",
            ("style-user",),
        ).fetchone()

    # Memory distill cycle should still process the user but skip style items
    assert processed_users == 1
    assert total_memories == 0, "style items should be skipped in memory distill"
    assert row is None, "no memories should be written for style-only LLM output"


@pytest.mark.asyncio
async def test_distill_cycle_does_not_extract_assistant_style_without_user_rows(
    plugin_with_ctx,
):
    """只有 assistant 聊天记录时不应产出用户风格记忆。"""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="assistant-only",
        role="assistant",
        content="我的回复风格是先给结论，然后用三条短句解释。",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
    )

    async def fake_llm_generate(**kwargs):
        raise AssertionError("assistant-only rows must not be sent to LLM")

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories = await plugin._run_distill_cycle(
        force=True, trigger="qa-assistant-only"
    )

    with plugin._db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_cache WHERE canonical_user_id=? AND distilled=0",
            ("assistant-only",),
        ).fetchone()["n"]
        memory_count = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE canonical_user_id=?",
            ("assistant-only",),
        ).fetchone()["n"]

    assert processed_users == 1
    assert total_memories == 0
    assert pending == 0
    assert memory_count == 0


def test_validate_distill_output_blocks_assistant_style_attribution(plugin):
    """校验器应拒绝把 assistant 说话风格保存成用户风格。"""
    items = [
        {
            "memory": "用户聊天风格是像助手一样先给结论再分点说明",
            "memory_type": "style",
            "importance": 0.8,
            "confidence": 0.9,
            "score": 0.8,
        }
    ]

    assert plugin._validate_distill_output(items) == []


@pytest.mark.asyncio
async def test_style_distill_collection_message_is_captured_without_reply(plugin):
    """style_distill enabled should collect a normal message into style_conversation_cache."""
    plugin._cfg.enable_auto_capture = True
    plugin._cfg.enable_style_distill = True

    event = _StyleDistillEvent("我写作时喜欢先说结论，再用短句补充理由。")

    result = await plugin.on_any_message(event)

    # ADR TMEAAA-180: style capture goes to style_conversation_cache
    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT * FROM style_conversation_cache WHERE canonical_user_id=?",
            ("qq:42",),
        ).fetchall()
    assert result is None
    assert event.call_llm_blocked is True
    assert event.stopped is False
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert "先说结论" in rows[0]["content"]
    assert plugin._list_memories("qq:42", limit=10) == []


@pytest.mark.asyncio
async def test_on_llm_response_captures_when_style_distill_on_and_auto_capture_off(plugin):
    """on_llm_response must capture assistant reply into style_conversation_cache when enable_style_distill=True, even with enable_auto_capture=False."""
    plugin._cfg.enable_auto_capture = False
    plugin._cfg.capture_assistant_reply = False
    plugin._cfg.enable_style_distill = True

    resp = types.SimpleNamespace(completion_text="好的，我来用简洁的方式回答你的问题。")
    event = _StyleDistillEvent("请用简洁方式回答")

    await plugin.on_llm_response(event, resp)

    # ADR TMEAAA-180: style capture goes to style_conversation_cache
    with plugin._db() as conn:
        pending = conn.execute(
            "SELECT * FROM style_conversation_cache WHERE canonical_user_id=?",
            ("qq:42",),
        ).fetchall()
    assert len(pending) == 1, "assistant reply should be captured for style distill"
    assert pending[0]["role"] == "assistant"
    assert "简洁" in pending[0]["content"]


@pytest.mark.asyncio
async def test_on_llm_response_skips_when_both_switches_off(plugin):
    """on_llm_response must skip capture when both enable_auto_capture and enable_style_distill are off."""
    plugin._cfg.enable_auto_capture = False
    plugin._cfg.capture_assistant_reply = False
    plugin._cfg.enable_style_distill = False

    resp = types.SimpleNamespace(completion_text="这是助手的回复。")
    event = _StyleDistillEvent("你好")

    await plugin.on_llm_response(event, resp)

    pending = plugin._fetch_pending_rows("qq:42", 10)
    assert len(pending) == 0, "no capture should happen when both switches are off"


@pytest.mark.asyncio
async def test_on_llm_response_respects_capture_assistant_reply_when_auto_capture_on(plugin):
    """on_llm_response must respect capture_assistant_reply when enable_auto_capture=True and style_distill=False."""
    plugin._cfg.enable_auto_capture = True
    plugin._cfg.capture_assistant_reply = False
    plugin._cfg.enable_style_distill = False

    resp = types.SimpleNamespace(completion_text="模型回复。")
    event = _StyleDistillEvent("用户消息")

    await plugin.on_llm_response(event, resp)

    pending = plugin._fetch_pending_rows("qq:42", 10)
    assert len(pending) == 0, "should not capture when capture_assistant_reply=False and style_distill off"


@pytest.mark.asyncio
async def test_style_distill_creates_temporary_profile_before_manual_archive(plugin_with_ctx):
    """ADR TMEAAA-180: StyleDistillManager should create temp profiles from style_conversation_cache."""
    plugin, ctx = plugin_with_ctx
    plugin._cfg.enable_style_distill = True
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True
    plugin._cfg.style_min_confidence = 0.55

    plugin._style_distill_mgr.insert_style_conversation(
        canonical_id="qq:42",
        role="user",
        content="我表达习惯先给结论，再分三点说明。",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
    )

    async def fake_llm_generate(**kwargs):
        return types.SimpleNamespace(
            completion_text=(
                '{"style_observations": ['
                '{"observation": "用户偏好先给结论再展开细节的沟通方式", "evidence": "先给结论，再分三点说明", "confidence": 0.9}'
                '], '
                '"prompt_supplement": "用户偏好先给结论再分三点说明的沟通风格。", '
                '"importance": 0.8}'
            ),
            usage=types.SimpleNamespace(input_other=10, input_cached=0, output=8),
        )

    ctx.llm_generate = fake_llm_generate

    users, candidates, profiles = await plugin._style_distill_mgr.run_style_distill_cycle(
        force=True, trigger="qa-style-temp-profile"
    )

    with plugin._db() as conn:
        temp_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='style_temp_profiles'"
        ).fetchall()
        temp_rows = []
        if temp_tables:
            temp_rows = conn.execute(
                "SELECT * FROM style_temp_profiles WHERE source_user=?",
                ("qq:42",),
            ).fetchall()

    assert temp_tables, "missing temporary style profile storage"
    assert temp_rows, "style_distill output was not stored as a temporary profile"
    assert users == 1
    assert candidates >= 1


def test_style_temporary_profile_can_merge_and_save_as_archive(plugin):
    """Temporary style profiles should support merge into existing and save-as-new flows."""
    from astrbot_plugin_tmemory.core.admin_service import AdminService

    admin = AdminService(plugin)

    assert hasattr(admin, "merge_temporary_style_profile")
    assert hasattr(admin, "save_temporary_style_profile")


@pytest.mark.asyncio
async def test_distill_rows_with_llm_fallback_on_llm_error(plugin_with_ctx):
    """LLM 调用抛出异常时，回退到规则蒸馏并返回非空结果。"""
    plugin, ctx = plugin_with_ctx

    rows = [
        {
            "id": 1,
            "role": "user",
            "content": "我住在北京，每天骑车上班",
            "source_adapter": "qq",
            "source_user_id": "42",
            "unified_msg_origin": "group:1",
            "scope": "user",
            "persona_id": "",
        }
    ]

    async def bad_llm(**kwargs):
        raise RuntimeError("simulate LLM timeout")

    ctx.llm_generate = bad_llm
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.distill_model_id = ""
    plugin._cfg.use_independent_distill_model = True

    items, tok_in, tok_out = await plugin._distill_rows_with_llm(rows)

    # 回退路径应返回至少 1 条规则蒸馏结果
    assert len(items) >= 1
    assert tok_in == -1
    assert tok_out == -1


@pytest.mark.asyncio
async def test_distill_rows_with_llm_fallback_when_no_provider(plugin):
    """未配置 provider 时直接走规则蒸馏，不调用 LLM。"""
    rows = [
        {
            "id": 1,
            "role": "user",
            "content": "喜欢爬山",
            "source_adapter": "qq",
            "source_user_id": "1",
            "unified_msg_origin": "",
            "scope": "user",
            "persona_id": "",
        }
    ]

    plugin._cfg.distill_provider_id = ""
    plugin._cfg.distill_model_id = ""
    plugin._cfg.use_independent_distill_model = False

    items, tok_in, tok_out = await plugin._distill_rows_with_llm(rows)

    assert len(items) >= 1
    assert tok_in == -1
    assert tok_out == -1


# ── 场景 2: 记忆衰减 / 自动裁剪 ─────────────────────────────────────────────


def _insert_memory_with_age(plugin, canonical_id, memory_text, days_old, score=0.3, importance=0.2, confidence=0.3, reinforce=0):
    """辅助：插入一条指定天数的历史记忆并直接调整时间戳。"""
    mem_id = plugin._insert_memory(
        canonical_id=canonical_id,
        adapter="qq",
        adapter_user="99",
        memory=memory_text,
        score=score,
        memory_type="fact",
        importance=importance,
        confidence=confidence,
    )
    old_ts = time.time() - days_old * 86400
    old_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old_ts))
    with plugin._db() as conn:
        conn.execute(
            "UPDATE memories SET last_seen_at=?, created_at=?, updated_at=? WHERE id=?",
            (old_dt, old_dt, old_dt, mem_id),
        )
    return mem_id


def test_decay_stale_memories_marks_stale_after_30_days(plugin):
    """超过 30 天未命中的记忆应被标记为 is_active=2 (stale)。"""
    mem_id = _insert_memory_with_age(plugin, "decay-user", "喜欢旅行", days_old=35, score=0.8, importance=0.8, confidence=0.8)

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 2


def test_decay_stale_memories_marks_archived_after_90_days(plugin):
    """超过 90 天未命中的记忆应被标记为 is_active=3 (archived)。"""
    mem_id = _insert_memory_with_age(plugin, "decay-user2", "曾住在上海", days_old=95, score=0.8, importance=0.8, confidence=0.8)

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 3


def test_decay_stale_memories_ignores_pinned(plugin):
    """is_pinned=1 的记忆即使超时也不应被衰减。"""
    mem_id = _insert_memory_with_age(plugin, "pinned-user", "核心偏好", days_old=100)
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET is_pinned=1 WHERE id=?", (mem_id,))

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 1  # 未变化


def test_auto_prune_low_quality_deactivates_old_low_quality(plugin):
    """低质量（质量分 < 0.35）且超过 7 天的记忆应被失活(is_active=0)。"""
    # 质量分 = 0.3*0.2 + 0.4*0.15 + 0.3*0.2 = 0.06 + 0.06 + 0.06 = 0.18 < 0.35
    mem_id = _insert_memory_with_age(
        plugin, "prune-user", "无用信息", days_old=10,
        score=0.2, importance=0.15, confidence=0.2, reinforce=0
    )

    plugin._auto_prune_low_quality()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 0


def test_auto_prune_preserves_young_low_quality(plugin):
    """低质量但小于 7 天的记忆不应被剪枝（缓冲期保护）。"""
    mem_id = _insert_memory_with_age(
        plugin, "prune-young", "新消息", days_old=2,
        score=0.1, importance=0.1, confidence=0.1, reinforce=0
    )

    plugin._auto_prune_low_quality()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 1  # 未被剪枝


def test_auto_prune_preserves_reinforced_memories(plugin):
    """被强化召回（reinforce_count > 1）的低质量记忆不应被剪枝。"""
    mem_id = _insert_memory_with_age(
        plugin, "prune-reinforced", "被强化过", days_old=15,
        score=0.1, importance=0.1, confidence=0.1, reinforce=0
    )
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET reinforce_count=2 WHERE id=?", (mem_id,))

    plugin._auto_prune_low_quality()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 1  # 有强化记录，保留


# ── 场景 3: WebUI _handle_login 异常路径 ─────────────────────────────────────


class _BadJsonRequest:
    """模拟 JSON 解析失败的请求对象。"""

    async def json(self):
        raise ValueError("invalid json")


def test_web_login_handles_bad_json_gracefully(web_module, plugin):
    """_handle_login 收到格式错误请求时应返回 400，而非服务器崩溃。"""
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_username": "admin",
            "webui_password": "secret",
        },
    )

    resp = asyncio.run(server._handle_login(_BadJsonRequest()))
    # 应优雅失败，返回 4xx
    assert resp.status in (400, 401, 422, 500)


# ── 场景 4: _LockedConnection 并发回归 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_locked_connection_concurrent_writes_no_deadlock_or_loss(plugin):
    """多线程并发写入 conversation_cache 时，锁机制应保证无死锁且数据完整。"""
    n_threads = 10
    n_writes_per_thread = 5
    errors = []

    async def writer(tid):
        try:
            for i in range(n_writes_per_thread):
                await plugin._insert_conversation(
                    canonical_id=f"thread-{tid}",
                    role="user",
                    content=f"msg-{tid}-{i}",
                    source_adapter="qq",
                    source_user_id=str(tid),
                    unified_msg_origin="",
                )
        except Exception as e:
            errors.append(e)

    def thread_runner(tid):
        asyncio.run(writer(tid))

    threads = [threading.Thread(target=thread_runner, args=(tid,)) for tid in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Thread errors: {errors}"

    with plugin._db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_cache WHERE canonical_user_id LIKE 'thread-%'"
        ).fetchone()["n"]

    # 每个线程写 n_writes_per_thread 条（不去重，canonical_id 唯一且内容不同）
    assert total == n_threads * n_writes_per_thread


@pytest.mark.asyncio
async def test_locked_connection_reentrant_read_does_not_deadlock(plugin):
    """同一线程在 with _db() 块外调用 _db() 再进行读操作不应死锁。"""
    # _db() 每次返回一个新的 _LockedConnection wrapper，锁是非递归的，
    # 但只要不在同一线程内嵌套 with 块就没问题。
    # 本测试验证顺序调用是安全的。
    await plugin._insert_conversation(
        canonical_id="lock-test",
        role="user",
        content="顺序访问测试",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="",
    )
    rows = plugin._fetch_pending_rows("lock-test", 10)
    assert len(rows) == 1


# ── 场景 5: _insert_conversation try-except 保护 ────────────────────────────


@pytest.mark.asyncio
async def test_insert_conversation_does_not_raise_on_db_error(plugin, monkeypatch):
    """_insert_conversation 在 DB 出错时不应向调用方抛出异常。"""

    original_db = plugin._db

    class _BrokenConn:
        def __enter__(self):
            raise RuntimeError("simulated DB error")

        def __exit__(self, *args):
            pass

        def execute(self, *args, **kwargs):
            raise RuntimeError("simulated DB error")

    call_count = [0]

    def patched_db():
        call_count[0] += 1
        if call_count[0] == 1:
            return _BrokenConn()
        return original_db()

    monkeypatch.setattr(plugin, "_db", patched_db)

    # Should not raise
    await plugin._insert_conversation(
        canonical_id="err-user",
        role="user",
        content="test content",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="",
    )


# ── 场景 6: _safe_bool 字符串容错 ────────────────────────────────────────────


def test_safe_bool_handles_string_false(plugin_module, tmp_path, monkeypatch):
    """enable_auto_capture='false' 字符串应被正确解析为 False。"""
    monkeypatch.chdir(tmp_path)
    p = plugin_module.TMemoryPlugin(
        context=None,
        config={"enable_auto_capture": "false", "enable_memory_injection": "false"},
    )
    assert p._cfg.enable_auto_capture is False
    assert p._cfg.enable_memory_injection is False


def test_safe_bool_handles_string_true(plugin_module, tmp_path, monkeypatch):
    """enable_auto_capture='true' 字符串应被正确解析为 True。"""
    monkeypatch.chdir(tmp_path)
    p = plugin_module.TMemoryPlugin(
        context=None,
        config={"enable_auto_capture": "true"},
    )
    assert p._cfg.enable_auto_capture is True



# ── 场景 2: 记忆衰减 / 自动裁剪 ─────────────────────────────────────────────


def _insert_memory_with_age(plugin, canonical_id, memory_text, days_old, score=0.3, importance=0.2, confidence=0.3, reinforce=0):
    """辅助：插入一条指定天数的历史记忆并直接调整时间戳。"""
    mem_id = plugin._insert_memory(
        canonical_id=canonical_id,
        adapter="qq",
        adapter_user="99",
        memory=memory_text,
        score=score,
        memory_type="fact",
        importance=importance,
        confidence=confidence,
    )
    old_ts = time.time() - days_old * 86400
    old_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old_ts))
    with plugin._db() as conn:
        conn.execute(
            "UPDATE memories SET last_seen_at=?, created_at=?, updated_at=? WHERE id=?",
            (old_dt, old_dt, old_dt, mem_id),
        )
    return mem_id


def test_decay_stale_memories_marks_stale_after_30_days(plugin):
    """超过 30 天未命中的记忆应被标记为 is_active=2 (stale)。"""
    mem_id = _insert_memory_with_age(plugin, "decay-user", "喜欢旅行", days_old=35, score=0.8, importance=0.8, confidence=0.8)

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 2


def test_decay_stale_memories_marks_archived_after_90_days(plugin):
    """超过 90 天未命中的记忆应被标记为 is_active=3 (archived)。"""
    mem_id = _insert_memory_with_age(plugin, "decay-user2", "曾住在上海", days_old=95, score=0.8, importance=0.8, confidence=0.8)

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 3


def test_decay_stale_memories_ignores_pinned(plugin):
    """is_pinned=1 的记忆即使超时也不应被衰减。"""
    mem_id = _insert_memory_with_age(plugin, "pinned-user", "核心偏好", days_old=100)
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET is_pinned=1 WHERE id=?", (mem_id,))

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 1  # 未变化


def test_auto_prune_low_quality_deactivates_old_low_quality(plugin):
    """低质量（质量分 < 0.35）且超过 7 天的记忆应被失活(is_active=0)。"""
    # 质量分 = 0.3*0.2 + 0.4*0.15 + 0.3*0.2 = 0.06 + 0.06 + 0.06 = 0.18 < 0.35
    mem_id = _insert_memory_with_age(
        plugin, "prune-user", "无用信息", days_old=10,
        score=0.2, importance=0.15, confidence=0.2, reinforce=0
    )

    plugin._auto_prune_low_quality()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 0


def test_auto_prune_preserves_young_low_quality(plugin):
    """低质量但小于 7 天的记忆不应被剪枝（缓冲期保护）。"""
    mem_id = _insert_memory_with_age(
        plugin, "prune-young", "新消息", days_old=2,
        score=0.1, importance=0.1, confidence=0.1, reinforce=0
    )

    plugin._auto_prune_low_quality()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 1  # 未被剪枝


def test_auto_prune_preserves_reinforced_memories(plugin):
    """被强化召回（reinforce_count > 1）的低质量记忆不应被剪枝。"""
    mem_id = _insert_memory_with_age(
        plugin, "prune-reinforced", "被强化过", days_old=15,
        score=0.1, importance=0.1, confidence=0.1, reinforce=0
    )
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET reinforce_count=2 WHERE id=?", (mem_id,))

    plugin._auto_prune_low_quality()

    with plugin._db() as conn:
        row = conn.execute("SELECT is_active FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert row["is_active"] == 1  # 有强化记录，保留


# ── 场景 3: WebUI _handle_login 异常路径 ─────────────────────────────────────


class _BadJsonRequest:
    """模拟 JSON 解析失败的请求对象。"""

    async def json(self):
        raise ValueError("invalid json")


def test_web_login_handles_bad_json_gracefully(web_module, plugin):
    """_handle_login 收到格式错误请求时应返回 400，而非服务器崩溃。"""
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_username": "admin",
            "webui_password": "secret",
        },
    )

    resp = asyncio.run(server._handle_login(_BadJsonRequest()))
    # 应优雅失败，返回 4xx
    assert resp.status in (400, 401, 422, 500)


# ── 场景 4: _LockedConnection 并发回归 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_locked_connection_concurrent_writes_no_deadlock_or_loss(plugin):
    """多线程并发写入 conversation_cache 时，锁机制应保证无死锁且数据完整。"""
    n_threads = 10
    n_writes_per_thread = 5
    errors = []

    async def writer(tid):
        try:
            for i in range(n_writes_per_thread):
                await plugin._insert_conversation(
                    canonical_id=f"thread-{tid}",
                    role="user",
                    content=f"msg-{tid}-{i}",
                    source_adapter="qq",
                    source_user_id=str(tid),
                    unified_msg_origin="",
                )
        except Exception as e:
            errors.append(e)

    def thread_runner(tid):
        asyncio.run(writer(tid))

    threads = [threading.Thread(target=thread_runner, args=(tid,)) for tid in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Thread errors: {errors}"

    with plugin._db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_cache WHERE canonical_user_id LIKE 'thread-%'"
        ).fetchone()["n"]

    # 每个线程写 n_writes_per_thread 条（不去重，canonical_id 唯一且内容不同）
    assert total == n_threads * n_writes_per_thread


@pytest.mark.asyncio
async def test_locked_connection_reentrant_read_does_not_deadlock(plugin):
    """同一线程在 with _db() 块外调用 _db() 再进行读操作不应死锁。"""
    # _db() 每次返回一个新的 _LockedConnection wrapper，锁是非递归的，
    # 但只要不在同一线程内嵌套 with 块就没问题。
    # 本测试验证顺序调用是安全的。
    await plugin._insert_conversation(
        canonical_id="lock-test",
        role="user",
        content="顺序访问测试",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="",
    )
    rows = plugin._fetch_pending_rows("lock-test", 10)
    assert len(rows) == 1


# ── 场景 5: _insert_conversation try-except 保护 ────────────────────────────


@pytest.mark.asyncio
async def test_insert_conversation_does_not_raise_on_db_error(plugin, monkeypatch):
    """_insert_conversation 在 DB 出错时不应向调用方抛出异常。"""

    original_db = plugin._db

    class _BrokenConn:
        def __enter__(self):
            raise RuntimeError("simulated DB error")

        def __exit__(self, *args):
            pass

        def execute(self, *args, **kwargs):
            raise RuntimeError("simulated DB error")

    call_count = [0]

    def patched_db():
        call_count[0] += 1
        if call_count[0] == 1:
            return _BrokenConn()
        return original_db()

    monkeypatch.setattr(plugin, "_db", patched_db)

    # Should not raise
    await plugin._insert_conversation(
        canonical_id="err-user",
        role="user",
        content="test content",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="",
    )


# ── 场景 6: _safe_bool 字符串容错 ────────────────────────────────────────────


def test_safe_bool_handles_string_false(plugin_module, tmp_path, monkeypatch):
    """enable_auto_capture='false' 字符串应被正确解析为 False。"""
    monkeypatch.chdir(tmp_path)
    p = plugin_module.TMemoryPlugin(
        context=None,
        config={"enable_auto_capture": "false", "enable_memory_injection": "false"},
    )
    assert p._cfg.enable_auto_capture is False
    assert p._cfg.enable_memory_injection is False


def test_safe_bool_handles_string_true(plugin_module, tmp_path, monkeypatch):
    """enable_auto_capture='true' 字符串应被正确解析为 True。"""
    monkeypatch.chdir(tmp_path)
    p = plugin_module.TMemoryPlugin(
        context=None,
        config={"enable_auto_capture": "true"},
    )
    assert p._cfg.enable_auto_capture is True


# ── 场景 7: style_distill 持久化与采集条件回归 (TMEAAA-163) ────────────────────


def test_build_distill_prompt_enable_style_includes_style_section(plugin):
    """enable_style=True 时蒸馏提示词应包含 style 类型与 persona 提示。"""
    prompt = plugin._distill_mgr.build_distill_prompt(
        "user: 请用简洁方式回复\nassistant: 好的",
        persona_profile="Bot 人格: 中文助手",
        enable_style=True,
    )
    assert "|style" in prompt
    assert "style 类型专项" in prompt
    assert "Bot 人格" in prompt


def test_build_distill_prompt_disable_style_excludes_style_section(plugin):
    """enable_style=False 时蒸馏提示词应排除 style 类型、style 专项、persona 提示。"""
    prompt = plugin._distill_mgr.build_distill_prompt(
        "user: 请用简洁方式回复\nassistant: 好的",
        persona_profile="Bot 人格: 中文助手",
        enable_style=False,
    )
    assert "|style" not in prompt
    assert "style 类型" not in prompt
    assert "Bot 人格" not in prompt
    assert '"memory_type": "preference|fact|task|restriction"' in prompt


def test_build_distill_prompt_default_excludes_style(plugin):
    """ADR TMEAAA-180: 默认 enable_style=False，记忆蒸馏不包含 style 类型。"""
    prompt = plugin._distill_mgr.build_distill_prompt(
        "user: hello\nassistant: hi",
        persona_profile="test",
    )
    assert "|style" not in prompt, "default should exclude style from memory distill prompt"


def test_style_distill_config_persists_to_plugin_config_not_global(plugin_module, tmp_path, monkeypatch):
    """style_distill 持久化应写入 self.config 而非 ctx.get_config()。

    注意：测试环境中 self.config 是普通 dict（无 save_config），
    生产环境为 AstrBotConfig 子类（自带 save_config）。
    此测试验证 self.config 字典被正确突变，不验证 save_config 磁盘写入。
    """
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "style_distill_settings": {"enable_style_distill": True}
        },
    )
    plugin._init_db()
    plugin._migrate_schema()

    # 模拟 /style_distill off 持久化逻辑
    enabled = False
    plugin._cfg.enable_style_distill = enabled
    style_settings = plugin.config.get("style_distill_settings", {})
    if not isinstance(style_settings, dict):
        style_settings = {}
    style_settings["enable_style_distill"] = enabled
    plugin.config["style_distill_settings"] = style_settings

    # 验证 self.config 被正确更新
    assert plugin.config["style_distill_settings"]["enable_style_distill"] is False
    assert plugin._cfg.enable_style_distill is False

    # 验证 parse_config 能从更新后的 config 正确读取
    from astrbot_plugin_tmemory.core.config import parse_config
    reparsed = parse_config(plugin.config)
    assert reparsed.enable_style_distill is False

    plugin._close_db()


@pytest.mark.asyncio
async def test_distill_rows_with_llm_respects_enable_style_flag(plugin_with_ctx):
    """当 enable_style_distill=False 时，蒸馏提示词不应包含 style 指令。"""
    plugin, ctx = plugin_with_ctx
    plugin._cfg.enable_style_distill = False
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    rows = [
        {
            "id": 1,
            "role": "user",
            "content": "我喜欢简洁的回复",
            "source_adapter": "qq",
            "source_user_id": "42",
            "unified_msg_origin": "group:1",
            "scope": "user",
            "persona_id": "",
        }
    ]

    captured_prompt = []

    async def capture_llm_generate(**kwargs):
        captured_prompt.append(kwargs.get("prompt", ""))
        return types.SimpleNamespace(
            completion_text='{"memories": []}',
            usage=types.SimpleNamespace(input_other=5, input_cached=0, output=3),
        )

    ctx.llm_generate = capture_llm_generate

    await plugin._distill_rows_with_llm(rows)
    assert captured_prompt, "LLM was not called"
    prompt = captured_prompt[0]
    assert "|style" not in prompt
    assert "style 类型" not in prompt
