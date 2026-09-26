"""TMEAAA-589: 蒸馏防误记 — 相对时间规范化 + event 落库。

覆盖验收：
- build_distill_prompt 注入时间锚 + event/相对时间规则；空锚不注入。
- build_extraction_prompt 同款时间锚 + 事件不得写入 facet。
- parse_llm_json_memories 透传 event_date/valid_until。
- event 记忆按列落库；无 event_date 的 event 仍可入库(空值)。
- _distill_rows_with_llm 用本批最早 created_at 作锚并把 event_date 带入库。
"""

import types

import pytest


class _MockContext:
    async def llm_generate(self, **kwargs):
        raise NotImplementedError("不应被调用（子类覆盖）")

    async def get_current_chat_provider_id(self, **kwargs):
        return None


@pytest.fixture()
def plugin_with_ctx(tmp_path, monkeypatch, plugin_module):
    monkeypatch.chdir(tmp_path)
    ctx = _MockContext()
    instance = plugin_module.TMemoryPlugin(context=ctx, config={})
    instance._init_db()
    instance._migrate_schema()
    yield instance, ctx
    instance._close_db()


# ── prompt 时间锚与规则 ───────────────────────────────────────────────────────


def test_build_distill_prompt_has_time_anchor_and_rules(plugin):
    prompt = plugin._distill_mgr.build_distill_prompt(
        "user: 我明天要考试", "", "2026-09-26"
    )
    assert "当前时间基准：今天=2026-09-26 星期六" in prompt
    assert "event" in prompt
    assert "相对时间词" in prompt
    assert "event_date" in prompt
    assert "绝对日期" in prompt
    assert "这月生日" in prompt
    assert prompt.index("当前时间基准") < prompt.index("输出格式")


def test_build_distill_prompt_without_anchor_omits_time_section(plugin):
    prompt = plugin._distill_mgr.build_distill_prompt("user: 喜欢黑咖啡")
    assert "当前时间基准：今天=" not in prompt
    # 空锚/非法锚都不注入，保持静态前缀可缓存。
    assert "当前时间基准：今天=" not in plugin._distill_mgr.build_distill_prompt(
        "user: 喜欢黑咖啡", "", "not-a-date"
    )


def test_distill_prompt_static_prefix_stable_without_anchor(plugin):
    style_a = "── 风格A ──"
    style_b = "── 风格B ──"
    p_a = plugin._distill_mgr.build_distill_prompt("user: a", style_a)
    p_b = plugin._distill_mgr.build_distill_prompt("user: b", style_b)
    assert p_a[: p_a.index(style_a)] == p_b[: p_b.index(style_b)]


def test_build_extraction_prompt_has_time_anchor_and_event_rule(plugin):
    from astrbot_plugin_tmemory.core.profile_extractor import ProfileExtractor

    extractor = ProfileExtractor(plugin._cfg)
    prompt = extractor.build_extraction_prompt("user: 我下周末去爬山", "2026-09-26")
    assert "当前时间基准：今天=2026-09-26 星期六" in prompt
    assert "时间与事件规则" in prompt
    assert "不得" in prompt and "facet" in prompt
    # 空锚不注入。
    assert "当前时间基准：今天=" not in extractor.build_extraction_prompt("user: hi")


# ── 解析透传 ──────────────────────────────────────────────────────────────────


def test_parse_llm_json_memories_passthrough_event_fields(plugin):
    raw = (
        '{"memories": [{"memory": "用户 2026-09-27 参加考试", '
        '"memory_type": "event", "importance": 0.7, "confidence": 0.8, '
        '"score": 0.7, "event_date": "2026-09-27", "valid_until": "2026-09-28"}]}'
    )
    items = plugin._parse_llm_json_memories(raw)
    assert len(items) == 1
    assert items[0]["memory_type"] == "event"
    assert items[0]["event_date"] == "2026-09-27"
    assert items[0]["valid_until"] == "2026-09-28"


def test_parse_llm_json_memories_event_without_date_is_empty_string(plugin):
    raw = (
        '{"memories": [{"memory": "用户参加了一场考试", "memory_type": "event", '
        '"importance": 0.7, "confidence": 0.8, "score": 0.7}]}'
    )
    items = plugin._parse_llm_json_memories(raw)
    assert items[0]["event_date"] == ""
    assert items[0]["valid_until"] == ""


def test_validate_distill_output_keeps_event_type(plugin):
    items = [{
        "memory": "用户 2026-09-27 参加考试",
        "memory_type": "event",
        "importance": 0.8,
        "confidence": 0.9,
        "score": 0.8,
        "event_date": "2026-09-27",
    }]
    valid = plugin._validate_distill_output(items)
    assert valid and valid[0]["memory_type"] == "event"


# ── 落库 ──────────────────────────────────────────────────────────────────────


def test_event_memory_roundtrip_columns(plugin):
    mid = plugin._insert_memory(
        "u1", "webchat", "u1", "用户 2026-09-27 参加考试", 0.6, "event", 0.6, 0.6,
        event_date="2026-09-27", valid_until="2026-09-28",
    )
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT memory_type, event_date, valid_until FROM memories WHERE id=?",
            (mid,),
        ).fetchone()
    assert row["memory_type"] == "event"
    assert row["event_date"] == "2026-09-27"
    assert row["valid_until"] == "2026-09-28"


