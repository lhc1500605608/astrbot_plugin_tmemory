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
async def test_on_any_message_does_not_capture_when_auto_capture_disabled(plugin):
    plugin._cfg.enable_auto_capture = False

    await plugin.on_any_message(DummyEvent("用户说想喝咖啡"))

    assert plugin._count_pending_rows() == 0


@pytest.mark.asyncio
async def test_on_llm_request_does_not_inject_when_memory_injection_disabled(plugin):
    _insert_memory(plugin, "用户喜欢意式浓缩")
    plugin._cfg.enable_memory_injection = False
    req = DummyRequest(prompt="今天喝什么咖啡？", system_prompt="base system")

    await plugin.on_llm_request(DummyEvent("今天喝什么咖啡？"), req)

    assert req.prompt == "今天喝什么咖啡？"
    assert req.system_prompt == "base system"
