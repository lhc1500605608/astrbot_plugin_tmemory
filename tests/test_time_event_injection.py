"""TMEAAA-590: 每次注入时间上下文 + 时效 event 仅窗口内注入。

覆盖验收：注入串含当天日期+星期；窗口内 event 带日期注入、`valid_until`
过期 event 不注入；开关关闭 → 无时间行；`inject_max_chars` 对新增块照常截断；
handler 在画像块为空时仍注入时间行。
"""

import datetime

import pytest

UTC = datetime.timezone.utc


@pytest.fixture()
def fmt(plugin_module):
    from astrbot_plugin_tmemory.core.injection import _format_event_date

    return _format_event_date


def _local_today() -> datetime.date:
    return datetime.datetime.now(tz=UTC).astimezone().date()


class DummyReq:
    def __init__(self, prompt: str = "", system_prompt: str = ""):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.extra_user_content_parts = []


class DummyEvent:
    def __init__(self, message_str: str):
        self.message_str = message_str
        self.adapter_name = "qq"

    def get_sender_id(self):
        return "42"

    def get_group_id(self):
        return None


def _seed_event(plugin, text, event_date, valid_until="", canonical_id="u1"):
    return plugin._insert_memory(
        canonical_id, "webchat", canonical_id, text, 0.6, "event", 0.6, 0.6,
        event_date=event_date, valid_until=valid_until,
    )


# ── time block ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_time_block_is_first_even_without_memory(plugin):
    block = await plugin._injection_builder.build_profile_injection(
        "u-none", "query", session_key="s1",
    )
    today = _local_today()
    assert block.startswith("[时间] ")
    assert f"{today:%Y-%m-%d}" in block


@pytest.mark.asyncio
async def test_time_block_disabled_yields_no_time_line(plugin):
    plugin._cfg.inject_time_context = False
    block = await plugin._injection_builder.build_profile_injection(
        "u-none", "query", session_key="s1",
    )
    assert "[时间]" not in block


@pytest.mark.asyncio
async def test_time_block_precedes_other_blocks(plugin):
    today = _local_today()
    _seed_event(plugin, "今天的事", today.isoformat())
    block = await plugin._injection_builder.build_profile_injection(
        "u1", "", session_key="s1",
    )
    assert block.index("[时间]") < block.index("[近期事件]")


# ── event block ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_in_window_injected_with_date(plugin, fmt):
    today = _local_today()
    soon = (today + datetime.timedelta(days=1)).isoformat()
    _seed_event(plugin, "明天要考试", soon)

    block = await plugin._injection_builder.build_profile_injection(
        "u1", "", session_key="s1",
    )
    assert "[近期事件]" in block
    assert "明天要考试" in block
    assert fmt(soon) in block


@pytest.mark.asyncio
async def test_expired_event_not_injected(plugin):
    today = _local_today()
    _seed_event(
        plugin,
        "过期活动",
        (today - datetime.timedelta(days=1)).isoformat(),
        valid_until=(today - datetime.timedelta(days=1)).isoformat(),
    )
    block = await plugin._injection_builder.build_profile_injection(
        "u1", "", session_key="s1",
    )
    assert "[近期事件]" not in block
    assert "过期活动" not in block


@pytest.mark.asyncio
async def test_event_outside_window_not_injected(plugin):
    today = _local_today()
    plugin._cfg.event_inject_window_days = 1
    _seed_event(plugin, "很久以后", (today + datetime.timedelta(days=10)).isoformat())
    block = await plugin._injection_builder.build_profile_injection(
        "u1", "", session_key="s1",
    )
    assert "很久以后" not in block


@pytest.mark.asyncio
async def test_event_block_omitted_when_no_events(plugin):
    block = await plugin._injection_builder.build_profile_injection(
        "u-none", "", session_key="s1",
    )
    assert "[近期事件]" not in block


def test_format_event_date_includes_short_weekday(fmt):
    assert fmt("2026-09-27") == "09-27(周日)"
    assert fmt("") == ""
    assert fmt("garbage") == ""


# ── truncation ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_max_chars_truncates_time_and_event_blocks(plugin):
    today = _local_today()
    _seed_event(plugin, "这是一个很长的近期事件描述" * 5, today.isoformat())
    plugin._cfg.inject_max_chars = 20

    block = await plugin._injection_builder.build_profile_injection(
        "u1", "", session_key="s1",
    )
    assert len(block) <= 23
    assert block.endswith("\u2026")


# ── handler wiring ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_llm_request_injects_time_when_profile_empty(plugin):
    plugin._cfg.enable_memory_injection = True
    plugin._cfg.inject_position = "system_prompt"
    req = DummyReq(prompt="你好", system_prompt="你是AI助手。")

    await plugin.on_llm_request(DummyEvent("你好"), req)

    assert req.system_prompt.startswith("你是AI助手。")
    assert "[时间]" in req.system_prompt


@pytest.mark.asyncio
async def test_on_llm_request_no_time_when_disabled(plugin):
    plugin._cfg.enable_memory_injection = True
    plugin._cfg.inject_time_context = False
    plugin._cfg.inject_position = "system_prompt"
    req = DummyReq(prompt="你好", system_prompt="你是AI助手。")

    await plugin.on_llm_request(DummyEvent("你好"), req)

    assert "[时间]" not in req.system_prompt
