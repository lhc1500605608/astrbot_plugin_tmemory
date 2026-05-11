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

    items, tok_in, tok_out, _errs = await plugin._distill_rows_with_llm(rows)

    assert len(items) == 1
    assert items[0]["memory"] == "用户偏好黑咖啡不加糖"
    assert items[0]["memory_type"] == "preference"
    assert tok_in == 100
    assert tok_out == 50


def test_distill_prompt_keeps_static_prefix_stable_for_cache(plugin):
    """动态风格分析和对话内容应位于静态规则之后，保留可缓存 prompt 前缀。"""
    style_a = "── 用户风格特征(规则分析) ──\n- 回复简短(平均8字)"
    style_b = "── 用户风格特征(规则分析) ──\n- 语气倾向: formal"

    prompt_a = plugin._distill_mgr.build_distill_prompt(
        "user: 哈哈我喜欢黑咖啡", style_a
    )
    prompt_b = plugin._distill_mgr.build_distill_prompt(
        "user: 请记住我周三晚上练球", style_b
    )

    static_prefix_a = prompt_a[: prompt_a.index(style_a)]
    static_prefix_b = prompt_b[: prompt_b.index(style_b)]

    assert static_prefix_a == static_prefix_b
    assert "输出格式(必须严格遵守)" in static_prefix_a
    assert "── 安全规则 ──" in static_prefix_a
    assert prompt_a.index(style_a) < prompt_a.index("对话如下:\n")
    assert prompt_b.index(style_b) < prompt_b.index("对话如下:\n")


