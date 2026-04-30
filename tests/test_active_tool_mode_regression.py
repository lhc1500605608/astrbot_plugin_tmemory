import pytest


class DummyEvent:
    def __init__(self, message_str: str = "/tm_memory"):
        self.message_str = message_str
        self.adapter_name = "qq"

    def get_sender_id(self):
        return "42"

    def plain_result(self, text):
        return text

    def get_group_id(self):
        return None


class DummyRequest:
    def __init__(self, prompt: str, system_prompt: str = ""):
        self.prompt = prompt
        self.system_prompt = system_prompt


async def _collect(async_gen):
    return [item async for item in async_gen]


def _insert_memory(plugin, memory: str) -> int:
    return plugin._insert_memory(
        canonical_id="qq:42",
        adapter="qq",
        adapter_user="42",
        memory=memory,
        score=0.8,
        memory_type="preference",
        importance=0.7,
        confidence=0.9,
    )


@pytest.mark.asyncio
async def test_tm_memory_returns_saved_memory_for_current_identity(plugin):
    _insert_memory(plugin, "用户偏好黑咖啡")

    results = await _collect(plugin.tm_memory(DummyEvent("/tm_memory")))

    assert len(results) == 1
    assert "canonical_id=qq:42" in results[0]
    assert "用户偏好黑咖啡" in results[0]


@pytest.mark.asyncio
async def test_tm_memory_returns_empty_state_when_no_memory_exists(plugin):
    results = await _collect(plugin.tm_memory(DummyEvent("/tm_memory")))

    assert results == ["当前还没有已保存记忆。"]


@pytest.mark.asyncio
async def test_tm_context_returns_recalled_memory_after_save(plugin):
    _insert_memory(plugin, "用户周末喜欢徒步")

    results = await _collect(plugin.tm_context(DummyEvent("/tm_context 徒步去哪儿")))

    assert len(results) == 1
    assert "[Memory Context]" in results[0]
    assert "canonical_user_id=qq:42" in results[0]
    assert "query=徒步去哪儿" in results[0]
    assert "用户周末喜欢徒步" in results[0]


@pytest.mark.asyncio
async def test_tm_context_returns_explicit_empty_recall_when_no_memory_exists(plugin):
    results = await _collect(plugin.tm_context(DummyEvent("/tm_context 今天吃什么")))

    assert len(results) == 1
    assert "[Memory Context]" in results[0]
    assert "Relevant Long-Term Memories:" in results[0]
    assert "- (none) 暂无匹配长期记忆" in results[0]


@pytest.mark.asyncio
async def test_style_distill_accepts_framework_stripped_argument(plugin):
    """event.message_str='on' (framework stripped prefix) — 手动解析回退路径。"""
    plugin._cfg.enable_style_distill = False
    plugin._cfg.distill_pause = True  # ADR TMEAAA-180: 互斥校验需要记忆蒸馏已暂停

    results = await _collect(plugin.style_distill(DummyEvent("on")))

    assert results == ["风格蒸馏采集已开启（不影响普通记忆整理）。"]
    assert plugin._cfg.enable_style_distill is True


@pytest.mark.asyncio
async def test_style_distill_receives_action_via_parsed_params(plugin):
    """action 由框架 parsed_params 注入为位置参数 — AstrBot 命令入参标准契约。"""
    plugin._cfg.enable_style_distill = False
    plugin._cfg.distill_pause = True  # ADR TMEAAA-180: 互斥校验需要记忆蒸馏已暂停

    # event.message_str 为空，action 由框架注入为第三个位置参数
    results = await _collect(plugin.style_distill(DummyEvent("ignored"), "on"))

    assert "已开启" in results[-1]
    assert plugin._cfg.enable_style_distill is True


@pytest.mark.asyncio
async def test_style_distill_action_param_takes_priority_over_message_str(plugin):
    """action 位置参数非空时，跳过 event.message_str 手动解析。"""
    plugin._cfg.enable_style_distill = True

    # event.message_str 说 off，但 action 位置参数说 on → action 优先
    results = await _collect(plugin.style_distill(DummyEvent("off"), "on"))

    # action="on" 优先级高于 message_str="off"，但状态已是 True，所以提示"无需重复设置"
    assert "开启" in results[-1]
    assert "无需重复设置" in results[-1]
    assert plugin._cfg.enable_style_distill is True


@pytest.mark.asyncio
async def test_style_distill_on_rejected_when_memory_distill_active(plugin):
    """ADR TMEAAA-180: /style_distill on 在记忆蒸馏活跃时应拒绝并提示暂停。"""
    plugin._cfg.enable_style_distill = False
    plugin._cfg.distill_pause = False
    plugin._cfg.memory_mode = "hybrid"

    results = await _collect(plugin.style_distill(DummyEvent("on")))

    assert len(results) == 1
    assert "无法开启风格蒸馏" in results[0]
    assert "distill_pause" in results[0]
    assert plugin._cfg.enable_style_distill is False


@pytest.mark.asyncio
async def test_style_distill_on_accepted_when_memory_distill_paused(plugin):
    """ADR TMEAAA-180: /style_distill on 在记忆蒸馏暂停时应正常开启。"""
    plugin._cfg.enable_style_distill = False
    plugin._cfg.distill_pause = True

    results = await _collect(plugin.style_distill(DummyEvent("on")))

    assert len(results) == 1
    assert "已开启" in results[0]
    assert plugin._cfg.enable_style_distill is True


