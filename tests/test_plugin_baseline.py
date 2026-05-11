import asyncio
import sqlite3
import sys
import time

import pytest


def test_init_db_creates_core_tables_and_indexes(plugin):
    with plugin._db() as conn:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        names = {row["name"] for row in table_rows}

        assert "identity_bindings" in names
        assert "memories" in names
        assert "conversation_cache" in names
        assert "memory_events" in names
        assert "distill_history" in names

        index_rows = conn.execute("PRAGMA index_list(memories)").fetchall()
        index_names = {row["name"] for row in index_rows}
        assert "idx_memories_user" in index_names


def test_migrate_schema_adds_missing_columns(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(context=None, config={})
    with sqlite3.connect(plugin.db_path) as conn:
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, canonical_user_id TEXT, source_adapter TEXT, source_user_id TEXT, memory TEXT, memory_hash TEXT, score REAL, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE conversation_cache (id INTEGER PRIMARY KEY, canonical_user_id TEXT, role TEXT, content TEXT, created_at TEXT)"
        )

    plugin._migrate_schema()

    with plugin._db() as conn:
        memory_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        cache_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(conversation_cache)").fetchall()
        }

    assert {"memory_type", "importance", "confidence", "is_active", "tokenized_memory"}.issubset(memory_columns)
    assert {"source_adapter", "source_user_id", "unified_msg_origin", "distilled", "scope", "persona_id", "episode_id", "session_key", "turn_index", "topic_hint", "captured_at"}.issubset(cache_columns)
    plugin._close_db()


@pytest.mark.asyncio
async def test_insert_conversation_truncates_content_and_tracks_pending_rows(plugin):
    await plugin._insert_conversation(
        canonical_id="user-1",
        role="user",
        content="x" * 1200,
        source_adapter="qq",
        source_user_id="42",
        unified_msg_origin="group:1",
        scope="session",
        persona_id="bot-a",
    )

    rows = plugin._fetch_pending_rows("user-1", 10)
    assert len(rows) == 1
    assert rows[0]["content"] == "x" * 1000
    assert rows[0]["scope"] == "session"
    assert rows[0]["persona_id"] == "bot-a"
    assert plugin._count_pending_rows() == 1
    assert plugin._count_pending_users() == 1


def test_insert_memory_duplicate_reinforces_existing_row(plugin):
    first_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="42",
        memory="喜欢黑咖啡",
        score=0.4,
        memory_type="preference",
        importance=0.4,
        confidence=0.5,
    )
    second_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="42",
        memory="  喜欢黑咖啡\n",
        score=0.8,
        memory_type="preference",
        importance=0.9,
        confidence=0.95,
    )

    with plugin._db() as conn:
        row = conn.execute(
            "SELECT score, importance, confidence, reinforce_count FROM memories WHERE id=?",
            (first_id,),
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]

    assert second_id == first_id
    assert count == 1
    assert row["score"] == 0.8
    assert row["importance"] == 0.9
    assert row["confidence"] == 0.95
    assert row["reinforce_count"] == 2


def test_insert_memory_conflict_deactivates_old_memory_and_logs_event(plugin):
    old_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="42",
        memory="喜欢 红色 蓝色 绿色 黄色",
        score=0.6,
        memory_type="preference",
        importance=0.5,
        confidence=0.5,
    )
    new_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="42",
        memory="现在更喜欢 红色 蓝色 绿色 黄色",
        score=0.8,
        memory_type="preference",
        importance=0.7,
        confidence=0.9,
    )

    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT id, is_active FROM memories WHERE canonical_user_id='user-1' ORDER BY id ASC"
        ).fetchall()
        event = conn.execute(
            "SELECT event_type, payload_json FROM memory_events WHERE canonical_user_id='user-1' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert old_id != new_id
    assert [(row["id"], row["is_active"]) for row in rows] == [(old_id, 0), (new_id, 1)]
    assert event["event_type"] == "create_with_conflict"
    assert '"deactivated_count": 1' in event["payload_json"]


def test_bind_identity_upserts_mapping_and_logs_event(plugin):
    plugin._identity_mgr.bind_identity("qq", "42", "user-a")
    plugin._identity_mgr.bind_identity("qq", "42", "user-b")

    with plugin._db() as conn:
        binding = conn.execute(
            "SELECT canonical_user_id FROM identity_bindings WHERE adapter='qq' AND adapter_user_id='42'"
        ).fetchone()
        event = conn.execute(
            "SELECT canonical_user_id, event_type FROM memory_events ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert binding["canonical_user_id"] == "user-b"
    assert event["canonical_user_id"] == "user-b"
    assert event["event_type"] == "bind"


@pytest.mark.asyncio
async def test_merge_identity_moves_cache_rebinds_accounts_and_merges_duplicate_memories(plugin):
    plugin._identity_mgr.bind_identity("qq", "42", "from-user")
    await plugin._insert_conversation("from-user", "user", "hello", "qq", "42", "group:1")
    plugin._insert_memory("from-user", "qq", "42", "喜欢寿司", 0.5, "preference", 0.6, 0.7)
    plugin._insert_memory("to-user", "wx", "88", "喜欢寿司", 0.2, "preference", 0.3, 0.4)

    moved = plugin._identity_mgr.merge_identity("from-user", "to-user")

    with plugin._db() as conn:
        caches = conn.execute(
            "SELECT canonical_user_id FROM conversation_cache"
        ).fetchall()
        binding = conn.execute(
            "SELECT canonical_user_id FROM identity_bindings WHERE adapter='qq' AND adapter_user_id='42'"
        ).fetchone()
        memories = conn.execute(
            "SELECT canonical_user_id, reinforce_count, importance, confidence FROM memories ORDER BY id ASC"
        ).fetchall()
        event = conn.execute(
            "SELECT event_type, payload_json FROM memory_events WHERE canonical_user_id='to-user' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert moved == 0
    assert [row["canonical_user_id"] for row in caches] == ["to-user"]
    assert binding["canonical_user_id"] == "to-user"
    assert len(memories) == 1
    assert memories[0]["canonical_user_id"] == "to-user"
    assert memories[0]["reinforce_count"] == 2
    assert memories[0]["importance"] == 0.6
    assert memories[0]["confidence"] == 0.7
    assert event["event_type"] == "profile_identity_merged"
    assert '"legacy_memories": 0' in event["payload_json"]


@pytest.mark.asyncio
async def test_terminate_cancels_background_task_and_closes_resources(plugin):
        state = {"vector_closed": False, "web_stopped": False}

        class DummyVectorManager:
            async def close(self):
                state["vector_closed"] = True

        class DummyWebServer:
            async def stop(self):
                state["web_stopped"] = True

        async def sleeper():
            await asyncio.sleep(10)

        plugin._worker_running = True
        plugin._vector_manager = DummyVectorManager()
        plugin._web_server = DummyWebServer()
        plugin._distill_task = asyncio.create_task(sleeper())
        plugin._db()

        await plugin.terminate()

        assert plugin._worker_running is False
        assert plugin._distill_task.cancelled()
        assert state == {"vector_closed": True, "web_stopped": True}
        assert plugin._db_mgr._conn is None


@pytest.mark.asyncio
async def test_initialize_passes_normalized_vector_config_to_manager(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    captured = {}

    class DummyVectorManager:
        def __init__(self, db_path, config):
            captured["db_path"] = db_path
            captured["config"] = dict(config)

        async def initialize(self):
            captured["initialized"] = True

        async def close(self):
            captured["closed"] = True

    import sys
    import types

    fake_module = types.ModuleType("astrbot_plugin_tmemory.vector_manager")
    fake_module.VectorManager = DummyVectorManager
    sys.modules["astrbot_plugin_tmemory.vector_manager"] = fake_module

    plugin = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "enable_vector_search": True,
            "embedding_provider": "openai",
            "embedding_api_key": "test-key",
            "embedding_model": "text-embedding-3-small",
            "vector_dim": 768,
        },
    )

    await plugin.initialize()
    await plugin.terminate()

    assert captured["initialized"] is True
    assert captured["closed"] is True
    assert captured["config"]["enable_vector_search"] is True
    assert captured["config"]["embedding_provider"] == "openai"
    assert captured["config"]["embedding_api_key"] == "test-key"
    assert captured["config"]["embedding_model"] == "text-embedding-3-small"
    assert captured["config"]["vector_dim"] == 768


def test_parse_config_supports_nested_and_legacy_distill_settings(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    nested = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "distill_model_settings": {
                "use_independent_distill_model": True,
                "distill_provider_id": "provider-a",
                "distill_model_id": "model-a",
                "purify_provider_id": "provider-b",
                "purify_model_id": "model-b",
            }
        },
    )

    assert nested._cfg.use_independent_distill_model is True
    assert nested._cfg.distill_provider_id == "provider-a"
    assert nested._cfg.distill_model_id == "model-a"
    assert nested._cfg.purify_provider_id == "provider-b"
    assert nested._cfg.purify_model_id == "model-b"

    legacy = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "distill_provider_id": "legacy-provider",
            "distill_model_id": "legacy-model",
            "purify_provider_id": "legacy-purify-provider",
            "purify_model_id": "legacy-purify-model",
        },
    )

    assert legacy._cfg.distill_provider_id == "legacy-provider"
    assert legacy._cfg.distill_model_id == "legacy-model"
    assert legacy._cfg.purify_provider_id == "legacy-purify-provider"
    assert legacy._cfg.purify_model_id == "legacy-purify-model"


