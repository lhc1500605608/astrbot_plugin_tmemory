"""Tests for profile storage layer: schema init, CRUD, state machine, evidence, merge.

Covers TMEAAA-283 acceptance criteria:
- New schema initializes correctly on empty DB
- Profile items can be created from conversation_cache with evidence
- merge_identity() handles profile_* tables with conflict resolution
"""

import json
import types

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Schema initialization
# ═══════════════════════════════════════════════════════════════════════════════


def test_profile_tables_created_on_init(plugin):
    """All four profile tables + indexes should exist after init_db."""
    with plugin._db() as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "user_profiles" in tables
    assert "profile_items" in tables
    assert "profile_item_evidence" in tables
    assert "profile_relations" in tables


def test_profile_items_check_constraints(plugin):
    """CHECK constraints should reject invalid facet_type and status."""
    now = plugin._now()
    with plugin._db() as conn:
        # Invalid facet_type
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO profile_items(canonical_user_id, facet_type, content, normalized_content, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("u1", "invalid_facet", "test", "test", now, now),
            )
        # Invalid status
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO profile_items(canonical_user_id, facet_type, content, normalized_content, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                ("u1", "fact", "test", "test", "invalid_status", now, now),
            )


def test_profile_items_unique_constraint(plugin):
    """UNIQUE constraint should prevent duplicate (user, facet, normalized, persona, scope)."""
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, normalized_content, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            ("u1", "fact", "user is a programmer", "user is a programmer", now, now),
        )
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO profile_items(canonical_user_id, facet_type, content, normalized_content, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("u1", "fact", "User is a programmer", "user is a programmer", now, now),
            )


def test_profile_relations_check_from_neq_to(plugin):
    """from_item_id must not equal to_item_id."""
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, normalized_content, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
            ("u1", "fact", "test", "test", now, now),
        )
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO profile_relations(canonical_user_id, from_item_id, to_item_id, relation_type, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                ("u1", item_id, item_id, "supports", now, now),
            )


# ═══════════════════════════════════════════════════════════════════════════════
# ProfileItemOps: upsert, evidence, state transitions
# ═══════════════════════════════════════════════════════════════════════════════


def test_upsert_profile_item_insert_new(plugin):
    """First upsert should create a new active item and user_profile."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ops = ProfileItemOps(plugin)
    item_id = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="preference",
        title="偏好Python",
        content="用户偏好使用 Python 编程语言",
        confidence=0.8,
        importance=0.7,
    )
    assert item_id > 0

    with plugin._db() as conn:
        row = conn.execute(
            "SELECT * FROM profile_items WHERE id=?", (item_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "active"
        assert row["facet_type"] == "preference"
        assert row["content"] == "用户偏好使用 Python 编程语言"
        assert row["confidence"] == 0.8

        # user_profile should be auto-created
        up = conn.execute(
            "SELECT * FROM user_profiles WHERE canonical_user_id='test-user'"
        ).fetchone()
        assert up is not None


def test_upsert_profile_item_reinforce_existing(plugin):
    """Second upsert with same normalized_content should reinforce, not duplicate."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ops = ProfileItemOps(plugin)
    id1 = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="fact",
        title="",
        content="用户是一名软件工程师",
        confidence=0.6,
        importance=0.5,
    )
    id2 = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="fact",
        title="",
        content="用户是一名软件工程师",
        confidence=0.9,
        importance=0.8,
    )
    assert id1 == id2

    with plugin._db() as conn:
        row = conn.execute(
            "SELECT * FROM profile_items WHERE id=?", (id1,)
        ).fetchone()
        assert row["confidence"] == 0.9  # MAX(0.6, 0.9)
        assert row["importance"] == 0.8  # MAX(0.5, 0.8)
        # Only one row should exist
        count = conn.execute(
            "SELECT COUNT(*) FROM profile_items WHERE canonical_user_id='test-user'"
        ).fetchone()[0]
        assert count == 1


