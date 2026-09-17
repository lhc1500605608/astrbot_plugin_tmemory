"""T4 Phase 4a 会话生命周期测试（TMEAAA-358 / Plan TMEAAA-354 Phase 4a）。

覆盖：
- ``session_reset_policy`` 三态解析与默认值（keep / archive / clear）
- ``conversation_cache.archived_at`` 列存在且旧库可迁移
- 会话删除钩子注册（能力可用 / 不可用）
- reset 策略行为：keep 保留、archive 软归档且工作上下文不再召回、clear 删除但保留证据
- 会话删除在日志中可观测
- on_agent_begin / on_agent_done 观测
"""

from __future__ import annotations

import logging

import pytest


class _FakeConvMgr:
    def __init__(self):
        self.callbacks = []

    def register_on_session_deleted(self, callback):
        self.callbacks.append(callback)


class _FakeContext:
    def __init__(self):
        self.conversation_manager = _FakeConvMgr()


class _NoHookContext:
    """有 conversation_manager 但缺少 register_on_session_deleted。"""

    class _Mgr:
        pass

    def __init__(self):
        self.conversation_manager = self._Mgr()


class _DummyEvent:
    def __init__(self, umo: str = "qq:FriendMessage:1"):
        self.unified_msg_origin = umo


class _RotatingConvMgr:
    def __init__(self, cid: str = "cid-1"):
        self.cid = cid

    async def get_curr_conversation_id(self, umo: str):
        return self.cid


class _RotatingContext:
    def __init__(self, cid: str = "cid-1"):
        self.conversation_manager = _RotatingConvMgr(cid)


async def _seed(plugin, user: str, umo: str, contents) -> None:
    for role, text in contents:
        await plugin._insert_conversation(
            canonical_id=user,
            role=role,
            content=text,
            source_adapter="qq",
            source_user_id="42",
            unified_msg_origin=umo,
        )


def _rows(plugin, umo: str):
    with plugin._db() as conn:
        return conn.execute(
            "SELECT id, content, archived_at FROM conversation_cache "
            "WHERE session_key=? ORDER BY id",
            (umo,),
        ).fetchall()


# ── 配置解析 ──────────────────────────────────────────────────────────────────


def test_default_policy_is_keep(plugin):
    assert plugin._cfg.session_reset_policy == "keep"
    assert plugin._normalized_session_reset_policy() == "keep"


@pytest.mark.parametrize("value", ["keep", "archive", "clear", "ARCHIVE"])
def test_parse_config_accepts_valid_policies(plugin_module, value):
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({"session_reset_policy": value})
    assert cfg.session_reset_policy == value.lower()


@pytest.mark.parametrize("value", ["bogus", "", "delete", None])
def test_parse_config_falls_back_to_keep(plugin_module, value):
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config({"session_reset_policy": value})
    assert cfg.session_reset_policy == "keep"


# ── Schema ────────────────────────────────────────────────────────────────────


def test_conversation_cache_has_archived_at_column(plugin):
    with plugin._db() as conn:
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(conversation_cache)").fetchall()
        }
    assert "archived_at" in cols


# ── 钩子注册 ──────────────────────────────────────────────────────────────────


def test_register_hook_registers_callback(plugin, caplog):
    plugin.context = _FakeContext()
    with caplog.at_level(logging.INFO):
        assert plugin._register_session_lifecycle_hooks() is True
    assert len(plugin.context.conversation_manager.callbacks) == 1
    assert "会话删除钩子已注册" in caplog.text


def test_register_hook_returns_false_without_capability(plugin, caplog):
    plugin.context = _NoHookContext()
    with caplog.at_level(logging.INFO):
        assert plugin._register_session_lifecycle_hooks() is False
    assert "会话删除钩子不可用" in caplog.text


def test_register_hook_returns_false_without_context(plugin):
    plugin.context = None
    assert plugin._register_session_lifecycle_hooks() is False


# ── 三态行为 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keep_policy_leaves_cache_intact(plugin, caplog):
    umo = "qq:FriendMessage:u-keep"
    await _seed(plugin, "u-keep", umo, [("user", "hello"), ("assistant", "hi")])
    with caplog.at_level(logging.INFO):
        await plugin._on_session_deleted(umo)
    rows = _rows(plugin, umo)
    assert len(rows) == 2
    assert all(r["archived_at"] == "" for r in rows)
    assert "会话删除观测" in caplog.text
    assert "policy=keep" in caplog.text


@pytest.mark.asyncio
async def test_archive_policy_marks_rows_and_hides_working_context(plugin):
    umo = "qq:FriendMessage:u-archive"
    plugin._cfg.session_reset_policy = "archive"
    await _seed(plugin, "u-archive", umo, [("user", "turn one"), ("user", "turn two")])

    await plugin._on_session_deleted(umo)

    rows = _rows(plugin, umo)
    assert len(rows) == 2
    assert all(r["archived_at"] != "" for r in rows)
    # 工作上下文不再召回
    assert plugin._retrieval_mgr.retrieve_working_context("u-archive", umo, limit=5) == []
    # 归档行仍具蒸馏资格（长期记忆保留）
    assert len(plugin._fetch_pending_rows("u-archive", limit=10)) == 2