def test_safe_load_web_server_merges_nested_webui_settings(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    captured = {}

    class DummyWebServer:
        def __init__(self, plugin, config):
            captured["plugin"] = plugin
            captured["config"] = dict(config)

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(
        plugin_module.TMemoryPlugin,
        "_load_web_server_class",
        lambda self: DummyWebServer,
    )

    plugin = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "webui_enabled": False,
            "webui_password": "legacy-secret",
            "webui_settings": {
                "webui_enabled": True,
                "webui_password": "nested-secret",
                "webui_port": 9001,
            },
        },
    )

    assert captured["plugin"] is plugin
    assert captured["config"]["webui_enabled"] is True
    assert captured["config"]["webui_password"] == "nested-secret"
    assert captured["config"]["webui_port"] == 9001


def test_load_web_server_class_preserves_package_context_for_admin_import(
    plugin_module, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    plugin = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "webui_enabled": True,
            "webui_password": "secret",
        },
    )

    web_server_cls = plugin._load_web_server_class()
    web_server = web_server_cls(plugin, {"webui_password": "secret"})

    admin = web_server._get_admin()

    assert web_server_cls.__module__ == "astrbot_plugin_tmemory.web_server"
    assert admin.__class__.__name__ == "AdminService"


def test_load_web_server_class_recovers_package_without_sys_path_parent(
    plugin_module, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    plugin = plugin_module.TMemoryPlugin(
        context=None,
        config={
            "webui_enabled": True,
            "webui_password": "secret",
        },
    )

    sys.modules.pop("astrbot_plugin_tmemory.web_server", None)
    sys.modules.pop("astrbot_plugin_tmemory", None)

    web_server_cls = plugin._load_web_server_class()
    web_server = web_server_cls(plugin, {"webui_password": "secret"})

    admin = web_server._get_admin()

    assert admin.__class__.__name__ == "AdminService"


# =============================================================================
# 新增：触发门控与批处理效率优化测试 (TMEAAA-35)
# =============================================================================


def test_low_info_content_is_skipped_by_should_skip_capture(plugin):
    """低信息量消息（纯感叹词/短字符串）不应被采集。"""
    plugin._cfg.capture_min_content_len = 5
    # 纯感叹词
    assert plugin._capture_filter.should_skip_capture("哈哈哈") is True
    assert plugin._capture_filter.should_skip_capture("ok") is True
    # 空格/标点占位的短文本
    assert plugin._capture_filter.should_skip_capture("好") is True
    # 有效实义文本不应被过滤
    assert plugin._capture_filter.should_skip_capture("我喜欢吃火锅") is False
    assert plugin._capture_filter.should_skip_capture("用户的职业是程序员") is False


def test_low_info_content_disabled_when_min_len_zero(plugin):
    """capture_min_content_len=0 时，低信息量门控不生效。"""
    plugin._cfg.capture_min_content_len = 0
    # 纯感叹词此时不被低信息量过滤（但可被其他层过滤）
    assert plugin._capture_filter.is_low_info_content("哈哈哈") is False
    assert plugin._capture_filter.is_low_info_content("ok") is False


@pytest.mark.asyncio
async def test_capture_dedup_window_prevents_duplicate_insertion(plugin):
    """相同内容在 capture_dedup_window 内重复写入时只保留一条。"""
    plugin._cfg.capture_dedup_window = 5
    for _ in range(3):
        await plugin._insert_conversation(
            canonical_id="user-dup",
            role="user",
            content="今天天气真好",
            source_adapter="qq",
            source_user_id="11",
            unified_msg_origin="",
        )
    rows = plugin._fetch_pending_rows("user-dup", 10)
    # 只有第一条写入，后续两条重复被去重
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_capture_dedup_window_allows_different_content(plugin):
    """不同内容不受去重影响，正常写入。"""
    plugin._cfg.capture_dedup_window = 5
    contents = ["我喜欢吃火锅", "今天心情不错", "明天有个会议"]
    for c in contents:
        await plugin._insert_conversation(
            canonical_id="user-varied",
            role="user",
            content=c,
            source_adapter="qq",
            source_user_id="22",
            unified_msg_origin="",
        )
    rows = plugin._fetch_pending_rows("user-varied", 10)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_capture_dedup_disabled_when_window_zero(plugin):
    """capture_dedup_window=0 时，重复内容正常写入。"""
    plugin._cfg.capture_dedup_window = 0
    for _ in range(2):
        await plugin._insert_conversation(
            canonical_id="user-nodedup",
            role="user",
            content="重复消息",
            source_adapter="qq",
            source_user_id="33",
            unified_msg_origin="",
        )
    rows = plugin._fetch_pending_rows("user-nodedup", 10)
    assert len(rows) == 2


def test_prefilter_distill_rows_removes_low_info_and_summary(plugin):
    """_prefilter_distill_rows 应过滤掉低信息量行和 summary 行。"""
    plugin._cfg.capture_min_content_len = 5
    rows = [
        {"role": "user", "content": "哈哈哈"},       # 低信息量，应被过滤
        {"role": "summary", "content": "今日摘要..."},  # summary，应被过滤
        {"role": "user", "content": "我是一名程序员"},   # 有效，保留
        {"role": "assistant", "content": "好的，我了解了"},  # 有效，保留
        {"role": "user", "content": "ok"},            # 低信息量，应被过滤
    ]
    result = plugin._prefilter_distill_rows(rows)
    assert len(result) == 2
    assert all(r["role"] != "summary" for r in result)
    assert all(r["content"] in {"我是一名程序员", "好的，我了解了"} for r in result)


def test_prefilter_distill_rows_empty_input(plugin):
    """空输入返回空列表。"""
    assert plugin._prefilter_distill_rows([]) == []


def test_prefilter_distill_rows_all_filtered_returns_empty(plugin):
    """所有行均为低信息量时，返回空列表。"""
    plugin._cfg.capture_min_content_len = 5
    rows = [
        {"role": "user", "content": "嗯"},
        {"role": "user", "content": "好"},
        {"role": "summary", "content": "..."},
    ]
    assert plugin._prefilter_distill_rows(rows) == []


@pytest.mark.asyncio
async def test_distill_skipped_rows_counter_increments(plugin):
    """_distill_skipped_rows 计数器在 _prefilter_distill_rows 过滤行时更新。"""
    plugin._cfg.capture_min_content_len = 5
    initial = plugin._distill_skipped_rows
    # 插入 3 条 pending rows (其中一条低信息量)
    await plugin._insert_conversation("user-gate", "user", "我喜欢跑步", "qq", "99", "")
    await plugin._insert_conversation("user-gate", "user", "哈哈哈", "qq", "99", "")
    await plugin._insert_conversation("user-gate", "user", "用户爱好是摄影", "qq", "99", "")
    rows = plugin._fetch_pending_rows("user-gate", 10)
    filtered = plugin._prefilter_distill_rows(rows)
    # 手动累加（与 _run_distill_cycle 内逻辑一致）
    plugin._distill_skipped_rows += len(rows) - len(filtered)
    assert plugin._distill_skipped_rows == initial + 1  # 只有"哈哈哈"被过滤


def test_user_last_distilled_ts_throttle_dict_initialized(plugin):
    """_user_last_distilled_ts 在插件初始化后为空字典。"""
    assert isinstance(plugin._user_last_distilled_ts, dict)
    assert len(plugin._user_last_distilled_ts) == 0


# =============================================================================
# 新增：覆盖 6 个关键场景 (TMEAAA-53)
# =============================================================================


def test_migrate_distill_history_old_to_new_schema(plugin_module, tmp_path, monkeypatch):
    """旧 distill_history schema（run_at/status/messages_processed 等）应迁移为新 schema。"""
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(context=None, config={})
    with sqlite3.connect(plugin.db_path) as conn:
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, canonical_user_id TEXT, "
            "source_adapter TEXT, source_user_id TEXT, memory TEXT, memory_hash TEXT, "
            "score REAL, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE conversation_cache (id INTEGER PRIMARY KEY, "
            "canonical_user_id TEXT, role TEXT, content TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE distill_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "canonical_user_id TEXT NOT NULL, "
            "persona_id TEXT NOT NULL DEFAULT '', "
            "scope TEXT NOT NULL DEFAULT 'user', "
            "status TEXT NOT NULL, "
            "messages_processed INTEGER NOT NULL DEFAULT 0, "
            "memories_generated INTEGER NOT NULL DEFAULT 0, "
            "error_msg TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO distill_history(canonical_user_id, status, messages_processed, "
            "memories_generated) VALUES('user-old', 'ok', 50, 3)"
        )

    plugin._migrate_schema()

    with plugin._db() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        new_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(distill_history)").fetchall()
        }

    assert "distill_history_old" in tables, "old table should be preserved"
    assert "distill_history" in tables, "new table should exist"
    assert "started_at" in new_columns, "new column missing"
    assert "finished_at" in new_columns, "new column missing"
    assert "trigger_type" in new_columns, "new column missing"
    assert "users_processed" in new_columns, "new column missing"
    assert "memories_created" in new_columns, "new column missing"
    assert "users_failed" in new_columns, "new column missing"
    assert "errors" in new_columns, "new column missing"
    assert "duration_sec" in new_columns, "new column missing"
    # old column names should NOT be in the new table
    assert "status" not in new_columns
    assert "run_at" not in new_columns
    assert "messages_processed" not in new_columns
    assert "error_msg" not in new_columns

    plugin._close_db()