def test_add_evidence(plugin):
    """Evidence rows should link profile item to conversation_cache sources."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    # Insert conversation_cache rows first
    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            ("test-user", "user", "Hello", now),
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    ops = ProfileItemOps(plugin)
    item_id = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="fact",
        title="",
        content="test content",
        confidence=0.7,
        importance=0.6,
    )
    ops.add_evidence(
        profile_item_id=item_id,
        canonical_user_id="test-user",
        source_ids=[cid],
        source_role="user",
        evidence_kind="conversation",
        confidence_delta=0.1,
    )

    with plugin._db() as conn:
        ev = conn.execute(
            "SELECT * FROM profile_item_evidence WHERE profile_item_id=?",
            (item_id,),
        ).fetchone()
        assert ev is not None
        assert ev["conversation_cache_id"] == cid
        assert ev["evidence_kind"] == "conversation"


def test_supersede_item(plugin):
    """Supersede should mark old item as superseded and create relation."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ops = ProfileItemOps(plugin)
    old_id = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="fact",
        title="",
        content="old fact about user",
        confidence=0.7,
        importance=0.6,
    )
    new_id = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="fact",
        title="",
        content="updated fact about user",
        confidence=0.9,
        importance=0.8,
    )
    assert old_id != new_id

    ops.supersede_item(old_id, new_id)

    with plugin._db() as conn:
        old = conn.execute(
            "SELECT status FROM profile_items WHERE id=?", (old_id,)
        ).fetchone()
        assert old["status"] == "superseded"

        rel = conn.execute(
            "SELECT * FROM profile_relations WHERE from_item_id=? AND to_item_id=?",
            (new_id, old_id),
        ).fetchone()
        assert rel is not None
        assert rel["relation_type"] == "supersedes"


def test_mark_contradicted(plugin):
    """Contradict should mark one item contradicted and create contradicts relation."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ops = ProfileItemOps(plugin)
    id_a = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="preference",
        title="",
        content="用户喜欢静态类型语言",
        confidence=0.8,
        importance=0.7,
    )
    id_b = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="preference",
        title="",
        content="用户喜欢动态类型语言",
        confidence=0.75,
        importance=0.65,
    )

    ops.mark_contradicted(id_a, id_b, "test-user")

    with plugin._db() as conn:
        b = conn.execute(
            "SELECT status FROM profile_items WHERE id=?", (id_b,)
        ).fetchone()
        assert b["status"] == "contradicted"

        rel = conn.execute(
            "SELECT * FROM profile_relations WHERE from_item_id=? AND to_item_id=?",
            (id_a, id_b),
        ).fetchone()
        assert rel is not None
        assert rel["relation_type"] == "contradicts"


def test_archive_item(plugin):
    """Archive should soft-delete item and its relations."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ops = ProfileItemOps(plugin)
    item_id = ops.upsert_profile_item(
        canonical_id="test-user",
        facet_type="fact",
        title="",
        content="temporary fact",
        confidence=0.5,
        importance=0.4,
    )
    ops.archive_item(item_id)

    with plugin._db() as conn:
        row = conn.execute(
            "SELECT status FROM profile_items WHERE id=?", (item_id,)
        ).fetchone()
        assert row["status"] == "archived"


# ═══════════════════════════════════════════════════════════════════════════════
# merge_identity with profile tables
# ═══════════════════════════════════════════════════════════════════════════════


