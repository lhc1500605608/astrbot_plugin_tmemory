"""蒸馏规则分级门控与 prompt 缓存测试（Plan TMEAAA-379 B3 / BC-4）。"""

import pytest


def _gating():
    from astrbot_plugin_tmemory.core.distill_gating import (
        classify_batch,
        has_memory_signal,
    )

    return classify_batch, has_memory_signal


class _MockUsage:
    input_other = 120
    input_cached = 0
    output = 60


class _MockLLMResponse:
    def __init__(self, text):
        self.completion_text = text
        self.usage = _MockUsage()


class _CountingLLMContext:
    """记录 llm_generate 调用次数，返回固定 JSON。"""

    def __init__(self, text=None):
        self.calls = 0
        self._text = text or (
            '{"memories":[{"memory":"用户喜欢吃火锅","memory_type":"preference",'
            '"importance":0.85,"confidence":0.95,"score":0.8}]}'
        )

    def get_using_provider(self, **kw):
        return None

    async def get_current_chat_provider_id(self, **kw):
        return "mock-pid"

    async def llm_generate(self, **kw):
        self.calls += 1
        return _MockLLMResponse(self._text)


# ── 规则分级纯函数 ──

def test_has_memory_signal_preference_and_identity(plugin_module):
    _, has_memory_signal = _gating()
    assert has_memory_signal("我喜欢吃火锅")
    assert has_memory_signal("我是一名后端工程师")
    assert has_memory_signal("我每周二晚上做复盘")
    assert has_memory_signal("不要放香菜")
    assert has_memory_signal("回答时先给结论")


def test_has_memory_signal_rejects_chitchat(plugin_module):
    _, has_memory_signal = _gating()
    assert not has_memory_signal("今天天气不错")
    assert not has_memory_signal("帮我写个代码")
    assert not has_memory_signal("这个视频挺好看")
    assert not has_memory_signal("")


def test_classify_batch_simple_vs_complex(plugin_module):
    classify_batch, _ = _gating()
    simple = [{"role": "user", "content": "今天天气不错"}]
    complex_signal = [{"role": "user", "content": "我喜欢喝黑咖啡"}]
    long_text = [{"role": "user", "content": "这是一段足够长的普通描述" * 4}]
    assert classify_batch(simple, min_chars=40) == "simple"
    assert classify_batch(complex_signal, min_chars=40) == "complex"
    assert classify_batch(long_text, min_chars=40) == "complex"


def test_classify_batch_ignores_assistant_only(plugin_module):
    classify_batch, _ = _gating()
    rows = [{"role": "assistant", "content": "我喜欢吃火锅"}]
    assert classify_batch(rows, min_chars=40) == "simple"


# ── 门控集成：默认关闭 → 纯 LLM 路径 ──

@pytest.mark.asyncio
async def test_rule_gating_disabled_calls_llm(plugin):
    plugin.context = _CountingLLMContext()
    plugin._cfg.distill_rule_gating = False
    await plugin._insert_conversation("g-off", "user", "今天天气不错", "qq", "1", "group:1")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert plugin.context.calls == 1
    assert p == 1 and c == 1


@pytest.mark.asyncio
async def test_rule_gating_enabled_skips_simple(plugin):
    plugin.context = _CountingLLMContext()
    plugin._cfg.distill_rule_gating = True
    await plugin._insert_conversation("g-simple", "user", "今天天气不错", "qq", "1", "")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert plugin.context.calls == 0
    assert c == 0
    assert plugin._distill_rule_gated_batches == 1
    assert len(plugin._list_memories("g-simple", 10)) == 0
    assert len(plugin._fetch_pending_rows("g-simple", 10)) == 0


@pytest.mark.asyncio
async def test_rule_gating_enabled_escalates_complex(plugin):
    plugin.context = _CountingLLMContext()
    plugin._cfg.distill_rule_gating = True
    await plugin._insert_conversation("g-complex", "user", "我喜欢喝黑咖啡", "qq", "1", "group:1")
    p, c, _errs = await plugin._run_distill_cycle(force=True)
    assert plugin.context.calls == 1
    assert p == 1 and c == 1
    assert len(plugin._list_memories("g-complex", 10)) == 1


@pytest.mark.asyncio
async def test_budget_exceeded_forces_rule_path(plugin):
    plugin.context = _CountingLLMContext()
    plugin._cfg.distill_rule_gating = False
    plugin._cfg.daily_token_budget = 1
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
            " users_processed, memories_created, users_failed, errors, duration_sec,"
            " tokens_input, tokens_output, tokens_total)"
            " VALUES(?, ?, 'test', 1, 0, 0, '[]', 0, 100, 100, 200)",
            (plugin._now(), plugin._now()),
        )
    await plugin._insert_conversation("g-budget", "user", "今天天气不错", "qq", "1", "")
    await plugin._run_distill_cycle(force=True)
    assert plugin.context.calls == 0


# ── prompt 缓存 ──

@pytest.mark.asyncio
async def test_prompt_cache_avoids_second_llm_call(plugin):
    plugin.context = _CountingLLMContext()
    plugin._cfg.distill_prompt_cache = True
    rows = [
        {
            "role": "user",
            "content": "我喜欢吃火锅",
            "canonical_user_id": "cache-u",
            "unified_msg_origin": "group:1",
            "source_adapter": "qq",
            "source_user_id": "1",
            "scope": "user",
            "persona_id": "",
        }
    ]
    items1, tok_in1, tok_out1, _e1 = await plugin._distill_rows_with_llm(rows)
    assert plugin.context.calls == 1
    assert tok_in1 > 0
    items2, tok_in2, tok_out2, _e2 = await plugin._distill_rows_with_llm(rows)
    assert plugin.context.calls == 1  # 命中缓存，不再调用 LLM
    assert tok_in2 == 0 and tok_out2 == 0
    assert items2 == items1
    assert plugin._distill_prompt_cache_hits == 1


@pytest.mark.asyncio
async def test_prompt_cache_disabled_calls_llm_each_time(plugin):
    plugin.context = _CountingLLMContext()
    plugin._cfg.distill_prompt_cache = False
    rows = [
        {
            "role": "user",
            "content": "我喜欢吃火锅",
            "canonical_user_id": "cache-u2",
            "unified_msg_origin": "group:1",
            "source_adapter": "qq",
            "source_user_id": "1",
            "scope": "user",
            "persona_id": "",
        }
    ]
    await plugin._distill_rows_with_llm(rows)
    await plugin._distill_rows_with_llm(rows)
    assert plugin.context.calls == 2


def test_prompt_cache_key_separates_models():
    from astrbot_plugin_tmemory.core.prompt_cache import compute_cache_key

    assert compute_cache_key("t", "m-a") != compute_cache_key("t", "m-b")
    assert compute_cache_key("t", "m-a") == compute_cache_key("t", "m-a")


def test_prompt_cache_store_get_and_prune(plugin):
    from astrbot_plugin_tmemory.core import prompt_cache as pc

    plugin._cfg.distill_prompt_cache_max_rows = 2
    for i in range(3):
        key = pc.compute_cache_key(f"transcript-{i}", "m")
        pc.store_distill_items(plugin, key, f"transcript-{i}", [{"memory": f"m{i}"}], "m")
    stats = pc.prompt_cache_stats(plugin)
    assert stats["entries"] == 2
    assert pc.get_cached_distill_items(plugin, pc.compute_cache_key("transcript-2", "m"))
    assert pc.clear_distill_prompt_cache(plugin) == 2