@pytest.mark.asyncio
async def test_clear_policy_deletes_rows_but_preserves_evidence(plugin):
    umo = "qq:FriendMessage:u-clear"
    plugin._cfg.session_reset_policy = "clear"
    await _seed(plugin, "u-clear", umo, [("user", "keep-evidence"), ("user", "drop-me")])

    with plugin._db() as conn:
        evidence_id = conn.execute(
            "SELECT id FROM conversation_cache WHERE content='keep-evidence'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO profile_items"
            "(canonical_user_id, facet_type, content, normalized_content, status,"
            " confidence, importance, stability, created_at, updated_at)"
            " VALUES('u-clear','fact','ev','ev','active',0.9,0.9,0.5,?,?)",
            (plugin._now(), plugin._now()),
        )
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO profile_item_evidence"
            "(profile_item_id, conversation_cache_id, canonical_user_id, created_at)"
            " VALUES(?,?,?,?)",
            (item_id, evidence_id, "u-clear", plugin._now()),
        )

    await plugin._on_session_deleted(umo)

    remaining = {r["content"] for r in _rows(plugin, umo)}
    assert remaining == {"keep-evidence"}


@pytest.mark.asyncio
async def test_empty_umo_is_noop(plugin, caplog):
    plugin._cfg.session_reset_policy = "clear"
    with caplog.at_level(logging.INFO):
        await plugin._on_session_deleted("")
    assert "(empty)" in caplog.text


@pytest.mark.asyncio
async def test_registered_callback_applies_policy(plugin):
    umo = "qq:FriendMessage:u-hook"
    plugin._cfg.session_reset_policy = "archive"
    plugin.context = _FakeContext()
    assert plugin._register_session_lifecycle_hooks() is True
    await _seed(plugin, "u-hook", umo, [("user", "hooked turn")])

    await plugin.context.conversation_manager.callbacks[0](umo)

    assert all(r["archived_at"] != "" for r in _rows(plugin, umo))


# ── /new /reset 轮转检测 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotation_baseline_then_applies_policy(plugin):
    umo = "qq:FriendMessage:u-rot"
    plugin._cfg.session_reset_policy = "archive"
    plugin.context = _RotatingContext("cid-1")
    await _seed(plugin, "u-rot", umo, [("user", "before reset")])

    event = _DummyEvent(umo)
    # 首次只建立基线，不误判
    assert await plugin._maybe_handle_session_rotation(event) is False
    assert all(r["archived_at"] == "" for r in _rows(plugin, umo))

    # /new /reset 之后对话 ID 变化 → 触发轮转
    plugin.context.conversation_manager.cid = "cid-2"
    assert await plugin._maybe_handle_session_rotation(event) is True
    assert all(r["archived_at"] != "" for r in _rows(plugin, umo))

    # 同一对话重复消息不再重复触发
    assert await plugin._maybe_handle_session_rotation(event) is False


@pytest.mark.asyncio
async def test_rotation_clear_policy_removes_rows(plugin):
    umo = "qq:FriendMessage:u-rot-clear"
    plugin._cfg.session_reset_policy = "clear"
    plugin.context = _RotatingContext("cid-a")
    await _seed(plugin, "u-rot-clear", umo, [("user", "old turn")])

    event = _DummyEvent(umo)
    await plugin._maybe_handle_session_rotation(event)
    plugin.context.conversation_manager.cid = "cid-b"
    assert await plugin._maybe_handle_session_rotation(event) is True
    assert _rows(plugin, umo) == []


@pytest.mark.asyncio
async def test_rotation_noop_without_conversation_id(plugin):
    umo = "qq:FriendMessage:u-no-cid"
    plugin.context = _RotatingContext("")
    event = _DummyEvent(umo)
    assert await plugin._maybe_handle_session_rotation(event) is False


# ── Agent 运行观测 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_hooks_observe_and_log(plugin, caplog):
    with caplog.at_level(logging.DEBUG):
        await plugin._handle_on_agent_begin(_DummyEvent("qq:FriendMessage:agent"))
        await plugin._handle_on_agent_done(_DummyEvent("qq:FriendMessage:agent"))
    assert plugin._agent_begin_count == 1
    assert plugin._agent_done_count == 1
    assert "on_agent_begin" in caplog.text
    assert "on_agent_done" in caplog.text


def test_version_probe_exposes_agent_hooks_flag(plugin_module):
    from astrbot_plugin_tmemory.adapters import version

    version.reset_capability_cache()
    try:
        caps = version.get_capabilities()
        assert hasattr(caps, "has_agent_hooks")
        data = caps.to_dict()
        assert "has_agent_hooks" in data
        assert "agent_hooks_min_version" in data
    finally:
        version.reset_capability_cache()