def test_merge_identity_no_duplicates(plugin):
    """Merge distinct profile items from from_id to to_id."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps
    from astrbot_plugin_tmemory.core.identity import IdentityManager

    mgr = IdentityManager(plugin._db_mgr, plugin._cfg, plugin._memory_logger)
    ops = ProfileItemOps(plugin)

    # Create items for from_id
    ops.upsert_profile_item("from-user", "fact", "", "from-specific fact", 0.7, 0.6)
    ops.upsert_profile_item("from-user", "preference", "", "from-specific pref", 0.8, 0.7)

    # Create items for to_id
    ops.upsert_profile_item("to-user", "fact", "", "to-specific fact", 0.6, 0.5)

    mgr.merge_identity("from-user", "to-user")

    with plugin._db() as conn:
        from_count = conn.execute(
            "SELECT COUNT(*) FROM profile_items WHERE canonical_user_id='from-user'"
        ).fetchone()[0]
        assert from_count == 0

        to_items = conn.execute(
            "SELECT * FROM profile_items WHERE canonical_user_id='to-user'"
        ).fetchall()
        assert len(to_items) == 3

        # from_user profile should be deleted
        up = conn.execute(
            "SELECT * FROM user_profiles WHERE canonical_user_id='from-user'"
        ).fetchone()
        assert up is None


def test_merge_identity_duplicate_items(plugin):
    """Merge duplicate profile items: survivor keeps id, scores merged."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps
    from astrbot_plugin_tmemory.core.identity import IdentityManager

    mgr = IdentityManager(plugin._db_mgr, plugin._cfg, plugin._memory_logger)
    ops = ProfileItemOps(plugin)

    # Same facet + same normalized_content for both users
    ops.upsert_profile_item("from-user", "fact", "", "用户是软件工程师", 0.6, 0.5)
    ops.upsert_profile_item("to-user", "fact", "", "用户是软件工程师", 0.9, 0.8)

    mgr.merge_identity("from-user", "to-user")

    with plugin._db() as conn:
        items = conn.execute(
            "SELECT * FROM profile_items WHERE canonical_user_id='to-user' AND normalized_content='用户是软件工程师'"
        ).fetchall()
        assert len(items) == 1
        survivor = items[0]
        assert survivor["confidence"] == 0.9  # MAX(0.9, 0.6)
        assert survivor["importance"] == 0.8  # MAX(0.8, 0.5)


def test_merge_identity_moves_evidence(plugin):
    """Evidence from from_id items should be reassigned to survivor."""
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps
    from astrbot_plugin_tmemory.core.identity import IdentityManager

    mgr = IdentityManager(plugin._db_mgr, plugin._cfg, plugin._memory_logger)
    ops = ProfileItemOps(plugin)

    now = plugin._now()
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            ("from-user", "user", "msg1", now),
        )
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    item_id = ops.upsert_profile_item("from-user", "fact", "", "unique from fact", 0.7, 0.6)
    ops.add_evidence(item_id, "from-user", [cid], "user", "conversation")

    mgr.merge_identity("from-user", "to-user")

    with plugin._db() as conn:
        ev = conn.execute(
            "SELECT * FROM profile_item_evidence WHERE profile_item_id=?",
            (item_id,),
        ).fetchone()
        assert ev is not None
        assert ev["canonical_user_id"] == "to-user"


def test_merge_identity_updates_bindings_and_cache(plugin):
    """merge_identity should update identity_bindings and conversation_cache."""
    from astrbot_plugin_tmemory.core.identity import IdentityManager

    mgr = IdentityManager(plugin._db_mgr, plugin._cfg, plugin._memory_logger)
    now = plugin._now()

    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO identity_bindings(adapter, adapter_user_id, canonical_user_id) VALUES(?, ?, ?)",
            ("qq", "123", "from-user"),
        )
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            ("from-user", "user", "msg", now),
        )

    mgr.merge_identity("from-user", "to-user")

    with plugin._db() as conn:
        ib = conn.execute(
            "SELECT canonical_user_id FROM identity_bindings WHERE adapter='qq' AND adapter_user_id='123'"
        ).fetchone()
        assert ib["canonical_user_id"] == "to-user"

        cc = conn.execute(
            "SELECT canonical_user_id FROM conversation_cache WHERE content='msg'"
        ).fetchone()
        assert cc["canonical_user_id"] == "to-user"


# ═══════════════════════════════════════════════════════════════════════════════
# ProfileExtractor: prompt building and JSON parsing
# ═══════════════════════════════════════════════════════════════════════════════