# ── 场景 1: Schema 初始化 ──────────────────────────────────────────────────


def test_init_db_creates_fts5_sync_triggers(plugin):
    """Schema 初始化后，如果支持 FTS5 则触发器应存在。"""
    with plugin._db() as conn:
        trigger_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        trigger_names = {row["name"] for row in trigger_rows}

    # 仅在 FTS5 初始化成功时才要求存在触发器
    has_fts = False
    with plugin._db() as conn:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
        names = {row["name"] for row in table_rows}
        has_fts = "memories_fts" in names
        
    if has_fts:
        assert "t_memories_ai" in trigger_names
        assert "t_memories_ad" in trigger_names
        assert "t_memories_au" in trigger_names


# ── 场景 2: 消息采集 ──────────────────────────────────────────────────────


def test_should_skip_capture_no_memory_protocol_marker(plugin):
    """含跨插件协议标记 \\x00[astrbot:no-memory]\\x00 的消息应被跳过采集。"""
    marked = "\x00[astrbot:no-memory]\x00" + "我有一个重要偏好"
    assert plugin._capture_filter.should_skip_capture(marked) is True
    # 不含标记的正常文本不应被跳过
    assert plugin._capture_filter.should_skip_capture("我有一个重要偏好") is False


def test_should_skip_capture_custom_prefix(plugin):
    """配置了 capture_skip_prefixes 后，匹配前缀的消息应被跳过采集。"""
    plugin._cfg.capture_skip_prefixes = ["提醒 #", "/debug"]
    assert plugin._capture_filter.should_skip_capture("提醒 # 明天开会") is True
    assert plugin._capture_filter.should_skip_capture("/debug 内部调试") is True
    # 不匹配前缀的正常消息不跳过
    assert plugin._capture_filter.should_skip_capture("我喜欢喝咖啡") is False


def test_should_skip_capture_regex_filter(plugin):
    """配置了 capture_skip_regex 后，匹配正则的消息应被跳过采集。"""
    import re
    plugin._cfg.capture_skip_regex = re.compile(r"^\[系统\]")
    assert plugin._capture_filter.should_skip_capture("[系统] 自动回复消息") is True
    assert plugin._capture_filter.should_skip_capture("用户喜欢看电影") is False


# ── 场景 3: 记忆插入 / 冲突失活 ──────────────────────────────────────────


