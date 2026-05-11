"""蒸馏全链路集成测试 — 覆盖 capture→distill→insert 端到端路径。

Tier 1: 规则蒸馏回退（无 LLM，快速确定）
Tier 2: Mock LLM 蒸馏（验证解析/校验/入库全链路）
"""

import pytest


class _MockContextNoProvider:
    """无 provider → 触发规则蒸馏（umo 为空直接返回""）。"""
    def get_using_provider(self, **kw):
        return None
    async def get_current_chat_provider_id(self, **kw):
        return None
    async def llm_generate(self, **kw):
        raise RuntimeError("LLM should not be called in rule-fallback path")


class _MockUsage:
    input_other = 120
    input_cached = 0
    output = 60


class _MockLLMResponse:
    def __init__(self, completion_text, usage=None):
        self.completion_text = completion_text
        self.usage = usage or _MockUsage()


class _MockLLMContext:
    """有 umo → 走 LLM 蒸馏路径。注意 resolve_distill_provider_id 需要 unified_msg_origin 非空。"""
    def __init__(self, resp_text=None):
        txt = resp_text or (
            '{"memories":[{"memory":"default","memory_type":"fact",'
            '"importance":0.7,"confidence":0.7,"score":0.6}]}'
        )
        self._resp = _MockLLMResponse(txt)
    def get_using_provider(self, **kw):
        return None
    async def get_current_chat_provider_id(self, **kw):
        return "mock-pid"
    async def llm_generate(self, **kw):
        return self._resp


# ═══ Tier 1: 规则蒸馏回退 ═══

@pytest.mark.asyncio
async def test_rule_distill_cycle_creates_memories_from_cache(plugin):
    """T1: 插入对话缓存 → 规则蒸馏 → 生成记忆 + 标记已蒸馏。"""
    plugin.context = _MockContextNoProvider()
    await plugin._insert_conversation("u1", "user", "我喜欢吃火锅每周都去", "qq", "42", "")
    await plugin._insert_conversation("u1", "assistant", "火锅确实很棒", "qq", "42", "")
    assert plugin._count_pending_rows() == 2
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert p >= 1 and c >= 1
    assert len(plugin._fetch_pending_rows("u1", 10)) == 0
    assert len(plugin._list_memories("u1", 10)) >= 1


@pytest.mark.asyncio
async def test_rule_distill_cycle_multiple_users(plugin):
    """T1: 多个用户各有待蒸馏消息 → 全部处理。"""
    plugin.context = _MockContextNoProvider()
    for uid in ("ua", "ub", "uc"):
        for i in range(5):
            await plugin._insert_conversation(uid, "user", "msg%d:like%d" % (i, i), "qq", uid, "")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert p >= 1 and c >= 1
    for uid in ("ua", "ub", "uc"):
        assert len(plugin._fetch_pending_rows(uid, 99)) == 0


@pytest.mark.asyncio
async def test_rule_distill_cycle_records_history(plugin):
    """T1: 蒸馏完成后 distill_history 表应有记录。"""
    plugin.context = _MockContextNoProvider()
    await plugin._insert_conversation("uhist", "user", "软件工程师", "qq", "99", "")
    await plugin._run_distill_cycle(force=True)
    with plugin._db() as conn:
        rows = conn.execute("SELECT * FROM distill_history ORDER BY id DESC LIMIT 1").fetchall()
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["trigger_type"] == "manual" and r["users_processed"] >= 1
    assert r["started_at"] and r["finished_at"]


@pytest.mark.asyncio
async def test_rule_distill_empty_cache_records_zero(plugin):
    """T1: 空缓存 → 蒸馏记录 0/0 但不崩溃。"""
    plugin.context = _MockContextNoProvider()
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert p == 0 and c == 0


