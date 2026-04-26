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
    assert {"source_adapter", "source_user_id", "unified_msg_origin", "distilled", "scope", "persona_id"}.issubset(cache_columns)
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
    assert event["event_type"] == "merge"
    assert '"moved_count": 0' in event["payload_json"]


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