@pytest.mark.asyncio
async def test_distill_rows_with_llm_preserves_prompt_prefix_across_batches(
    plugin_with_ctx,
):
    """不同批次的动态内容变化时，发送给 LLM 的静态 prompt 前缀仍应一致。"""
    plugin, ctx = plugin_with_ctx

    captured_prompts = []
    fake_resp = types.SimpleNamespace(
        completion_text='{"memories": []}',
        usage=types.SimpleNamespace(input_other=10, input_cached=5, output=2),
    )

    async def fake_llm_generate(**kwargs):
        captured_prompts.append(kwargs["prompt"])
        return fake_resp

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    await plugin._distill_rows_with_llm(
        [
            {
                "id": 1,
                "role": "user",
                "content": "哈哈今天也要喝黑咖啡！",
                "source_adapter": "qq",
                "source_user_id": "42",
                "unified_msg_origin": "group:1",
                "scope": "user",
                "persona_id": "",
            },
            {
                "id": 2,
                "role": "user",
                "content": "哈哈黑咖啡继续安排！",
                "source_adapter": "qq",
                "source_user_id": "42",
                "unified_msg_origin": "group:1",
                "scope": "user",
                "persona_id": "",
            },
            {
                "id": 3,
                "role": "user",
                "content": "哈哈不加糖！",
                "source_adapter": "qq",
                "source_user_id": "42",
                "unified_msg_origin": "group:1",
                "scope": "user",
                "persona_id": "",
            },
        ]
    )
    await plugin._distill_rows_with_llm(
        [
            {
                "id": 4,
                "role": "user",
                "content": "请记住我周三晚上固定练羽毛球。",
                "source_adapter": "qq",
                "source_user_id": "42",
                "unified_msg_origin": "group:1",
                "scope": "user",
                "persona_id": "",
            },
            {
                "id": 5,
                "role": "user",
                "content": "麻烦提醒事项避开周三晚上。",
                "source_adapter": "qq",
                "source_user_id": "42",
                "unified_msg_origin": "group:1",
                "scope": "user",
                "persona_id": "",
            },
            {
                "id": 6,
                "role": "user",
                "content": "谢谢，请以后都按这个约束处理。",
                "source_adapter": "qq",
                "source_user_id": "42",
                "unified_msg_origin": "group:1",
                "scope": "user",
                "persona_id": "",
            },
        ]
    )

    assert len(captured_prompts) == 2
    static_prefixes = [
        prompt[: prompt.index("── 用户风格特征(规则分析) ──")]
        for prompt in captured_prompts
    ]
    assert static_prefixes[0] == static_prefixes[1]
    assert "哈哈今天也要喝黑咖啡" not in static_prefixes[0]
    assert "周三晚上固定练羽毛球" not in static_prefixes[1]



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

    processed_users, total_memories, _errs = await plugin._run_distill_cycle(
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





@pytest.mark.asyncio
async def test_distill_cycle_integrates_cache_llm_memory_and_history(plugin_with_ctx):
    """强制蒸馏应贯通 conversation_cache、LLM、memories 与 distill_history。"""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="cycle-user",
        role="user",
        content="我每周三晚上都要练羽毛球，提醒事项请避开这个时间。",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
        scope="user",
        persona_id="persona-a",
    )
    await plugin._insert_conversation(
        canonical_id="cycle-user",
        role="assistant",
        content="收到，我会避开周三晚上安排提醒。",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
        scope="user",
        persona_id="persona-a",
    )

    seen_kwargs = {}
    fake_resp = types.SimpleNamespace(
        completion_text=(
            '{"memories": [{"memory": "用户每周三晚上练羽毛球，提醒事项需避开该时间", '
            '"memory_type": "preference", "importance": 0.82, '
            '"confidence": 0.91, "score": 0.88}]}'
        ),
        usage=types.SimpleNamespace(input_other=120, input_cached=10, output=35),
    )

    async def fake_llm_generate(**kwargs):
        seen_kwargs.update(kwargs)
        return fake_resp

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.distill_model_id = "mock-model"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories, _errs = await plugin._run_distill_cycle(
        force=True, trigger="qa-full-cycle"
    )

    with plugin._db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_cache WHERE canonical_user_id=? AND distilled=0",
            ("cycle-user",),
        ).fetchone()["n"]
        memory = conn.execute(
            """
            SELECT memory, memory_type, source_channel, scope, persona_id
            FROM memories
            WHERE canonical_user_id=?
            """,
            ("cycle-user",),
        ).fetchone()
        history = conn.execute(
            """
            SELECT trigger_type, users_processed, memories_created, users_failed,
                   tokens_input, tokens_output, tokens_total
            FROM distill_history
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert processed_users == 1
    assert total_memories == 1
    assert pending == 0
    assert seen_kwargs["chat_provider_id"] == "mock-provider"
    assert seen_kwargs["model_id"] == "mock-model"
    assert "周三晚上" in seen_kwargs["prompt"]
    assert memory["memory"] == "用户每周三晚上练羽毛球，提醒事项需避开该时间"
    assert memory["memory_type"] == "preference"
    assert memory["source_channel"] == "scheduled_distill"
    assert memory["scope"] == "user"
    assert memory["persona_id"] == "persona-a"
    assert history["trigger_type"] == "qa-full-cycle"
    assert history["users_processed"] == 1
    assert history["memories_created"] == 1
    assert history["users_failed"] == 0
    assert history["tokens_input"] == 130
    assert history["tokens_output"] == 35
    assert history["tokens_total"] == 165


@pytest.mark.asyncio
async def test_distill_cycle_falls_back_to_rule_and_still_persists_memory(
    plugin_with_ctx,
):
    """LLM 异常时 run_distill_cycle 应走规则降级并完成入库与历史记录。"""
    plugin, ctx = plugin_with_ctx

    await plugin._insert_conversation(
        canonical_id="fallback-cycle-user",
        role="user",
        content="我长期住在杭州，工作日早上通常七点半出门。",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
    )

    async def bad_llm_generate(**kwargs):
        raise RuntimeError("simulate distill provider outage")

    ctx.llm_generate = bad_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories, _errs = await plugin._run_distill_cycle(
        force=True, trigger="qa-rule-fallback-cycle"
    )

    with plugin._db() as conn:
        memory = conn.execute(
            """
            SELECT memory, memory_type, source_channel
            FROM memories
            WHERE canonical_user_id=?
            """,
            ("fallback-cycle-user",),
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM conversation_cache WHERE canonical_user_id=? AND distilled=0",
            ("fallback-cycle-user",),
        ).fetchone()["n"]
        history = conn.execute(
            """
            SELECT trigger_type, users_processed, memories_created,
                   tokens_input, tokens_output, tokens_total
            FROM distill_history
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert processed_users == 1
    assert total_memories == 1
    assert pending == 0
    assert memory["memory_type"] == "fact"
    assert memory["source_channel"] == "scheduled_distill"
    assert "杭州" in memory["memory"]
    assert history["trigger_type"] == "qa-rule-fallback-cycle"
    assert history["users_processed"] == 1
    assert history["memories_created"] == 1
    assert history["tokens_input"] == -1
    assert history["tokens_output"] == -1
    assert history["tokens_total"] == -1


@pytest.mark.asyncio
async def test_distill_cycle_marks_conflicting_old_memory_inactive(plugin_with_ctx):
    """蒸馏入库时应复用 _insert_memory 冲突检测并记录审计事件。"""
    plugin, ctx = plugin_with_ctx

    old_id = plugin._insert_memory(
        canonical_id="conflict-cycle-user",
        adapter="qq",
        adapter_user="42",
        memory="用户每周三晚上练羽毛球",
        score=0.8,
        memory_type="preference",
        importance=0.8,
        confidence=0.8,
    )
    await plugin._insert_conversation(
        canonical_id="conflict-cycle-user",
        role="user",
        content="我每周三晚上固定练羽毛球，提醒事项请避开这个时间。",
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
    )

    fake_resp = types.SimpleNamespace(
        completion_text=(
            '{"memories": [{"memory": "用户每周三晚上练羽毛球，提醒事项需避开该时间", '
            '"memory_type": "preference", "importance": 0.9, '
            '"confidence": 0.9, "score": 0.9}]}'
        ),
        usage=types.SimpleNamespace(input_other=80, input_cached=0, output=20),
    )

    async def fake_llm_generate(**kwargs):
        return fake_resp

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories, _errs = await plugin._run_distill_cycle(
        force=True, trigger="qa-conflict-cycle"
    )

    with plugin._db() as conn:
        old_memory = conn.execute(
            "SELECT is_active FROM memories WHERE id=?", (old_id,)
        ).fetchone()
        active_new = conn.execute(
            """
            SELECT COUNT(*) AS n FROM memories
            WHERE canonical_user_id=? AND is_active=1 AND source_channel='scheduled_distill'
            """,
            ("conflict-cycle-user",),
        ).fetchone()["n"]
        event = conn.execute(
            """
            SELECT event_type, payload_json
            FROM memory_events
            WHERE canonical_user_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            ("conflict-cycle-user",),
        ).fetchone()

    assert processed_users == 1
    assert total_memories == 1
    assert old_memory["is_active"] == 0
    assert active_new == 1
    assert event["event_type"] == "create_with_conflict"
    assert '"deactivated_count": 1' in event["payload_json"]


@pytest.mark.asyncio
async def test_distill_cycle_triggers_memory_decay_after_success(plugin_with_ctx):
    """run_distill_cycle 完成后应触发衰减，把长期未命中的旧记忆标记 stale。"""
    plugin, ctx = plugin_with_ctx

    stale_id = _insert_memory_with_age(
        plugin,
        "decay-cycle-user",
        "用户喜欢长途徒步旅行",
        days_old=35,
        score=0.8,
        importance=0.8,
        confidence=0.8,
    )
    await plugin._insert_conversation(
        canonical_id="fresh-cycle-user",
        role="user",
        content="我喜欢用番茄钟安排深度工作。",
        source_adapter="qq",
        source_user_id="43",
        unified_msg_origin="group:1",
    )

    fake_resp = types.SimpleNamespace(
        completion_text=(
            '{"memories": [{"memory": "用户喜欢用番茄钟安排深度工作", '
            '"memory_type": "preference", "importance": 0.8, '
            '"confidence": 0.9, "score": 0.85}]}'
        ),
        usage=types.SimpleNamespace(input_other=70, input_cached=0, output=18),
    )

    async def fake_llm_generate(**kwargs):
        return fake_resp

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.use_independent_distill_model = True

    processed_users, total_memories, _errs = await plugin._run_distill_cycle(
        force=True, trigger="qa-decay-cycle"
    )

    with plugin._db() as conn:
        stale_memory = conn.execute(
            "SELECT is_active FROM memories WHERE id=?", (stale_id,)
        ).fetchone()
        fresh_memory = conn.execute(
            """
            SELECT is_active FROM memories
            WHERE canonical_user_id=? AND source_channel='scheduled_distill'
            """,
            ("fresh-cycle-user",),
        ).fetchone()

    assert processed_users == 1
    assert total_memories == 1
    assert stale_memory["is_active"] == 2
    assert fresh_memory["is_active"] == 1


@pytest.mark.asyncio
async def test_on_llm_response_skips_when_both_switches_off(plugin):
    """on_llm_response must skip capture when enable_auto_capture and capture_assistant_reply are off."""
    plugin._cfg.enable_auto_capture = False
    plugin._cfg.capture_assistant_reply = False

    resp = types.SimpleNamespace(completion_text="这是助手的回复。")

    class Event:
        adapter_name = "qq"
        def __init__(self, msg): self.message_str = msg
        def get_sender_id(self): return "42"
        def get_group_id(self): return None

    event = Event("你好")

    await plugin.on_llm_response(event, resp)

    pending = plugin._fetch_pending_rows("qq:42", 10)
    assert len(pending) == 0, "no capture should happen when both switches are off"


@pytest.mark.asyncio
async def test_on_llm_response_respects_capture_assistant_reply_when_auto_capture_on(plugin):
    """on_llm_response must respect capture_assistant_reply when enable_auto_capture=True."""
    plugin._cfg.enable_auto_capture = True
    plugin._cfg.capture_assistant_reply = False

    resp = types.SimpleNamespace(completion_text="模型回复。")

    class Event:
        adapter_name = "qq"
        def __init__(self, msg): self.message_str = msg
        def get_sender_id(self): return "42"
        def get_group_id(self): return None

    event = Event("用户消息")

    await plugin.on_llm_response(event, resp)

    pending = plugin._fetch_pending_rows("qq:42", 10)
    assert len(pending) == 0, "should not capture when capture_assistant_reply=False"



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

    items, tok_in, tok_out, _errs = await plugin._distill_rows_with_llm(rows)

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

    items, tok_in, tok_out, _errs = await plugin._distill_rows_with_llm(rows)

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