@pytest.mark.asyncio
async def test_rule_distill_prefilter_skips_low_info(plugin):
    """T1: 纯低信息量消息被 prefilter 过滤后直接标记已蒸馏。"""
    plugin.context = _MockContextNoProvider()
    plugin._cfg.capture_min_content_len = 5
    for m in ("嗯", "好", "ok", "哈哈"):
        await plugin._insert_conversation("ulo", "user", m, "qq", "1", "")
    await plugin._run_distill_cycle(force=True)
    assert len(plugin._fetch_pending_rows("ulo", 10)) == 0
    assert len(plugin._list_memories("ulo", 10)) == 0
    assert plugin._distill_skipped_rows >= 4


# ═══ Tier 2: Mock LLM 蒸馏 ═══

@pytest.mark.asyncio
async def test_mock_llm_distill_parses_and_inserts_memory(plugin):
    """T2: Mock LLM 返回 JSON → 解析 → 校验 → 入库。"""
    plugin.context = _MockLLMContext(
        '{"memories":[{"memory":"用户喜欢吃火锅","memory_type":"preference",'
        '"importance":0.85,"confidence":0.95,"score":0.80}]}'
    )
    await plugin._insert_conversation("um", "user", "我特别喜欢吃火锅每周都要去",
                                       "qq", "42", "group:1")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert p == 1 and c == 1
    mems = plugin._list_memories("um", 10)
    assert len(mems) == 1
    m = mems[0]
    assert "火锅" in m["memory"] and m["memory_type"] == "preference"
    assert 0.8 <= m["importance"] <= 1.0
    assert 0.9 <= m["confidence"] <= 1.0


@pytest.mark.asyncio
async def test_mock_llm_distill_multiple_memories(plugin):
    """T2: 一批对话蒸馏出多条记忆。"""
    plugin.context = _MockLLMContext(
        '{"memories":['
        '{"memory":"用户喜欢咖啡","memory_type":"preference","importance":0.9,"confidence":0.95,"score":0.85},'
        '{"memory":"用户住在北京","memory_type":"fact","importance":0.8,"confidence":0.90,"score":0.75},'
        '{"memory":"用户是后端工程师","memory_type":"fact","importance":0.7,"confidence":0.85,"score":0.70}]}'
    )
    for i in range(5):
        await plugin._insert_conversation("um2", "user", "d%d:个人信息" % i, "qq", "42", "group:1")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert p == 1 and c == 3
    texts = [m["memory"] for m in plugin._list_memories("um2", 10)]
    assert any("咖啡" in t for t in texts)
    assert any("北京" in t for t in texts)
    assert any("工程师" in t for t in texts)


@pytest.mark.asyncio
async def test_mock_llm_distill_validates_output(plugin):
    """T2: LLM 返回空 memory → validate 过滤。"""
    plugin.context = _MockLLMContext(
        '{"memories":['
        '{"memory":"这是一个有效记忆","memory_type":"fact","importance":0.8,"confidence":0.9,"score":0.7},'
        '{"memory":"","memory_type":"fact","importance":0.5,"confidence":0.5,"score":0.5}]}'
    )
    await plugin._insert_conversation("uv", "user", "测试消息较长足以不被过滤",
                                       "qq", "1", "group:1")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert c == 1
    mems = plugin._list_memories("uv", 10)
    assert len(mems) == 1 and mems[0]["memory"] == "这是一个有效记忆"