def test_deactivated_memory_excluded_from_list(plugin):
    """失活（is_active=0）的记忆不应出现在 _list_memories 的结果中。"""
    mem_id = plugin._insert_memory(
        canonical_id="user-inactive",
        adapter="qq",
        adapter_user="99",
        memory="喜欢喝茶",
        score=0.7,
        memory_type="preference",
        importance=0.6,
        confidence=0.7,
    )
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET is_active=0 WHERE id=?", (mem_id,))

    memories = plugin._list_memories("user-inactive", limit=10)
    assert all(m["id"] != mem_id for m in memories)


def test_delete_memory_logs_delete_event(plugin):
    """删除记忆后，memory_events 中应存在对应的 delete 事件。"""
    mem_id = plugin._insert_memory(
        canonical_id="user-del",
        adapter="qq",
        adapter_user="55",
        memory="喜欢骑行",
        score=0.6,
        memory_type="preference",
        importance=0.5,
        confidence=0.6,
    )

    deleted = plugin._delete_memory(mem_id)

    with plugin._db() as conn:
        event = conn.execute(
            "SELECT event_type FROM memory_events WHERE canonical_user_id='user-del'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert deleted is True
    assert event is not None
    assert event["event_type"] == "delete"


# ── 场景 4: 身份绑定 / 合并 ──────────────────────────────────────────────


def test_merge_identity_moves_unique_memories_to_target(plugin):
    """合并时，from_user 独有（无哈希冲突）的记忆应迁移到 to_user，moved_count > 0。"""
    plugin._insert_memory(
        canonical_id="from-unique",
        adapter="qq",
        adapter_user="10",
        memory="喜欢爬山",
        score=0.6,
        memory_type="preference",
        importance=0.5,
        confidence=0.6,
    )

    moved = plugin._identity_mgr.merge_identity("from-unique", "to-unique")

    with plugin._db() as conn:
        from_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE canonical_user_id='from-unique'"
        ).fetchone()["n"]
        to_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE canonical_user_id='to-unique'"
        ).fetchone()["n"]

    assert moved == 1
    assert from_rows == 0
    assert to_rows == 1


# ── 场景 5: WebUI 登录鉴权 ── 见 test_web_server.py

# ── 场景 6: Terminate 资源清理 ────────────────────────────────────────────




def test_close_db_sets_conn_to_none(plugin):
    """_close_db() 调用后，plugin._db_mgr._conn 应为 None。"""
    with plugin._db_mgr.db():
        pass
    assert plugin._db_mgr._conn is not None
    plugin._close_db()
    assert plugin._db_mgr._conn is None


# =============================================================================
# MIG-001: Fresh DB init creates all 0.8.0 conversation_cache columns
# =============================================================================


def test_mig001_fresh_db_conversation_cache_columns(plugin_module, tmp_path, monkeypatch):
    """Fresh init_db must create conversation_cache with all 0.8.0 columns
    including session_key, turn_index, topic_hint, captured_at, episode_id."""
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(context=None, config={})
    plugin._init_db()
    plugin._migrate_schema()

    required = {
        "id", "canonical_user_id", "role", "content",
        "source_adapter", "source_user_id", "unified_msg_origin",
        "distilled", "distilled_at", "created_at",
        "scope", "persona_id", "episode_id",
        "session_key", "turn_index", "topic_hint", "captured_at",
    }

    with plugin._db() as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(conversation_cache)").fetchall()
        }

    missing = required - columns
    assert not missing, f"MIG-001 FAIL: missing columns in fresh DB: {missing}"
    plugin._close_db()


# =============================================================================
# MIG-002: Migration backfill for captured_at and session_key
# =============================================================================


def test_mig002_backfill_captured_at_and_session_key(plugin_module, tmp_path, monkeypatch):
    """Existing rows without captured_at/session_key must be backfilled:
    captured_at ← created_at, session_key ← unified_msg_origin."""
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(context=None, config={})
    with sqlite3.connect(plugin.db_path) as conn:
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, canonical_user_id TEXT, "
            "source_adapter TEXT, source_user_id TEXT, memory TEXT, memory_hash TEXT, "
            "score REAL, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE conversation_cache ("
            "id INTEGER PRIMARY KEY, canonical_user_id TEXT, role TEXT, content TEXT, "
            "created_at TEXT, unified_msg_origin TEXT DEFAULT '')"
        )
        # Row with both populated
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, created_at, unified_msg_origin) "
            "VALUES('user-a', 'user', 'hello', '2025-01-01T00:00:00', 'group:123')"
        )
        # Row with empty unified_msg_origin
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, created_at, unified_msg_origin) "
            "VALUES('user-b', 'assistant', 'hi', '2025-01-02T00:00:00', '')"
        )

    plugin._migrate_schema()

    with plugin._db() as conn:
        row_a = conn.execute(
            "SELECT captured_at, session_key FROM conversation_cache WHERE canonical_user_id='user-a'"
        ).fetchone()
        row_b = conn.execute(
            "SELECT captured_at, session_key FROM conversation_cache WHERE canonical_user_id='user-b'"
        ).fetchone()

    assert row_a["captured_at"] == "2025-01-01T00:00:00", f"MIG-002 FAIL: captured_at={row_a['captured_at']}"
    assert row_a["session_key"] == "group:123", f"MIG-002 FAIL: session_key={row_a['session_key']}"
    assert row_b["captured_at"] == "2025-01-02T00:00:00", f"MIG-002 FAIL: captured_at={row_b['captured_at']}"
    assert row_b["session_key"] == "", f"MIG-002 FAIL: session_key should stay empty: {row_b['session_key']}"

    plugin._close_db()


# =============================================================================
# MIG-003: distill_history old-to-new schema migration preserves data
# =============================================================================


def test_mig003_distill_history_migration_preserves_old_data(plugin_module, tmp_path, monkeypatch):
    """Old distill_history must be renamed to distill_history_old with data intact,
    and new distill_history must have correct 0.8.0 schema (empty)."""
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(context=None, config={})
    with sqlite3.connect(plugin.db_path) as conn:
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, canonical_user_id TEXT, "
            "source_adapter TEXT, source_user_id TEXT, memory TEXT, memory_hash TEXT, "
            "score REAL, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE conversation_cache ("
            "id INTEGER PRIMARY KEY, canonical_user_id TEXT, role TEXT, content TEXT, "
            "created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE distill_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "canonical_user_id TEXT NOT NULL, "
            "persona_id TEXT NOT NULL DEFAULT '', "
            "scope TEXT NOT NULL DEFAULT 'user', "
            "status TEXT NOT NULL, "
            "messages_processed INTEGER NOT NULL DEFAULT 0, "
            "memories_generated INTEGER NOT NULL DEFAULT 0, "
            "error_msg TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO distill_history(canonical_user_id, status, messages_processed, "
            "memories_generated) VALUES('user-old', 'ok', 50, 3)"
        )
        conn.execute(
            "INSERT INTO distill_history(canonical_user_id, status, messages_processed, "
            "memories_generated) VALUES('user-old-2', 'error', 20, 0)"
        )

    plugin._migrate_schema()

    with plugin._db() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        # Verify both tables exist
        assert "distill_history_old" in tables, "MIG-003 FAIL: distill_history_old not created"
        assert "distill_history" in tables, "MIG-003 FAIL: new distill_history not created"

        # Old data preserved
        old_count = conn.execute("SELECT COUNT(*) AS n FROM distill_history_old").fetchone()["n"]
        assert old_count == 2, f"MIG-003 FAIL: expected 2 old rows, got {old_count}"

        old_row = conn.execute(
            "SELECT canonical_user_id, status, messages_processed "
            "FROM distill_history_old WHERE canonical_user_id='user-old'"
        ).fetchone()
        assert old_row is not None, "MIG-003 FAIL: old data row missing"
        assert old_row["status"] == "ok"
        assert old_row["messages_processed"] == 50

        # New table has empty data but correct schema
        new_count = conn.execute("SELECT COUNT(*) AS n FROM distill_history").fetchone()["n"]
        assert new_count == 0, f"MIG-003 FAIL: new distill_history should be empty, got {new_count}"

        new_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(distill_history)").fetchall()
        }
        required_new = {"started_at", "finished_at", "trigger_type",
                        "users_processed", "memories_created", "users_failed",
                        "errors", "duration_sec"}
        missing_new = required_new - new_columns
        assert not missing_new, f"MIG-003 FAIL: new schema missing columns: {missing_new}"

        # Old column names must NOT be in new table
        old_markers = {"status", "run_at", "messages_processed", "error_msg"}
        leaked = old_markers & new_columns
        assert not leaked, f"MIG-003 FAIL: old columns leaked into new schema: {leaked}"

    plugin._close_db()