def test_profile_extractor_parse_valid_json(plugin):
    """ProfileExtractor should parse valid LLM output into profile items."""
    from astrbot_plugin_tmemory.core.consolidation import ProfileExtractor
    from astrbot_plugin_tmemory.core.config import PluginConfig

    extractor = ProfileExtractor(PluginConfig())
    raw = json.dumps({
        "profile_items": [
            {
                "facet_type": "preference",
                "title": "编程语言偏好",
                "content": "用户偏好使用 Python 编程语言",
                "importance": 0.8,
                "confidence": 0.9,
            },
            {
                "facet_type": "fact",
                "title": "职业",
                "content": "用户是一名后端工程师",
                "importance": 0.7,
                "confidence": 0.85,
            },
        ]
    }, ensure_ascii=False)

    items = extractor.parse_profile_json(
        raw,
        lambda x: x.strip(),
        ProfileExtractor.safe_facet_type,
        lambda v: max(0.0, min(1.0, float(v))),
    )
    assert len(items) == 2
    assert items[0]["facet_type"] == "preference"
    assert items[0]["content"] == "用户偏好使用 Python 编程语言"
    assert items[1]["facet_type"] == "fact"
    assert items[1]["importance"] == 0.7


def test_profile_extractor_parse_empty(plugin):
    """ProfileExtractor should handle empty profiles array."""
    from astrbot_plugin_tmemory.core.consolidation import ProfileExtractor
    from astrbot_plugin_tmemory.core.config import PluginConfig

    extractor = ProfileExtractor(PluginConfig())
    identity = lambda x: x
    assert extractor.parse_profile_json("", identity, identity, identity) == []
    assert extractor.parse_profile_json(
        '{"profile_items": []}', identity, identity, identity
    ) == []
    assert extractor.parse_profile_json("garbage", identity, identity, identity) == []


def test_profile_extractor_parse_with_think_tags(plugin):
    """ProfileExtractor should strip think tags before parsing."""
    from astrbot_plugin_tmemory.core.consolidation import ProfileExtractor
    from astrbot_plugin_tmemory.core.config import PluginConfig

    extractor = ProfileExtractor(PluginConfig())
    raw = '<think>analyze...</think>\n' + json.dumps({
        "profile_items": [
            {
                "facet_type": "style",
                "title": "沟通风格",
                "content": "用户沟通风格简洁直接",
                "importance": 0.6,
                "confidence": 0.7,
            }
        ]
    }, ensure_ascii=False)

    items = extractor.parse_profile_json(
        raw,
        lambda x: x.strip(),
        ProfileExtractor.safe_facet_type,
        lambda v: max(0.0, min(1.0, float(v))),
    )
    assert len(items) == 1
    assert items[0]["facet_type"] == "style"


def test_safe_facet_type_mapping(plugin):
    """safe_facet_type should map synonyms to canonical facet types."""
    from astrbot_plugin_tmemory.core.consolidation import ProfileExtractor

    assert ProfileExtractor.safe_facet_type("preference") == "preference"
    assert ProfileExtractor.safe_facet_type("pref") == "preference"
    assert ProfileExtractor.safe_facet_type("task") == "task_pattern"
    assert ProfileExtractor.safe_facet_type("constraint") == "restriction"
    assert ProfileExtractor.safe_facet_type("styles") == "style"
    assert ProfileExtractor.safe_facet_type("unknown_x") == "fact"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: profile extraction pipeline with mocked LLM
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def plugin_with_ctx(tmp_path, monkeypatch, plugin_module):
    monkeypatch.chdir(tmp_path)

    class _MockContext:
        async def llm_generate(self, **kwargs):
            raise NotImplementedError

        async def get_current_chat_provider_id(self, **kwargs):
            return None

    ctx = _MockContext()
    instance = plugin_module.TMemoryPlugin(context=ctx, config={})
    instance._init_db()
    instance._migrate_schema()
    yield instance, ctx
    instance._close_db()


