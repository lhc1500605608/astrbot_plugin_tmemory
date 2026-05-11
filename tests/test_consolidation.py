"""Integration tests for consolidation pipeline (Stage B + Stage C)."""

import asyncio
import json
import types

import pytest


@pytest.fixture()
def plugin_with_ctx(tmp_path, monkeypatch, plugin_module):
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


# ═══════════════════════════════════════════════════════════════════════════════
# Stage B: Episode Summarization Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_episode_manager_parse_valid_json(plugin):
    """EpisodeManager.parse_episode_json should parse a valid LLM response."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    mgr = EpisodeManager(PluginConfig())
    raw = json.dumps({
        "episode_title": "用户学习Python",
        "episode_summary": "用户表达了学习Python的意愿，计划每天学习一小时，偏好使用VS Code编辑器。",
        "topic_tags": ["Python学习", "编程工具"],
        "key_entities": ["Python", "VS Code"],
        "status": "ongoing",
        "importance": 0.7,
        "confidence": 0.8,
    }, ensure_ascii=False)

    result = mgr.parse_episode_json(raw)
    assert result is not None
    assert result["episode_title"] == "用户学习Python"
    assert result["status"] == "ongoing"
    assert result["importance"] == 0.7
    assert result["confidence"] == 0.8
    assert "Python学习" in result["topic_tags"]


@pytest.mark.asyncio
async def test_episode_manager_parse_with_think_tags(plugin):
    """EpisodeManager should strip think tags before parsing."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    mgr = EpisodeManager(PluginConfig())
    raw = '<think>let me analyze...</think>\n' + json.dumps({
        "episode_title": "Test",
        "episode_summary": "A test episode with enough content to pass validation.",
        "topic_tags": ["test"],
        "key_entities": [],
        "status": "ongoing",
        "importance": 0.5,
        "confidence": 0.6,
    }, ensure_ascii=False)

    result = mgr.parse_episode_json(raw)
    assert result is not None
    assert result["episode_title"] == "Test"


@pytest.mark.asyncio
async def test_episode_manager_parse_invalid_json_returns_none(plugin):
    """EpisodeManager should return None for unparseable input."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    mgr = EpisodeManager(PluginConfig())
    assert mgr.parse_episode_json("") is None
    assert mgr.parse_episode_json("not json at all") is None
    assert mgr.parse_episode_json('{"missing": "fields"}') is None


@pytest.mark.asyncio
async def test_episode_manager_parse_short_summary(plugin):
    """EpisodeManager should reject summaries shorter than 10 chars."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    mgr = EpisodeManager(PluginConfig())
    raw = json.dumps({
        "episode_title": "T",
        "episode_summary": "short",
        "topic_tags": [],
        "key_entities": [],
        "status": "ongoing",
        "importance": 0.5,
        "confidence": 0.5,
    })
    assert mgr.parse_episode_json(raw) is None


@pytest.mark.asyncio
async def test_extractive_summary_fallback(plugin):
    """extractive_summary should produce output from user messages."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    mgr = EpisodeManager(PluginConfig())
    rows = [
        {"role": "user", "content": "我喜欢喝咖啡不加糖"},
        {"role": "assistant", "content": "好的我记住了"},
        {"role": "user", "content": "最近在学习Rust"},
    ]
    result = mgr.extractive_summary(rows)
    assert result["episode_title"] != ""
    assert "咖啡" in result["episode_summary"] or "Rust" in result["episode_summary"]
    assert result["importance"] == 0.3
    assert result["confidence"] == 0.2


@pytest.mark.asyncio
async def test_extractive_summary_empty(plugin):
    """extractive_summary should handle empty input gracefully."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    mgr = EpisodeManager(PluginConfig())
    result = mgr.extractive_summary([])
    assert result["episode_title"] == "未命名对话"
    assert result["episode_summary"] == "(无内容)"