# =============================================================================
# CFG-001: parse_config maps all layered injection fields
# =============================================================================


def test_cfg001_parse_config_layered_injection_fields(plugin_module):
    """CFG-001: parse_config must map enable_layered_injection, inject_working_turns,
    inject_episode_limit, inject_episode_max_chars, inject_style_max_chars from raw dict."""
    from astrbot_plugin_tmemory.core.config import parse_config

    raw = {
        "enable_layered_injection": True,
        "inject_working_turns": 10,
        "inject_episode_limit": 5,
        "inject_episode_max_chars": 800,
        "inject_style_max_chars": 500,
    }
    cfg = parse_config(raw)

    assert cfg.enable_layered_injection is True, "CFG-001 FAIL: enable_layered_injection not True"
    assert cfg.inject_working_turns == 10, "CFG-001 FAIL: inject_working_turns not parsed"
    assert cfg.inject_episode_limit == 5, "CFG-001 FAIL: inject_episode_limit not parsed"
    assert cfg.inject_episode_max_chars == 800, "CFG-001 FAIL: inject_episode_max_chars not parsed"
    assert cfg.inject_style_max_chars == 500, "CFG-001 FAIL: inject_style_max_chars not parsed"


def test_cfg001_parse_config_layered_injection_defaults(plugin_module):
    """CFG-001: When raw dict is empty, all layered injection fields get safe defaults."""
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({})

    assert cfg.enable_layered_injection is False
    assert cfg.inject_working_turns == 5
    assert cfg.inject_episode_limit == 3
    assert cfg.inject_episode_max_chars == 600
    assert cfg.inject_style_max_chars == 400


def test_cfg001_parse_config_layered_injection_zero_clamped(plugin_module):
    """CFG-001: Negative values for int fields clamp to 0."""
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({
        "inject_working_turns": -5,
        "inject_episode_limit": -1,
        "inject_episode_max_chars": -100,
        "inject_style_max_chars": -50,
    })

    assert cfg.inject_working_turns == 0
    assert cfg.inject_episode_limit == 0
    assert cfg.inject_episode_max_chars == 0
    assert cfg.inject_style_max_chars == 0


# =============================================================================
# CFG-003: enable_layered_injection gate controls injection path
# =============================================================================


class _DummyEvent:
    """Minimal stub for AstrMessageEvent in injection tests."""
    def __init__(self, group_id: str = ""):
        self._group_id = group_id
    def get_group_id(self):
        return self._group_id
    def get_message_type(self):
        from enum import Enum
        class _MT(str, Enum):
            FRIEND_MESSAGE = "friend"
            GROUP_MESSAGE = "group"
        return _MT.FRIEND_MESSAGE if not self._group_id else _MT.GROUP_MESSAGE


class _DummyProviderRequest:
    """Minimal stub for ProviderRequest in injection tests."""
    def __init__(self, prompt: str = "", system_prompt: str = ""):
        self.prompt = prompt
        self.system_prompt = system_prompt


@pytest.mark.asyncio
async def test_cfg003_injection_uses_profile_path(plugin_module, tmp_path, monkeypatch):
    """CFG-003: Injection always uses build_profile_injection regardless of enable_layered_injection."""
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={
        "enable_layered_injection": True,
        "enable_memory_injection": True,
    })
    instance._init_db()
    instance._migrate_schema()
    instance._cfg.inject_working_turns = 3

    called = []

    async def fake_build_profile(canonical_id, query, session_key, **kwargs):
        called.append((canonical_id, query, session_key))
        return "[PROFILE_BLOCK]"

    instance._injection_builder.build_profile_injection = fake_build_profile
    instance._identity_mgr.resolve_current_identity = lambda e: ("user-cfg3", "qq", "42")

    event = _DummyEvent()
    req = _DummyProviderRequest(prompt="测试查询", system_prompt="你是AI助手。")
    await instance._handle_on_llm_request(event, req)

    assert len(called) == 1, "CFG-003 FAIL: profile injection path not called"
    assert called[0][1] == "测试查询"
    assert "[PROFILE_BLOCK]" in req.system_prompt
    instance._close_db()


@pytest.mark.asyncio
async def test_cfg003_injection_unified_regardless_of_layered_flag(plugin_module, tmp_path, monkeypatch):
    """CFG-003: enable_layered_injection=False still uses unified profile injection path."""
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={
        "enable_layered_injection": False,
        "enable_memory_injection": True,
    })
    instance._init_db()
    instance._migrate_schema()

    called = []

    async def fake_build_profile(canonical_id, query, session_key, **kwargs):
        called.append((canonical_id, query, session_key))
        return "[PROFILE_BLOCK]"

    instance._injection_builder.build_profile_injection = fake_build_profile
    instance._identity_mgr.resolve_current_identity = lambda e: ("user-cfg3b", "qq", "42")

    event = _DummyEvent()
    req = _DummyProviderRequest(prompt="测试查询", system_prompt="你是AI助手。")
    await instance._handle_on_llm_request(event, req)

    assert len(called) == 1, "CFG-003 FAIL: unified profile path not called"
    assert "[PROFILE_BLOCK]" in req.system_prompt
    instance._close_db()


@pytest.mark.asyncio
async def test_cfg003_injection_skipped_when_disabled(plugin_module, tmp_path, monkeypatch):
    """CFG-003: When enable_memory_injection=False, injection is skipped."""
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={
        "enable_layered_injection": True,
        "enable_memory_injection": False,
    })
    instance._init_db()
    instance._migrate_schema()

    called = []

    async def fake_build_profile(**kwargs):
        called.append(1)
        return "BLOCK"

    instance._injection_builder.build_profile_injection = fake_build_profile
    instance._identity_mgr.resolve_current_identity = lambda e: ("user-off", "qq", "42")

    event = _DummyEvent()
    req = _DummyProviderRequest(prompt="测试查询", system_prompt="原始prompt")
    await instance._handle_on_llm_request(event, req)

    assert len(called) == 0, "CFG-003 FAIL: injection should be skipped when disabled"
    instance._close_db()