@pytest.mark.asyncio
async def test_profile_extraction_happy_path(plugin_with_ctx):
    """End-to-end: insert conversations, run profile extraction, verify profile_items + evidence."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.profile_extraction_enabled = True
    plugin._cfg.profile_extraction_min_messages = 2
    plugin._cfg.profile_extraction_timeout_sec = 30
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    # Insert conversations
    now = plugin._now()
    for role, content in [
        ("user", "我最近在学习 Python，感觉很有意思"),
        ("assistant", "Python 确实很棒！"),
        ("user", "我想用 Python 做数据分析，需要学 pandas 吗？"),
        ("user", "我平时喜欢用 VS Code 写代码"),
    ]:
        plugin._insert_conversation_sync(
            canonical_id="test-user",
            role=role,
            content=content,
            source_adapter="qq",
            source_user_id="42",
            unified_msg_origin="group:1",
        )

    profile_json = json.dumps({
        "profile_items": [
            {
                "facet_type": "preference",
                "title": "编程语言偏好",
                "content": "用户偏好使用 Python 编程语言",
                "importance": 0.8,
                "confidence": 0.9,
            },
            {
                "facet_type": "preference",
                "title": "编辑器偏好",
                "content": "用户偏好使用 VS Code 编辑器",
                "importance": 0.7,
                "confidence": 0.85,
            },
        ]
    }, ensure_ascii=False)

    async def fake_llm(**kwargs):
        return types.SimpleNamespace(
            completion_text=profile_json,
            usage=types.SimpleNamespace(input_other=100, input_cached=0, output=50),
        )

    ctx.llm_generate = fake_llm

    items = await plugin._run_profile_extraction_cycle(force=True, trigger="test")

    assert items == 2

    with plugin._db() as conn:
        profile_items = conn.execute(
            "SELECT * FROM profile_items WHERE canonical_user_id='test-user'"
        ).fetchall()
        assert len(profile_items) == 2

        evidence = conn.execute(
            "SELECT * FROM profile_item_evidence WHERE canonical_user_id='test-user'"
        ).fetchall()
        assert len(evidence) > 0

        # Source rows should be marked distilled
        pending = conn.execute(
            "SELECT COUNT(*) FROM conversation_cache WHERE canonical_user_id='test-user' AND distilled=0"
        ).fetchone()[0]
        assert pending == 0


@pytest.mark.asyncio
async def test_profile_extraction_disabled_skips(plugin_with_ctx):
    """When profile_extraction_enabled is False, nothing should happen."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.profile_extraction_enabled = False

    plugin._insert_conversation_sync(
        canonical_id="test-user",
        role="user",
        content="test message",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    items = await plugin._run_profile_extraction_cycle(force=True, trigger="test")
    assert items == 0


@pytest.mark.asyncio
async def test_profile_extraction_empty_llm_result(plugin_with_ctx):
    """When LLM returns empty profile_items, rows should still be marked distilled."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.profile_extraction_enabled = True
    plugin._cfg.profile_extraction_min_messages = 1
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    for i in range(2):
        plugin._insert_conversation_sync(
            canonical_id="test-user",
            role="user",
            content=f"test message {i}",
            source_adapter="qq",
            source_user_id="1",
            unified_msg_origin="group:1",
        )

    async def fake_llm(**kwargs):
        return types.SimpleNamespace(
            completion_text='{"profile_items": []}',
            usage=None,
        )

    ctx.llm_generate = fake_llm

    items = await plugin._run_profile_extraction_cycle(force=True, trigger="test")

    assert items == 0

    with plugin._db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM conversation_cache WHERE canonical_user_id='test-user' AND distilled=0"
        ).fetchone()[0]
        assert pending == 0


@pytest.mark.asyncio
async def test_profile_extraction_llm_failure(plugin_with_ctx):
    """When LLM throws, rows should still be marked distilled to avoid retry loops."""
    plugin, ctx = plugin_with_ctx

    plugin._cfg.profile_extraction_enabled = True
    plugin._cfg.profile_extraction_min_messages = 1
    plugin._cfg.use_independent_consolidation_model = True
    plugin._cfg.consolidation_provider_id = "mock-provider"

    plugin._insert_conversation_sync(
        canonical_id="test-user",
        role="user",
        content="test message",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )
    plugin._insert_conversation_sync(
        canonical_id="test-user",
        role="user",
        content="another message",
        source_adapter="qq",
        source_user_id="1",
        unified_msg_origin="group:1",
    )

    async def fake_llm_fail(**kwargs):
        raise RuntimeError("provider unavailable")

    ctx.llm_generate = fake_llm_fail

    items = await plugin._run_profile_extraction_cycle(force=True, trigger="test")

    assert items == 0

    with plugin._db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM conversation_cache WHERE canonical_user_id='test-user' AND distilled=0"
        ).fetchone()[0]
        assert pending == 0