def test_event_without_date_inserts_empty_columns(plugin):
    mid = plugin._insert_memory(
        "u1", "webchat", "u1", "用户参加了一场考试", 0.6, "event", 0.6, 0.6,
    )
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT memory_type, event_date, valid_until FROM memories WHERE id=?",
            (mid,),
        ).fetchone()
    assert row["memory_type"] == "event"
    assert row["event_date"] == ""
    assert row["valid_until"] == ""


# ── 端到端：distill_ops 锚 + event 入库 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_distill_rows_llm_plumbs_anchor_and_event(plugin_with_ctx):
    plugin, ctx = plugin_with_ctx
    prompt_seen = {}

    fake_resp = types.SimpleNamespace(
        completion_text=(
            '{"memories": [{"memory": "用户 2026-09-27 参加考试", '
            '"memory_type": "event", "importance": 0.8, "confidence": 0.9, '
            '"score": 0.85, "event_date": "2026-09-27"}]}'
        ),
        usage=types.SimpleNamespace(input_other=10, input_cached=0, output=5),
    )

    async def fake_llm_generate(**kwargs):
        prompt_seen.update(kwargs)
        return fake_resp

    ctx.llm_generate = fake_llm_generate
    plugin._cfg.distill_provider_id = "mock-provider"
    plugin._cfg.distill_model_id = "mock-model"
    plugin._cfg.use_independent_distill_model = True

    rows = [
        {
            "id": 1,
            "role": "user",
            "content": "我明天要考试",
            "source_adapter": "qq",
            "source_user_id": "42",
            "unified_msg_origin": "webchat:tester",
            "scope": "user",
            "persona_id": "",
            "created_at": "2026-09-26 09:00:00",
        },
        {
            "id": 2,
            "role": "user",
            "content": "对，就是明天",
            "source_adapter": "qq",
            "source_user_id": "42",
            "unified_msg_origin": "webchat:tester",
            "scope": "user",
            "persona_id": "",
            "created_at": "2026-09-26 10:00:00",
        },
    ]

    items, _tok_in, _tok_out, _errs = await plugin._distill_rows_with_llm(rows)
    assert "当前时间基准：今天=2026-09-26 星期六" in prompt_seen.get("prompt", "")
    assert len(items) == 1 and items[0]["memory_type"] == "event"
    assert items[0]["event_date"] == "2026-09-27"

    mid = plugin._insert_memory(
        "u1", "qq", "42", items[0]["memory"], 0.7, items[0]["memory_type"],
        0.8, 0.9, event_date=items[0]["event_date"],
    )
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT memory_type, event_date FROM memories WHERE id=?", (mid,)
        ).fetchone()
    assert row["memory_type"] == "event"
    assert row["event_date"] == "2026-09-27"


# ── TMEAAA-593：相对时间 → 持久 fact 防误记 ──────────────────────────────────


def _item(text, mtype, **extra):
    base = {
        "memory": text,
        "memory_type": mtype,
        "importance": 0.8,
        "confidence": 0.9,
        "score": 0.8,
    }
    base.update(extra)
    return base


def test_guard_drops_relative_month_fact(plugin):
    """「这月生日」→「用户生日在9月」不得成 fact。"""
    items = [_item("用户生日在9月", "fact")]
    valid = plugin._validate_distill_output(items, "这月生日，朋友说要给我庆祝一下")
    assert valid == []


def test_guard_keeps_month_fact_without_relative_source(plugin):
    """用户明确给出绝对日期时，属稳定属性，不误伤。"""
    items = [_item("用户生日在9月1日", "fact")]
    valid = plugin._validate_distill_output(items, "我的生日是9月1日")
    assert len(valid) == 1 and valid[0]["memory_type"] == "fact"


def test_guard_normalizes_relative_attribute_with_absolute_date_to_event(plugin):
    items = [_item("用户2026-10-03参加考试", "fact")]
    valid = plugin._validate_distill_output(items, "下周要考试")
    assert len(valid) == 1
    assert valid[0]["memory_type"] == "event"
    assert valid[0]["event_date"] == "2026-10-03"


def test_guard_leaves_event_and_plain_fact_untouched(plugin):
    items = [
        _item("用户2026-10-03参加考试", "event", event_date="2026-10-03"),
        _item("用户喜欢黑咖啡", "fact"),
    ]
    valid = plugin._validate_distill_output(items, "下周要考试，我平时喜欢黑咖啡")
    assert [it["memory_type"] for it in valid] == ["event", "fact"]


def test_guard_noop_without_transcript(plugin):
    items = [_item("用户生日在9月", "fact")]
    assert plugin._validate_distill_output(items) == items


def test_guard_filters_mixed_batch(plugin):
    """复现 TMEAAA-593 的真实批次：只剔除误记 fact。"""
    transcript = "这月生日，朋友说要给我庆祝一下\n下周六要考试\n今天很不舒服"
    items = [
        _item("用户生日在9月", "fact"),
        _item("用户于2026-10-03参加考试", "event", event_date="2026-10-03"),
    ]
    valid = plugin._validate_distill_output(items, transcript)
    assert len(valid) == 1
    assert valid[0]["memory_type"] == "event"
    assert "生日" not in valid[0]["memory"]