# =============================================================================
# HOT-001: on_llm_request performs zero consolidation LLM calls
# =============================================================================


@pytest.mark.asyncio
async def test_hot001_on_llm_request_zero_llm_calls(plugin_module, tmp_path, monkeypatch):
    """HOT-001: The entire on_llm_request code path must make zero LLM calls.

    This verifies the ADR-006 hot-path boundary: consolidation LLM work is
    confined to the background worker loop.
    """
    monkeypatch.chdir(tmp_path)

    llm_call_count = [0]

    class _MockContext:
        async def llm_generate(self, **kwargs):
            llm_call_count[0] += 1
            raise RuntimeError("LLM should not be called in hot path")

        async def get_current_chat_provider_id(self, **kwargs):
            llm_call_count[0] += 1
            return None

    ctx = _MockContext()
    instance = plugin_module.TMemoryPlugin(context=ctx, config={
        "enable_layered_injection": True,
        "enable_memory_injection": True,
    })
    instance._init_db()
    instance._migrate_schema()

    instance._identity_mgr.resolve_current_identity = lambda e: ("user-hot1", "qq", "42")
    instance._identity_mgr.bind_identity("qq", "42", "user-hot1")

    # Insert a profile item so retrieval has something to return
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with instance._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            ("user-hot1", "preference", "用户喜欢咖啡", "用户喜欢咖啡",
             0.8, 0.7, 0.5, now, now),
        )

    event = _DummyEvent()
    req = _DummyProviderRequest(prompt="我想喝咖啡", system_prompt="你是AI助手。")
    await instance._handle_on_llm_request(event, req)

    assert llm_call_count[0] == 0, (
        f"HOT-001 FAIL: on_llm_request made {llm_call_count[0]} LLM calls, "
        f"expected 0. Consolidation LLM work must be confined to background worker."
    )
    instance._close_db()


# =============================================================================
# CFG-002: conf_schema exposes consolidation pipeline + layered injection config
# =============================================================================


def test_cfg002_parse_config_consolidation_fields(plugin_module):
    """CFG-002: parse_config must map all consolidation pipeline fields from raw dict."""
    from astrbot_plugin_tmemory.core.config import parse_config

    raw = {
        "enable_consolidation_pipeline": True,
        "enable_episodic_summarization": False,
        "enable_episode_semantic_distill": False,
        "distill_max_users_per_cycle": 20,
        "stage_timeout_sec": 180,
        "episode_summary_min_messages": 10,
        "episode_summary_max_input_tokens": 5000,
        "episode_session_gap_minutes": 120,
    }
    cfg = parse_config(raw)

    assert cfg.enable_consolidation_pipeline is True
    assert cfg.enable_episodic_summarization is False
    assert cfg.enable_episode_semantic_distill is False
    assert cfg.distill_max_users_per_cycle == 20
    assert cfg.stage_timeout_sec == 180
    assert cfg.episode_summary_min_messages == 10
    assert cfg.episode_summary_max_input_tokens == 5000
    assert cfg.episode_session_gap_minutes == 120


def test_cfg002_parse_config_consolidation_defaults(plugin_module):
    """CFG-002: When raw dict is empty, all consolidation fields get safe defaults."""
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({})

    assert cfg.enable_consolidation_pipeline is False
    assert cfg.enable_episodic_summarization is True
    assert cfg.enable_episode_semantic_distill is True
    assert cfg.distill_max_users_per_cycle == 10
    assert cfg.stage_timeout_sec == 120
    assert cfg.episode_summary_min_messages == 5
    assert cfg.episode_summary_max_input_tokens == 3000
    assert cfg.episode_session_gap_minutes == 60


def test_cfg002_parse_config_consolidation_nested(plugin_module):
    """CFG-002: parse_config must parse nested consolidation_pipeline dict matching schema."""
    from astrbot_plugin_tmemory.core.config import parse_config

    nested = parse_config({
        "consolidation_pipeline": {
            "enable_consolidation_pipeline": True,
            "stage_timeout_sec": 45,
            "enable_episodic_summarization": False,
            "distill_max_users_per_cycle": 25,
        }
    })
    flat = parse_config({
        "enable_consolidation_pipeline": True,
        "stage_timeout_sec": 45,
        "enable_episodic_summarization": False,
        "distill_max_users_per_cycle": 25,
    })

    assert nested.enable_consolidation_pipeline is True
    assert nested.stage_timeout_sec == 45
    assert nested.enable_episodic_summarization is False
    assert nested.distill_max_users_per_cycle == 25

    assert flat.enable_consolidation_pipeline is True
    assert flat.stage_timeout_sec == 45
    assert flat.enable_episodic_summarization is False
    assert flat.distill_max_users_per_cycle == 25

    # Unspecified nested fields get defaults
    assert nested.enable_episode_semantic_distill is True
    assert nested.episode_summary_min_messages == 5


def test_cfg002_parse_config_consolidation_nested_merges_top_level_fallback(plugin_module):
    """CFG-002: Nested consolidation_pipeline merges top-level fallback for backwards compat."""
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({
        "consolidation_pipeline": {
            "enable_consolidation_pipeline": True,
            "stage_timeout_sec": 45,
        },
        "enable_episodic_summarization": False,  # top-level fallback
        "distill_max_users_per_cycle": 15,        # top-level fallback
    })

    # Nested values take priority
    assert cfg.enable_consolidation_pipeline is True
    assert cfg.stage_timeout_sec == 45
    # Top-level keys fill in gaps not in nested
    assert cfg.enable_episodic_summarization is False
    assert cfg.distill_max_users_per_cycle == 15


def test_cfg002_parse_config_consolidation_clamping(plugin_module):
    """CFG-002: Consolidation int fields clamp to safe minimums."""
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({
        "distill_max_users_per_cycle": 0,
        "stage_timeout_sec": 10,
        "episode_summary_min_messages": 0,
        "episode_summary_max_input_tokens": 100,
        "episode_session_gap_minutes": 0,
    })

    assert cfg.distill_max_users_per_cycle == 1   # min 1
    assert cfg.stage_timeout_sec == 30              # min 30
    assert cfg.episode_summary_min_messages == 2    # min 2
    assert cfg.episode_summary_max_input_tokens == 500  # min 500
    assert cfg.episode_session_gap_minutes == 5     # min 5


def test_cfg002_parse_config_consolidation_model_settings(plugin_module):
    """CFG-002: parse_config handles nested consolidation_model_settings."""
    from astrbot_plugin_tmemory.core.config import parse_config

    raw = {
        "consolidation_model_settings": {
            "use_independent_consolidation_model": True,
            "consolidation_provider_id": "provider-c",
            "consolidation_model_id": "model-c",
        }
    }
    cfg = parse_config(raw)

    assert cfg.use_independent_consolidation_model is True
    assert cfg.consolidation_provider_id == "provider-c"
    assert cfg.consolidation_model_id == "model-c"