@pytest.mark.asyncio
async def test_mock_llm_distill_records_token_usage(plugin):
    """T2: LLM 蒸馏应记录 token 消耗到 distill_history。"""
    plugin.context = _MockLLMContext(
        '{"memories":[{"memory":"token测试记忆","memory_type":"fact",'
        '"importance":0.7,"confidence":0.8,"score":0.6}]}'
    )
    await plugin._insert_conversation("utok", "user", "token记录测试消息",
                                       "qq", "1", "group:1")
    await plugin._run_distill_cycle(force=True)
    with plugin._db() as conn:
        r = conn.execute(
            "SELECT tokens_input, tokens_output FROM distill_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert r["tokens_input"] >= 0 and r["tokens_output"] >= 0


@pytest.mark.asyncio
async def test_mock_llm_distill_fallback_on_llm_error(plugin):
    """T2: LLM 调用抛出异常 → 回退到规则蒸馏，不崩溃。"""
    class EC:
        def get_using_provider(self, **kw): return None
        async def get_current_chat_provider_id(self, **kw): return "err"
        async def llm_generate(self, **kw): raise ConnectionError("down")
    plugin.context = EC()
    await plugin._insert_conversation("uerr", "user", "数据科学家", "qq", "1", "group:1")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert p >= 1 and c >= 1
    assert len(plugin._fetch_pending_rows("uerr", 10)) == 0


@pytest.mark.asyncio
async def test_mock_llm_distill_records_memory_event(plugin):
    """T2: 蒸馏生成的记忆应在 memory_events 中记录事件。"""
    plugin.context = _MockLLMContext(
        '{"memories":[{"memory":"事件测试记忆","memory_type":"fact",'
        '"importance":0.8,"confidence":0.9,"score":0.7}]}'
    )
    await plugin._insert_conversation("uev", "user", "事件记录测试消息",
                                       "qq", "1", "group:1")
    await plugin._run_distill_cycle(force=True)
    with plugin._db() as conn:
        evts = conn.execute(
            "SELECT event_type FROM memory_events WHERE canonical_user_id='uev'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(evts) >= 1


# ═══ 边界条件 ═══

@pytest.mark.asyncio
async def test_distill_assistant_only_conversation_skipped(plugin):
    """边角：纯 assistant 消息 → 标记已蒸馏但不生成记忆。"""
    plugin.context = _MockContextNoProvider()
    await plugin._insert_conversation("uao", "assistant", "我是AI助手", "qq", "1", "")
    await plugin._run_distill_cycle(force=True)
    assert len(plugin._fetch_pending_rows("uao", 10)) == 0
    assert len(plugin._list_memories("uao", 10)) == 0


# ═══ 端到端链路 ═══

@pytest.mark.asyncio
async def test_distill_to_inject_chain(plugin):
    """端到端: capture → distill → memories → 缓存清理 → 历史记录。"""
    plugin.context = _MockContextNoProvider()
    for i in range(5):
        await plugin._insert_conversation("uchain", "user", "like%d:川菜" % i,
                                           "qq", "42", "group:1")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert p >= 1 and c >= 1
    with plugin._db() as conn:
        h = conn.execute("SELECT * FROM distill_history ORDER BY id DESC LIMIT 1").fetchone()
    assert h is not None
    assert len(plugin._list_memories("uchain", 10)) >= 1
    assert len(plugin._fetch_pending_rows("uchain", 10)) == 0


@pytest.mark.asyncio
async def test_distill_preserves_source_metadata(plugin):
    """蒸馏后 DB 中 memories 的 source_adapter / source_user_id 正确。"""
    plugin.context = _MockContextNoProvider()
    await plugin._insert_conversation("usrc", "user", "我在微信上用这个机器人",
                                       "wechat", "wx-123", "group:test")
    await plugin._run_distill_cycle(force=True)
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT source_adapter, source_user_id FROM memories "
            "WHERE canonical_user_id='usrc' LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["source_adapter"] == "wechat"
    assert row["source_user_id"] == "wx-123"


@pytest.mark.asyncio
async def test_distill_multiple_cycles_incremental(plugin):
    """连续多次蒸馏：第二批新消息也能独立蒸馏并追加记忆。"""
    plugin.context = _MockContextNoProvider()
    for i in range(3):
        await plugin._insert_conversation("uinc", "user", "r1:跑步%d" % i, "qq", "42", "")
    p1, c1, _errs = await plugin._run_distill_cycle(force=True)
    assert p1 >= 1 and c1 >= 1
    n1 = len(plugin._list_memories("uinc", 20))
    for i in range(3):
        await plugin._insert_conversation("uinc", "user", "r2:篮球%d" % i, "qq", "42", "")
    p2, c2, _errs = await plugin._run_distill_cycle(force=True)
    assert p2 >= 1 and c2 >= 1
    n2 = len(plugin._list_memories("uinc", 50))
    assert n2 > n1
    assert len(plugin._fetch_pending_rows("uinc", 20)) == 0