@pytest.mark.asyncio
async def test_tool_remember_rejects_style_always(plugin):
    """ADR TMEAAA-180: tool_remember always rejects style-type memories regardless of enable_style_distill."""
    plugin._cfg.enable_style_distill = False

    result = await plugin.tool_remember(
        DummyEvent("记住"),
        content="用户偏好简洁回复",
        memory_type="style",
    )
    assert "不再通过 remember 工具写入" in result

    # 确认未落库
    memories = plugin._list_memories("qq:42", limit=10)
    assert not any(m["memory_type"] == "style" for m in memories)


@pytest.mark.asyncio
async def test_tool_remember_rejects_style_even_when_distill_enabled(plugin):
    """ADR TMEAAA-180: tool_remember rejects style-type even when enable_style_distill=True."""
    plugin._cfg.enable_style_distill = True

    result = await plugin.tool_remember(
        DummyEvent("记住"),
        content="用户偏好简洁回复",
        memory_type="style",
    )
    assert "不再通过 remember 工具写入" in result

    memories = plugin._list_memories("qq:42", limit=10)
    assert not any(m["memory_type"] == "style" for m in memories)


@pytest.mark.asyncio
async def test_on_any_message_does_not_capture_when_auto_capture_disabled(plugin):
    plugin._cfg.enable_auto_capture = False
    plugin._cfg.enable_style_distill = False

    await plugin.on_any_message(DummyEvent("用户说想喝咖啡"))

    assert plugin._count_pending_rows() == 0


@pytest.mark.asyncio
async def test_on_any_message_skips_command_without_slash_prefix(plugin):
    """AstrBot 剥离 wake_prefix 后 /style_distill on 变成 style_distill on — 必须跳过。"""
    plugin._cfg.enable_auto_capture = True
    plugin._cfg.enable_style_distill = True

    # 模拟 AstrBot 剥离 / 后的无斜杠命令文本
    await plugin.on_any_message(DummyEvent("style_distill on"))
    await plugin.on_any_message(DummyEvent("tm_distill_now"))
    await plugin.on_any_message(DummyEvent("tm_memory"))
    await plugin.on_any_message(DummyEvent("style_bind my_profile"))

    assert plugin._count_pending_rows() == 0, "控制命令文本不应进入 conversation_cache"


@pytest.mark.asyncio
async def test_on_any_message_still_captures_normal_text_without_slash(plugin):
    """无斜杠的普通对话文本仍应正常采集。"""
    plugin._cfg.enable_auto_capture = True
    plugin._cfg.enable_style_distill = False

    await plugin.on_any_message(DummyEvent("我今天心情很好"))
    await plugin.on_any_message(DummyEvent("style 这个词在中文里是风格的意思"))
    await plugin.on_any_message(DummyEvent("tm 是商标的缩写"))

    rows = plugin._fetch_pending_rows("qq:42", 10)
    assert len(rows) == 3, "普通对话（即使含 style_/tm_ 子串但不是首词）应正常采集"


@pytest.mark.asyncio
async def test_on_llm_request_does_not_inject_when_memory_injection_disabled(plugin):
    _insert_memory(plugin, "用户喜欢意式浓缩")
    plugin._cfg.enable_memory_injection = False
    req = DummyRequest(prompt="今天喝什么咖啡？", system_prompt="base system")

    await plugin.on_llm_request(DummyEvent("今天喝什么咖啡？"), req)

    assert req.prompt == "今天喝什么咖啡？"
    assert req.system_prompt == "base system"


@pytest.mark.asyncio
async def test_style_migrate_preview_shows_old_data(plugin):
    """ADR TMEAAA-180: /style_migrate_preview 只读预览旧 style 记忆。"""
    plugin._insert_memory(
        canonical_id="qq:42",
        adapter="qq",
        adapter_user="42",
        memory="用户偏好简洁的风格",
        score=0.8,
        memory_type="style",
        importance=0.7,
        confidence=0.8,
    )

    results = await _collect(plugin.style_migrate_preview(DummyEvent("style_migrate_preview")))

    assert len(results) == 1
    assert "旧 style 记忆预览" in results[0]
    assert "qq:42" in results[0]


@pytest.mark.asyncio
async def test_build_knowledge_injection_excludes_style(plugin):
    """ADR TMEAAA-180: _build_knowledge_injection 不包含 style 类型内容。"""
    plugin._insert_memory(
        canonical_id="qq:42",
        adapter="qq",
        adapter_user="42",
        memory="用户是程序员",
        score=0.8,
        memory_type="fact",
        importance=0.7,
        confidence=0.8,
    )
    plugin._insert_memory(
        canonical_id="qq:42",
        adapter="qq",
        adapter_user="42",
        memory="用户偏好简洁风格",
        score=0.8,
        memory_type="style",
        importance=0.7,
        confidence=0.8,
    )

    block = await plugin._build_knowledge_injection("qq:42", "用户", limit=5)

    assert "用户是程序员" in block
    assert "用户偏好简洁风格" not in block, "style memory should be excluded from knowledge injection"