@pytest.mark.asyncio
async def test_group_conversations_into_sessions(plugin):
    """group_conversations_into_sessions should split by time gaps."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    cfg = PluginConfig()
    cfg.episode_session_gap_minutes = 30
    mgr = EpisodeManager(cfg)

    # Two groups separated by >30 min gap
    rows = [
        {"role": "user", "content": "msg1", "created_at": "2026-01-01T10:00:00"},
        {"role": "user", "content": "msg2", "created_at": "2026-01-01T10:05:00"},
        {"role": "user", "content": "msg3", "created_at": "2026-01-01T11:00:00"},
        {"role": "user", "content": "msg4", "created_at": "2026-01-01T11:02:00"},
    ]
    sessions = mgr.group_conversations_into_sessions(rows)
    assert len(sessions) == 2
    assert len(sessions[0]) == 2
    assert len(sessions[1]) == 2


@pytest.mark.asyncio
async def test_group_conversations_single_session(plugin):
    """All messages within gap should form a single session."""
    from astrbot_plugin_tmemory.core.consolidation import EpisodeManager
    from astrbot_plugin_tmemory.core.config import PluginConfig

    cfg = PluginConfig()
    cfg.episode_session_gap_minutes = 60
    mgr = EpisodeManager(cfg)

    rows = [
        {"role": "user", "content": "msg1", "created_at": "2026-01-01T10:00:00"},
        {"role": "user", "content": "msg2", "created_at": "2026-01-01T10:20:00"},
        {"role": "user", "content": "msg3", "created_at": "2026-01-01T10:40:00"},
    ]
    sessions = mgr.group_conversations_into_sessions(rows)
    assert len(sessions) == 1
    assert len(sessions[0]) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Stage C: Semantic Extraction Tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_semantic_extractor_parse_valid_json(plugin):
    """SemanticExtractor should parse valid LLM memory output."""
    from astrbot_plugin_tmemory.core.consolidation import SemanticExtractor
    from astrbot_plugin_tmemory.core.config import PluginConfig

    extractor = SemanticExtractor(PluginConfig())

    def norm(text):
        return text.strip()

    def safe_type(t):
        return str(t) if str(t) in {"preference", "fact", "task", "restriction", "style"} else "fact"

    def clamp01(v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    raw = json.dumps({
        "memories": [
            {
                "memory": "用户偏好使用 Python 编程",
                "memory_type": "preference",
                "importance": 0.8,
                "confidence": 0.9,
                "score": 0.85,
            }
        ]
    }, ensure_ascii=False)

    items = extractor.parse_memories_json(raw, norm, safe_type, clamp01)
    assert len(items) == 1
    assert items[0]["memory"] == "用户偏好使用 Python 编程"
    assert items[0]["memory_type"] == "preference"
    assert items[0]["importance"] == 0.8


@pytest.mark.asyncio
async def test_semantic_extractor_parse_empty(plugin):
    """SemanticExtractor should handle empty memories array."""
    from astrbot_plugin_tmemory.core.consolidation import SemanticExtractor
    from astrbot_plugin_tmemory.core.config import PluginConfig

    extractor = SemanticExtractor(PluginConfig())
    items = extractor.parse_memories_json(
        '{"memories": []}',
        lambda x: x.strip(),
        lambda x: str(x),
        lambda x: float(x) if x else 0.5,
    )
    assert items == []


@pytest.mark.asyncio
async def test_semantic_extractor_parse_invalid(plugin):
    """SemanticExtractor should return empty for invalid input."""
    from astrbot_plugin_tmemory.core.consolidation import SemanticExtractor
    from astrbot_plugin_tmemory.core.config import PluginConfig

    extractor = SemanticExtractor(PluginConfig())
    identity = lambda x: x
    assert extractor.parse_memories_json("", identity, identity, identity) == []
    assert extractor.parse_memories_json("garbage", identity, identity, identity) == []
    assert extractor.parse_memories_json('{"not_memories": []}', identity, identity, identity) == []


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: Stage B + C with mocked LLM
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_consolidation_pipeline_happy_path(plugin_with_ctx):
    """End-to-end: insert conversations, run consolidation, verify episodes + memories."""
    plugin, ctx = plugin_with_ctx

    # Enable consolidation
    plugin._cfg.enable_consolidation_pipeline = True
    plugin._cfg.enable_episodic_summarization = True
    plugin._cfg.enable_episode_semantic_distill = True
    plugin._cfg.episode_summary_min_messages = 2
    plugin._cfg.stage_timeout_sec = 30
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    # Insert conversations for one user (same session = close timestamps)
    now = plugin._now()
    for i, (role, content) in enumerate([
        ("user", "我最近在学习 Python，感觉很有意思"),
        ("assistant", "Python 确实很棒！"),
        ("user", "我想用 Python 做数据分析，需要学 pandas 吗？"),
        ("assistant", "是的，pandas 是数据分析的基础库"),
        ("user", "好的，那我从 pandas 开始学起"),
    ]):
        plugin._insert_conversation_sync(
            canonical_id="test-user",
            role=role,
            content=content,
            source_adapter="qq",
            source_user_id="42",
            unified_msg_origin="group:1",
            scope="user",
            persona_id="",
        )

    # Verify pending rows
    pending_users = plugin._pending_consolidation_users(limit=10, min_batch=2)
    assert "test-user" in pending_users

    # Mock LLM response for Stage B (episode summarization)
    episode_json = json.dumps({
        "episode_title": "用户学习Python数据分析",
        "episode_summary": "用户表达了学习Python的强烈兴趣，计划从pandas开始学习数据分析。用户对编程有持续热情。",
        "topic_tags": ["Python学习", "数据分析", "pandas"],
        "key_entities": ["Python", "pandas", "数据分析"],
        "status": "ongoing",
        "importance": 0.75,
        "confidence": 0.85,
    }, ensure_ascii=False)

    # Mock LLM response for Stage C (semantic extraction)
    memory_json = json.dumps({
        "memories": [
            {
                "memory": "用户偏好使用 Python 编程语言",
                "memory_type": "preference",
                "importance": 0.8,
                "confidence": 0.9,
                "score": 0.85,
            },
            {
                "memory": "用户正在学习数据分析，从 pandas 开始",
                "memory_type": "fact",
                "importance": 0.7,
                "confidence": 0.85,
                "score": 0.8,
            },
        ]
    }, ensure_ascii=False)

    call_count = [0]

    async def fake_llm(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # Stage B response
            return types.SimpleNamespace(
                completion_text=episode_json,
                usage=types.SimpleNamespace(input_other=100, input_cached=0, output=50),
            )
        else:
            # Stage C response
            return types.SimpleNamespace(
                completion_text=memory_json,
                usage=types.SimpleNamespace(input_other=50, input_cached=0, output=40),
            )

    ctx.llm_generate = fake_llm

    # Run consolidation
    episodes_created, memories_extracted = await plugin._run_consolidation_cycle(
        force=True, trigger="test-happy"
    )

    assert episodes_created == 1
    assert memories_extracted == 2
    assert call_count[0] == 2  # 1 for Stage B, 1 for Stage C

    # Verify episode in DB
    with plugin._db() as conn:
        ep_row = conn.execute(
            "SELECT * FROM memory_episodes WHERE canonical_user_id='test-user'"
        ).fetchone()
        assert ep_row is not None
        assert ep_row["consolidation_status"] == "semantic_done"
        assert ep_row["episode_title"] == "用户学习Python数据分析"

        # Verify episode_sources
        src_rows = conn.execute(
            "SELECT * FROM episode_sources WHERE episode_id=?", (ep_row["id"],)
        ).fetchall()
        assert len(src_rows) == 5

        # Verify memories created
        mem_rows = conn.execute(
            "SELECT * FROM memories WHERE canonical_user_id='test-user' AND derived_from='episode'"
        ).fetchall()
        assert len(mem_rows) == 2


@pytest.mark.asyncio
async def test_consolidation_episode_llm_failure_fallback(plugin_with_ctx):
    """When Stage B LLM fails, extractive fallback should still create an episode."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.enable_consolidation_pipeline = True
    plugin._cfg.enable_episodic_summarization = True
    plugin._cfg.enable_episode_semantic_distill = False  # only Stage B
    plugin._cfg.episode_summary_min_messages = 2
    plugin._cfg.stage_timeout_sec = 30
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    plugin._insert_conversation_sync(
        canonical_id="fallback-user",
        role="user",
        content="我喜欢喝黑咖啡不加糖",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )
    plugin._insert_conversation_sync(
        canonical_id="fallback-user",
        role="user",
        content="最近在学习 Rust 编程语言",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    # LLM throws exception
    async def fake_llm_fail(**kwargs):
        raise RuntimeError("provider unavailable")

    ctx.llm_generate = fake_llm_fail

    episodes_created, memories_extracted = await plugin._run_consolidation_cycle(
        force=True, trigger="test-fallback"
    )

    assert episodes_created == 1
    assert memories_extracted == 0

    with plugin._db() as conn:
        ep = conn.execute(
            "SELECT * FROM memory_episodes WHERE canonical_user_id='fallback-user'"
        ).fetchone()
        assert ep is not None
        # Extractive fallback should have lower confidence
        assert ep["confidence"] <= 0.3


@pytest.mark.asyncio
async def test_consolidation_episode_parse_failure_retry(plugin_with_ctx):
    """When first LLM response is invalid JSON, retry should succeed."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.enable_consolidation_pipeline = True
    plugin._cfg.enable_episodic_summarization = True
    plugin._cfg.enable_episode_semantic_distill = False
    plugin._cfg.episode_summary_min_messages = 2
    plugin._cfg.stage_timeout_sec = 30
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    plugin._insert_conversation_sync(
        canonical_id="retry-user",
        role="user",
        content="我在学习机器学习的知识",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )
    plugin._insert_conversation_sync(
        canonical_id="retry-user",
        role="user",
        content="对深度学习特别感兴趣",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    call_count = [0]
    valid_json = json.dumps({
        "episode_title": "用户学习机器学习",
        "episode_summary": "用户正在学习机器学习，对深度学习有浓厚兴趣。",
        "topic_tags": ["机器学习", "深度学习"],
        "key_entities": ["机器学习", "深度学习"],
        "status": "ongoing",
        "importance": 0.7,
        "confidence": 0.8,
    }, ensure_ascii=False)

    async def fake_llm_retry(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: invalid JSON
            return types.SimpleNamespace(
                completion_text="this is not json at all, just some text",
                usage=None,
            )
        else:
            # Second call (retry with stricter prompt): valid
            return types.SimpleNamespace(
                completion_text=valid_json,
                usage=None,
            )

    ctx.llm_generate = fake_llm_retry

    episodes_created, _ = await plugin._run_consolidation_cycle(
        force=True, trigger="test-retry"
    )

    assert episodes_created == 1
    assert call_count[0] == 2  # first + retry


@pytest.mark.asyncio
async def test_consolidation_stage_c_empty_extraction(plugin_with_ctx):
    """When Stage C LLM returns empty memories, episode should be marked semantic_done."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.enable_consolidation_pipeline = True
    plugin._cfg.enable_episodic_summarization = True
    plugin._cfg.enable_episode_semantic_distill = True
    plugin._cfg.episode_summary_min_messages = 2
    plugin._cfg.stage_timeout_sec = 30
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    plugin._insert_conversation_sync(
        canonical_id="empty-extract-user",
        role="user",
        content="你好",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )
    plugin._insert_conversation_sync(
        canonical_id="empty-extract-user",
        role="user",
        content="今天天气怎么样",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    episode_resp = json.dumps({
        "episode_title": "日常寒暄",
        "episode_summary": "用户进行了简单的日常寒暄，询问天气情况，没有实质性内容。",
        "topic_tags": ["日常"],
        "key_entities": [],
        "status": "background",
        "importance": 0.2,
        "confidence": 0.9,
    }, ensure_ascii=False)

    empty_memory_resp = '{"memories": []}'

    call_count = [0]

    async def fake_llm(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return types.SimpleNamespace(
                completion_text=episode_resp,
                usage=types.SimpleNamespace(input_other=30, input_cached=0, output=30),
            )
        else:
            return types.SimpleNamespace(
                completion_text=empty_memory_resp,
                usage=types.SimpleNamespace(input_other=20, input_cached=0, output=5),
            )

    ctx.llm_generate = fake_llm

    episodes_created, memories_extracted = await plugin._run_consolidation_cycle(
        force=True, trigger="test-empty"
    )

    assert episodes_created == 1
    assert memories_extracted == 0

    with plugin._db() as conn:
        ep = conn.execute(
            "SELECT * FROM memory_episodes WHERE canonical_user_id='empty-extract-user'"
        ).fetchone()
        assert ep is not None
        assert ep["consolidation_status"] == "semantic_done"


@pytest.mark.asyncio
async def test_consolidation_disabled_skips_pipeline(plugin_with_ctx):
    """When enable_consolidation_pipeline is False, no episodes should be created."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.enable_consolidation_pipeline = False
    plugin._cfg.enable_episodic_summarization = True
    plugin._cfg.enable_episode_semantic_distill = True

    plugin._insert_conversation_sync(
        canonical_id="disabled-user",
        role="user",
        content="我有重要信息需要被记录和蒸馏",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    ctx.llm_generate = None  # should never be called via consolidation

    episodes_created, memories_extracted = await plugin._run_consolidation_cycle(
        force=True, trigger="test-disabled"
    )

    assert episodes_created == 0
    assert memories_extracted == 0


@pytest.mark.asyncio
async def test_consolidation_min_messages_threshold(plugin_with_ctx):
    """Fewer than episode_summary_min_messages should not trigger summarization."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.enable_consolidation_pipeline = True
    plugin._cfg.enable_episodic_summarization = True
    plugin._cfg.enable_episode_semantic_distill = False
    plugin._cfg.episode_summary_min_messages = 5  # need at least 5

    plugin._insert_conversation_sync(
        canonical_id="few-msgs-user",
        role="user",
        content="只有一条消息",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    ctx.llm_generate = None

    episodes_created, _ = await plugin._run_consolidation_cycle(
        force=False, trigger="test-threshold"
    )

    assert episodes_created == 0


@pytest.mark.asyncio
async def test_consolidation_blocks_flat_distill_for_same_rows(plugin_with_ctx):
    """Pipeline enabled: source rows processed by consolidation must NOT be
    reprocessed by flat distill. This prevents double LLM cost and
    duplicate/conflicting semantic memories."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.enable_consolidation_pipeline = True
    plugin._cfg.enable_episodic_summarization = True
    plugin._cfg.enable_episode_semantic_distill = True
    plugin._cfg.episode_summary_min_messages = 2
    plugin._cfg.distill_min_batch_count = 1
    plugin._cfg.stage_timeout_sec = 30
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    # Insert conversations for one user
    for i in range(3):
        plugin._insert_conversation_sync(
            canonical_id="dual-user",
            role="user",
            content=f"这是第{i+1}条测试消息，包含有意义的内容用于蒸馏",
            source_adapter="qq",
            source_user_id="42",
            unified_msg_origin="group:1",
        )

    ep_json = json.dumps({
        "episode_title": "测试对话",
        "episode_summary": "用户发送了多条测试消息，包含有意义的内容。",
        "topic_tags": ["测试"],
        "key_entities": [],
        "status": "ongoing",
        "importance": 0.5,
        "confidence": 0.7,
    }, ensure_ascii=False)

    mem_json = json.dumps({
        "memories": [
            {
                "memory": "用户偏好参与有意义的对话测试",
                "memory_type": "preference",
                "importance": 0.5,
                "confidence": 0.7,
                "score": 0.6,
            }
        ]
    }, ensure_ascii=False)

    call_seq = [0]

    async def fake_llm(**kwargs):
        call_seq[0] += 1
        n = call_seq[0]
        if n == 1:
            return types.SimpleNamespace(completion_text=ep_json, usage=None)
        elif n == 2:
            return types.SimpleNamespace(completion_text=mem_json, usage=None)
        raise RuntimeError(f"unexpected LLM call #{n}")

    ctx.llm_generate = fake_llm
    plugin._cfg.use_independent_distill_model = True
    plugin._cfg.distill_provider_id = "mock-provider"

    # Run consolidation (Stages B + C)
    episodes_created, memories_extracted = await plugin._run_consolidation_cycle(
        force=True, trigger="test-dual"
    )
    assert episodes_created == 1
    assert memories_extracted == 1

    # Verify source rows now have episode_id set
    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT id, episode_id, distilled FROM conversation_cache WHERE canonical_user_id='dual-user'"
        ).fetchall()
        for r in rows:
            assert r["episode_id"] != 0, f"row {r['id']} should have episode_id set"

    # Flat distill should find NO pending rows (episode_id != 0 excludes them)
    processed, total, _errs = await plugin._run_distill_cycle(force=True, trigger="test-dual")
    assert processed == 0, (
        f"flat distill should skip consolidation-processed rows, got {processed}"
    )

    # LLM should NOT have been called for flat distill (only 2 calls for Stages B+C)
    assert call_seq[0] == 2, f"expected 2 LLM calls, got {call_seq[0]}"


@pytest.mark.asyncio
async def test_flat_distill_still_works_when_pipeline_disabled(plugin_with_ctx):
    """Pipeline disabled: flat distill must still process undistilled rows as before."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.enable_consolidation_pipeline = False
    plugin._cfg.distill_min_batch_count = 1
    plugin._cfg.use_independent_distill_model = True
    plugin._cfg.distill_provider_id = "mock-provider"

    for i in range(2):
        plugin._insert_conversation_sync(
            canonical_id="flat-only-user",
            role="user",
            content=f"第{i+1}条测试消息，包含有意义的内容",
            source_adapter="qq",
            source_user_id="1",
            unified_msg_origin="group:1",
        )

    distill_json = json.dumps({
        "memories": [
            {
                "memory": "用户参与了有意义的测试对话",
                "memory_type": "fact",
                "importance": 0.6,
                "confidence": 0.8,
                "score": 0.7,
            }
        ]
    }, ensure_ascii=False)

    call_count = [0]

    async def fake_llm(**kwargs):
        call_count[0] += 1
        return types.SimpleNamespace(completion_text=distill_json, usage=None)

    ctx.llm_generate = fake_llm

    processed, total, _errs = await plugin._run_distill_cycle(force=True, trigger="test-flat-only")
    assert processed >= 1
    assert call_count[0] >= 1