def test_cfg002_parse_config_consolidation_model_defaults(plugin_module):
    """CFG-002: Consolidation model defaults: no independent model, empty provider/model."""
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({})

    assert cfg.use_independent_consolidation_model is False
    assert cfg.consolidation_provider_id == ""
    assert cfg.consolidation_model_id == ""


def test_cfg002_schema_exposes_all_consolidation_fields(plugin_module):
    """CFG-002: _conf_schema.json must contain all consolidation pipeline config keys."""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text())

    # Top-level consolidation sections
    assert "consolidation_pipeline" in schema, "CFG-002 FAIL: consolidation_pipeline section missing"
    assert "consolidation_model_settings" in schema, "CFG-002 FAIL: consolidation_model_settings section missing"

    cp = schema["consolidation_pipeline"]
    assert cp["type"] == "object"
    items = cp["items"]

    required = [
        "enable_consolidation_pipeline",
        "enable_episodic_summarization",
        "enable_episode_semantic_distill",
        "distill_max_users_per_cycle",
        "stage_timeout_sec",
        "episode_summary_min_messages",
        "episode_summary_max_input_tokens",
        "episode_session_gap_minutes",
    ]
    for key in required:
        assert key in items, f"CFG-002 FAIL: consolidation_pipeline.items missing '{key}'"

    # Verify defaults match runtime
    assert items["enable_consolidation_pipeline"]["default"] is False
    assert items["enable_episodic_summarization"]["default"] is True
    assert items["enable_episode_semantic_distill"]["default"] is True
    assert items["distill_max_users_per_cycle"]["default"] == 10
    assert items["stage_timeout_sec"]["default"] == 120
    assert items["episode_summary_min_messages"]["default"] == 5
    assert items["episode_summary_max_input_tokens"]["default"] == 3000
    assert items["episode_session_gap_minutes"]["default"] == 60

    cms = schema["consolidation_model_settings"]
    assert cms["type"] == "object"
    cms_items = cms["items"]

    required_cms = [
        "use_independent_consolidation_model",
        "consolidation_provider_id",
        "consolidation_model_id",
    ]
    for key in required_cms:
        assert key in cms_items, f"CFG-002 FAIL: consolidation_model_settings.items missing '{key}'"

    assert cms_items["use_independent_consolidation_model"]["default"] is False
    assert cms_items["consolidation_provider_id"]["default"] == ""
    assert cms_items["consolidation_provider_id"]["_special"] == "select_provider"
    assert cms_items["consolidation_model_id"]["default"] == ""


def test_cfg002_schema_exposes_all_layered_injection_fields(plugin_module):
    """CFG-002: _conf_schema.json must contain all layered injection config keys."""
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text())

    required = [
        "enable_layered_injection",
        "inject_working_turns",
        "inject_episode_limit",
        "inject_episode_max_chars",
        "inject_style_max_chars",
    ]
    for key in required:
        assert key in schema, f"CFG-002 FAIL: schema missing '{key}'"

    assert schema["enable_layered_injection"]["default"] is False
    assert schema["inject_working_turns"]["default"] == 5
    assert schema["inject_episode_limit"]["default"] == 3
    assert schema["inject_episode_max_chars"]["default"] == 600
    assert schema["inject_style_max_chars"]["default"] == 400


def test_cfg002_schema_defaults_match_parse_config_defaults(plugin_module):
    """CFG-002: Every new schema field default must match parse_config({}) output."""
    import json
    from pathlib import Path
    from astrbot_plugin_tmemory.core.config import parse_config

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text())
    cfg = parse_config({})

    # Consolidation pipeline
    cp = schema["consolidation_pipeline"]["items"]
    assert cp["enable_consolidation_pipeline"]["default"] == cfg.enable_consolidation_pipeline
    assert cp["enable_episodic_summarization"]["default"] == cfg.enable_episodic_summarization
    assert cp["enable_episode_semantic_distill"]["default"] == cfg.enable_episode_semantic_distill
    assert cp["distill_max_users_per_cycle"]["default"] == cfg.distill_max_users_per_cycle
    assert cp["stage_timeout_sec"]["default"] == cfg.stage_timeout_sec
    assert cp["episode_summary_min_messages"]["default"] == cfg.episode_summary_min_messages
    assert cp["episode_summary_max_input_tokens"]["default"] == cfg.episode_summary_max_input_tokens
    assert cp["episode_session_gap_minutes"]["default"] == cfg.episode_session_gap_minutes

    # Consolidation model settings
    cms = schema["consolidation_model_settings"]["items"]
    assert cms["use_independent_consolidation_model"]["default"] == cfg.use_independent_consolidation_model
    assert cms["consolidation_provider_id"]["default"] == cfg.consolidation_provider_id
    assert cms["consolidation_model_id"]["default"] == cfg.consolidation_model_id

    # Layered injection
    assert schema["enable_layered_injection"]["default"] == cfg.enable_layered_injection
    assert schema["inject_working_turns"]["default"] == cfg.inject_working_turns
    assert schema["inject_episode_limit"]["default"] == cfg.inject_episode_limit
    assert schema["inject_episode_max_chars"]["default"] == cfg.inject_episode_max_chars
    assert schema["inject_style_max_chars"]["default"] == cfg.inject_style_max_chars


# =============================================================================
# PROF-001: Profile retrieval with per-facet quota and dedup
# =============================================================================


def test_prof001_compute_facet_quota_distributes_slots():
    """PROF-001: _compute_facet_quota distributes total across facets by weight."""
    from astrbot_plugin_tmemory.search.retrieval import _compute_facet_quota

    weights = {
        "restriction": 2.0,
        "preference": 1.5,
        "fact": 1.0,
        "style": 0.5,
        "task_pattern": 0.5,
    }

    quota = _compute_facet_quota(5, weights)
    assert sum(quota.values()) >= 5, f"PROF-001 FAIL: total slots {sum(quota.values())} < 5"
    # Higher-weight facets should get at least as many slots as lower-weight
    assert quota.get("restriction", 0) >= quota.get("style", 0)
    assert quota.get("preference", 0) >= quota.get("task_pattern", 0)


def test_prof001_compute_facet_quota_single_facet():
    """PROF-001: With only one facet having weight, all slots go there."""
    from astrbot_plugin_tmemory.search.retrieval import _compute_facet_quota

    quota = _compute_facet_quota(3, {"fact": 1.0, "style": 0.0})
    assert quota.get("fact", 0) == 3
    assert quota.get("style", 0) == 0


def test_prof001_compute_facet_quota_empty_weights():
    """PROF-001: Empty or zero-weight dict returns empty quota."""
    from astrbot_plugin_tmemory.search.retrieval import _compute_facet_quota
    assert _compute_facet_quota(5, {}) == {}
    assert _compute_facet_quota(5, {"x": 0.0}) == {}


def test_prof001_profile_dedup_with_quota_respects_facet_limits():
    """PROF-001: _profile_dedup_with_quota enforces per-facet limits and dedups prefixes."""
    from astrbot_plugin_tmemory.search.retrieval import _profile_dedup_with_quota

    items = [
        {"facet_type": "preference", "content": "喜欢咖啡"},
        {"facet_type": "preference", "content": "喜欢咖啡加奶"},  # same prefix "喜欢咖啡" → dedup
        {"facet_type": "fact", "content": "职业是程序员"},
        {"facet_type": "fact", "content": "住在北京"},
        {"facet_type": "fact", "content": "喜欢跑步"},
    ]
    quota = {"preference": 1, "fact": 2, "style": 1, "restriction": 1, "task_pattern": 1}

    result = _profile_dedup_with_quota(items, 5, quota)

    # preference: only 1 slot, first "喜欢咖啡" takes it, "喜欢咖啡加奶" deduped by prefix
    prefs = [r for r in result if r["facet_type"] == "preference"]
    assert len(prefs) == 1
    # fact: 2 slots, all 3 are unique but quota=2
    facts = [r for r in result if r["facet_type"] == "fact"]
    assert len(facts) == 2


def test_prof001_profile_dedup_with_quota_skips_empty_prefix():
    """PROF-001: Items with empty content prefix are skipped."""
    from astrbot_plugin_tmemory.search.retrieval import _profile_dedup_with_quota

    items = [
        {"facet_type": "fact", "content": ""},
        {"facet_type": "fact", "content": "   "},
        {"facet_type": "fact", "content": "有效内容"},
    ]
    quota = {"fact": 5}
    result = _profile_dedup_with_quota(items, 5, quota)
    assert len(result) == 1
    assert result[0]["content"] == "有效内容"


# =============================================================================
# INJ-001: Profile injection output format
# =============================================================================


def test_inj001_assemble_profile_blocks_groups_by_facet():
    """INJ-001: _assemble_profile_blocks groups items by facet with correct headings."""
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder

    items = [
        {"facet_type": "fact", "content": "用户是程序员"},
        {"facet_type": "preference", "content": "喜欢黑咖啡"},
        {"facet_type": "restriction", "content": "不吃海鲜"},
        {"facet_type": "fact", "content": "住在上海"},
        {"facet_type": "style", "content": "回答简洁"},
    ]

    block = InjectionBuilder._assemble_profile_blocks(items)
    assert "[用户画像·限制]" in block
    assert "[用户画像·偏好]" in block
    assert "[用户画像·事实]" in block
    assert "[用户画像·风格指导]" in block
    # task_pattern should NOT appear since no items of that facet
    assert "[用户画像·任务模式]" not in block
    # Content checks
    assert "不吃海鲜" in block
    assert "喜欢黑咖啡" in block
    assert "用户是程序员" in block
    assert "回答简洁" in block


def test_inj001_assemble_profile_blocks_empty_input():
    """INJ-001: Empty item list returns empty string."""
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder
    assert InjectionBuilder._assemble_profile_blocks([]) == ""


def test_inj001_assemble_profile_blocks_skips_empty_content():
    """INJ-001: Items with empty content are skipped."""
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder

    items = [
        {"facet_type": "fact", "content": ""},
        {"facet_type": "fact", "content": "有效"},
    ]
    block = InjectionBuilder._assemble_profile_blocks(items)
    assert "有效" in block
    # Should not have two fact entries
    assert block.count("- 有效") == 1


def test_inj001_assemble_profile_blocks_facet_ordering():
    """INJ-001: Facets appear in priority order: restriction > preference > fact > style > task_pattern."""
    from astrbot_plugin_tmemory.core.injection import InjectionBuilder

    items = [
        {"facet_type": "task_pattern", "content": "每天写日报"},
        {"facet_type": "restriction", "content": "素食"},
        {"facet_type": "fact", "content": "程序员"},
    ]

    block = InjectionBuilder._assemble_profile_blocks(items)
    # restriction must appear before fact, which must appear before task_pattern
    r_pos = block.find("[用户画像·限制]")
    f_pos = block.find("[用户画像·事实]")
    t_pos = block.find("[用户画像·任务模式]")
    assert r_pos < f_pos < t_pos, (
        f"INJ-001 FAIL: facet order wrong: restriction={r_pos}, fact={f_pos}, task_pattern={t_pos}"
    )


# =============================================================================
# INJ-002: build_profile_injection integration
# =============================================================================


@pytest.mark.asyncio
async def test_inj002_build_profile_injection_with_profile_items(plugin_module, tmp_path, monkeypatch):
    """INJ-002: build_profile_injection returns profile blocks + working context when data exists."""
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={
        "enable_memory_injection": True,
    })
    instance._init_db()
    instance._migrate_schema()

    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # Insert profile items
    with instance._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            ("user-inj2", "preference", "喜欢黑咖啡", "喜欢黑咖啡", 0.9, 0.8, 0.7, now, now),
        )
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            ("user-inj2", "fact", "职业是程序员", "职业是程序员", 0.8, 0.7, 0.6, now, now),
        )
        # Insert working context (conversation)
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, "
            "session_key, created_at) VALUES(?, ?, ?, ?, ?)",
            ("user-inj2", "user", "今天喝什么？", "session-1", now),
        )
        conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id, role, content, "
            "session_key, created_at) VALUES(?, ?, ?, ?, ?)",
            ("user-inj2", "assistant", "你可以喝咖啡", "session-1", now),
        )

    instance._cfg.inject_working_turns = 3
    instance._cfg.inject_memory_limit = 5

    block = await instance._injection_builder.build_profile_injection(
        "user-inj2", "咖啡", "session-1",
    )

    assert "[当前对话]" in block
    assert "今天喝什么？" in block
    assert "你可以喝咖啡" in block
    assert "[用户画像·偏好]" in block
    assert "喜欢黑咖啡" in block
    assert "[用户画像·事实]" in block
    assert "职业是程序员" in block
    instance._close_db()


@pytest.mark.asyncio
async def test_inj002_build_profile_injection_empty_when_no_data(plugin_module, tmp_path, monkeypatch):
    """INJ-002: build_profile_injection returns empty string when no data exists."""
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={})
    instance._init_db()
    instance._migrate_schema()

    block = await instance._injection_builder.build_profile_injection(
        "no-user", "", "no-session",
    )
    assert block == ""
    instance._close_db()


@pytest.mark.asyncio
async def test_inj002_build_layered_injection_alias_works(plugin_module, tmp_path, monkeypatch):
    """INJ-002: build_layered_injection is a backward-compat alias for build_profile_injection."""
    monkeypatch.chdir(tmp_path)
    instance = plugin_module.TMemoryPlugin(context=None, config={})
    instance._init_db()
    instance._migrate_schema()

    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with instance._db() as conn:
        conn.execute(
            "INSERT INTO profile_items(canonical_user_id, facet_type, content, "
            "normalized_content, status, confidence, importance, stability, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            ("user-alias", "style", "回答简洁明了", "回答简洁明了", 0.7, 0.6, 0.5, now, now),
        )

    block = await instance._injection_builder.build_layered_injection(
        "user-alias", "", "session-x",
    )
    assert "[用户画像·风格指导]" in block
    assert "回答简洁明了" in block
    instance._close_db()
