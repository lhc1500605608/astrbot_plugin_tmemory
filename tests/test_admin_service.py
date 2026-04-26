"""AdminService 单元测试。

覆盖 core/admin_service.py 中的查询与写操作用例。
"""

import asyncio
import hashlib

import pytest


@pytest.fixture()
def admin(plugin):
    """构造绑定到测试 plugin 实例的 AdminService。"""
    # 需要先安装 core 包路径
    import sys, types
    from pathlib import Path

    ROOT = Path(__file__).resolve().parents[1]

    # 确保 core 子包可导入
    pkg_name = "astrbot_plugin_tmemory"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = package

    core_pkg = f"{pkg_name}.core"
    if core_pkg not in sys.modules:
        core_mod = types.ModuleType(core_pkg)
        core_mod.__path__ = [str(ROOT / "core")]
        sys.modules[core_pkg] = core_mod

    # 加载 admin_service 模块
    import importlib.util

    for mod_name, filename in [
        (f"{core_pkg}.admin_service", "core/admin_service.py"),
    ]:
        if mod_name not in sys.modules:
            spec = importlib.util.spec_from_file_location(mod_name, ROOT / filename)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)

    from astrbot_plugin_tmemory.core.admin_service import AdminService
    return AdminService(plugin)


# =========================================================================
# Batch 1.1 — 只读查询
# =========================================================================


class TestGetUsers:
    def test_empty_db_returns_empty_list(self, admin):
        assert admin.get_users() == []

    def test_returns_merged_user_list(self, admin, plugin):
        # 插入一条记忆
        plugin._insert_memory(
            canonical_id="user1",
            adapter="test",
            adapter_user="u1",
            memory="喜欢喝咖啡",
            score=0.8,
            memory_type="preference",
            importance=0.7,
            confidence=0.9,
        )
        users = admin.get_users()
        assert len(users) == 1
        assert users[0]["id"] == "user1"
        assert users[0]["memory_count"] == 1
        assert users[0]["pending_count"] == 0


class TestGetGlobalStats:
    def test_returns_stats_dict(self, admin, plugin):
        stats = admin.get_global_stats()
        assert "total_users" in stats
        assert "total_active_memories" in stats
        assert "pending_users" in stats
        assert isinstance(stats["pending_users"], int)


class TestGetMemories:
    def test_empty_user_returns_empty(self, admin):
        assert admin.get_memories("") == []

    def test_returns_user_memories(self, admin, plugin):
        plugin._insert_memory(
            canonical_id="alice",
            adapter="test",
            adapter_user="alice",
            memory="每天跑步五公里",
            score=0.7,
            memory_type="fact",
            importance=0.6,
            confidence=0.8,
        )
        memories = admin.get_memories("alice")
        assert len(memories) == 1
        assert memories[0]["memory"] == "每天跑步五公里"
        assert memories[0]["memory_type"] == "fact"
        # 验证 DTO 形状
        for key in ("id", "score", "importance", "confidence", "is_pinned",
                     "last_seen_at", "created_at", "updated_at"):
            assert key in memories[0]


class TestGetEvents:
    def test_empty_user_returns_empty(self, admin):
        assert admin.get_events("") == []


class TestGetPending:
    def test_empty_db(self, admin):
        assert admin.get_pending() == []


class TestGetIdentities:
    def test_empty_db(self, admin):
        assert admin.get_identities() == []

    def test_returns_binding_after_insert(self, admin, plugin):
        plugin._identity_mgr.bind_identity("qq", "12345", "canonical-a")
        bindings = admin.get_identities()
        assert len(bindings) >= 1
        b = [x for x in bindings if x["adapter_user_id"] == "12345"]
        assert len(b) == 1
        assert b[0]["canonical_user_id"] == "canonical-a"


class TestGetDistillHistory:
    def test_returns_list(self, admin):
        history = admin.get_distill_history(limit=10)
        assert isinstance(history, list)


class TestCountPendingUsers:
    def test_returns_zero_on_empty(self, admin):
        assert admin.count_pending_users() == 0


# =========================================================================
# Batch 1.2 — 低风险写操作
# =========================================================================


class TestAddMemory:
    def test_adds_and_returns_id(self, admin):
        mem_id = admin.add_memory(
            user="bob",
            memory="喜欢蓝色",
            score=0.8,
            memory_type="preference",
            importance=0.7,
            confidence=0.9,
        )
        assert isinstance(mem_id, int)
        assert mem_id > 0
        # 验证可以查回来
        memories = admin.get_memories("bob")
        assert any(m["id"] == mem_id for m in memories)


class TestUpdateMemory:
    def test_update_score_and_memory(self, admin):
        mem_id = admin.add_memory(user="carol", memory="原始文本", score=0.5)
        admin.update_memory(mem_id, {
            "user": "carol",
            "score": 0.9,
            "memory": "更新后的文本",
        })
        memories = admin.get_memories("carol")
        updated = [m for m in memories if m["id"] == mem_id]
        assert len(updated) == 1
        assert updated[0]["memory"] == "更新后的文本"
        assert abs(updated[0]["score"] - 0.9) < 0.01

    def test_update_no_fields_raises(self, admin):
        mem_id = admin.add_memory(user="carol2", memory="test")
        with pytest.raises(ValueError, match="no fields"):
            admin.update_memory(mem_id, {"user": "carol2"})


class TestDeleteMemory:
    def test_delete_existing(self, admin):
        mem_id = admin.add_memory(user="dave", memory="临时记忆")
        assert admin.delete_memory(mem_id) is True
        assert admin.get_memories("dave") == []

    def test_delete_nonexistent(self, admin):
        assert admin.delete_memory(999999) is False


class TestSetPinned:
    def test_pin_and_unpin(self, admin):
        mem_id = admin.add_memory(user="eve", memory="需要常驻的记忆")
        assert admin.set_pinned(mem_id, True) is True
        memories = admin.get_memories("eve")
        pinned = [m for m in memories if m["id"] == mem_id]
        assert pinned[0]["is_pinned"] == 1

        assert admin.set_pinned(mem_id, False) is True
        memories = admin.get_memories("eve")
        unpinned = [m for m in memories if m["id"] == mem_id]
        assert unpinned[0]["is_pinned"] == 0


class TestStyleProfiles:
    def test_style_profile_crud_and_binding_roundtrip(self, admin):
        created = admin.create_style_profile(
            "qa-style", "请使用更有条理的语气。", "qa profile"
        )

        assert created["id"] > 0
        assert admin.set_style_binding("qq", "conv-a", created["id"]) is True
        binding = admin.get_style_binding("qq", "conv-a")
        assert binding["profile_id"] == created["id"]
        assert binding["prompt_supplement"] == "请使用更有条理的语气。"

        assert admin.delete_style_profile(created["id"]) is True
        binding = admin.get_style_binding("qq", "conv-a")
        assert binding["profile_id"] is None

    def test_auto_create_profile_after_three_style_memories(self, admin, plugin):
        for i in range(3):
            plugin._insert_memory(
                canonical_id="style-user",
                adapter="qq",
                adapter_user="42",
                memory=f"用户偏好简洁表达风格{i}",
                score=0.8,
                memory_type="style",
                importance=0.7,
                confidence=0.9,
            )

        profile_id = plugin._style_mgr.auto_create_profile_if_ready("style-user", "qq")
        profile = admin.get_style_profile(profile_id)

        assert profile["profile_name"] == "style-user-auto-style"
        assert "用户偏好简洁表达风格" in profile["prompt_supplement"]


# =========================================================================
# Batch 1.3 — 高风险写操作
# =========================================================================


class TestSetDistillPause:
    def test_sets_cfg_attribute(self, admin, plugin):
        admin.set_distill_pause(True)
        assert plugin._cfg.distill_pause is True
        admin.set_distill_pause(False)
        assert plugin._cfg.distill_pause is False


class TestMergeUsers:
    def test_merge_moves_memories(self, admin, plugin):
        plugin._insert_memory(
            canonical_id="src_user",
            adapter="test",
            adapter_user="src",
            memory="来源用户的记忆",
            score=0.7,
            memory_type="fact",
            importance=0.6,
            confidence=0.8,
        )
        moved = admin.merge_users("src_user", "dst_user")
        assert moved >= 1
        # 来源用户应该没有记忆了
        assert admin.get_memories("src_user") == []
        # 目标用户应该有
        assert len(admin.get_memories("dst_user")) >= 1


class TestRebindUser:
    def test_rebind_existing_binding(self, admin, plugin):
        plugin._identity_mgr.bind_identity("wechat", "wx123", "old-canonical")
        bindings = admin.get_identities()
        bid = [b for b in bindings if b["adapter_user_id"] == "wx123"][0]["id"]

        result = admin.rebind_user(bid, "new-canonical")
        assert result["old_canonical"] == "old-canonical"

        # 验证绑定已更新
        bindings = admin.get_identities()
        updated = [b for b in bindings if b["adapter_user_id"] == "wx123"]
        assert updated[0]["canonical_user_id"] == "new-canonical"

    def test_rebind_nonexistent_raises(self, admin):
        with pytest.raises(LookupError, match="not found"):
            admin.rebind_user(999999, "new-canonical")


class TestExportUser:
    def test_export_returns_data(self, admin, plugin):
        plugin._insert_memory(
            canonical_id="export_user",
            adapter="test",
            adapter_user="eu",
            memory="可导出的记忆",
            score=0.8,
            memory_type="fact",
            importance=0.7,
            confidence=0.9,
        )
        export = admin.export_user("export_user")
        assert export["canonical_user_id"] == "export_user"
        assert "memories" in export
        assert "exported_at" in export


class TestPurgeUser:
    def test_purge_clears_data(self, admin, plugin):
        plugin._insert_memory(
            canonical_id="purge_user",
            adapter="test",
            adapter_user="pu",
            memory="即将被清除的记忆",
            score=0.8,
            memory_type="fact",
            importance=0.7,
            confidence=0.9,
        )
        result = admin.purge_user("purge_user")
        assert result["memories"] >= 1
        assert admin.get_memories("purge_user") == []

    @pytest.mark.asyncio
    async def test_purge_removes_user_from_all_webui_user_sources(self, admin, plugin):
        plugin._identity_mgr.bind_identity("qq", "purge-source", "purge_user")
        plugin._insert_memory(
            canonical_id="purge_user",
            adapter="qq",
            adapter_user="purge-source",
            memory="待删除用户的记忆",
            score=0.8,
            memory_type="fact",
            importance=0.7,
            confidence=0.9,
        )
        await plugin._insert_conversation(
            canonical_id="purge_user",
            role="user",
            content="待删除用户的缓存",
            source_adapter="qq",
            source_user_id="purge-source",
            unified_msg_origin="group:1",
        )
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO conversations(canonical_user_id, role, content, timestamp) VALUES(?, ?, ?, ?)",
                ("purge_user", "user", "历史会话", plugin._now()),
            )

        result = admin.purge_user("purge_user")

        with plugin._db() as conn:
            counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE canonical_user_id=?",
                    ("purge_user",),
                ).fetchone()["n"]
                for table in (
                    "memories",
                    "conversation_cache",
                    "conversations",
                    "identity_bindings",
                )
            }

        assert result["memories"] == 1
        assert result["cache"] == 1
        assert counts == {
            "memories": 0,
            "conversation_cache": 0,
            "conversations": 0,
            "identity_bindings": 0,
        }
        assert all(user["id"] != "purge_user" for user in admin.get_users())


class TestAdapterUserId:
    def test_get_adapter_user_id_ignores_platform_metadata_repr(self, plugin):
        class _Event:
            def get_sender_id(self):
                return None

            def get_user_id(self):
                return None

            def get_sender_name(self):
                return "alice"

            platform = "PlatformMetadata(name='qq', version='1')"

        assert plugin._get_adapter_user_id(_Event()) == "alice"


class TestPlatformMetadataRepr:
    """Regression: PlatformMetadata dataclass repr should never leak as adapter name."""

    @staticmethod
    def _inject_platform_metadata_class():
        """Ensure ``astrbot.core.platform.platform_metadata.PlatformMetadata`` is
        importable so that _platform_str's isinstance check actually fires.
        """
        import dataclasses
        import sys
        import types

        if "astrbot.core" not in sys.modules:
            astrbot_core = types.ModuleType("astrbot.core")
            sys.modules["astrbot.core"] = astrbot_core
        if "astrbot.core.platform" not in sys.modules:
            astrbot_platform = types.ModuleType("astrbot.core.platform")
            sys.modules["astrbot.core.platform"] = astrbot_platform
        pkg = types.ModuleType("astrbot.core.platform.platform_metadata")

        @dataclasses.dataclass
        class PlatformMetadata:
            name: str
            description: str
            id: str

        pkg.PlatformMetadata = PlatformMetadata
        sys.modules["astrbot.core.platform.platform_metadata"] = pkg
        return PlatformMetadata

    def test_identity_get_adapter_name_extracts_platform_id(self, plugin):
        PM = self._inject_platform_metadata_class()
        meta = PM(name="aiocqhttp", description="QQ适配器", id="qq_prod_1")
        assert meta.name in repr(meta) and meta.id in repr(meta)  # verbose dataclass repr

        class _Event:
            adapter_name = None
            platform = meta
            platform_id = None

        name = plugin._identity_mgr.get_adapter_name(_Event())
        assert name == "qq_prod_1"
        assert "PlatformMetadata" not in name

    def test_identity_get_adapter_name_falls_back_platform_id_str(self, plugin):
        class _Event:
            adapter_name = None
            platform = None
            platform_id = "custom_platform_42"

        name = plugin._identity_mgr.get_adapter_name(_Event())
        assert name == "custom_platform_42"

    def test_main_get_adapter_name_fallback_handles_platform_metadata(self, plugin):
        PM = self._inject_platform_metadata_class()
        meta = PM(name="discord", description="Discord适配器", id="dc_bot_7")

        class _Event:
            pass

        event = _Event()
        setattr(event, "platform", meta)

        name = plugin._get_adapter_name(event)
        assert name == "dc_bot_7"
        assert "PlatformMetadata" not in name

    def test_main_get_adapter_name_prefers_method_over_platform_attr(self, plugin):
        PM = self._inject_platform_metadata_class()
        meta = PM(name="slack", description="Slack适配器", id="slack_9")

        class _Event:
            def get_platform_name(self):
                return "slack"

            platform = meta

        name = plugin._get_adapter_name(_Event())
        assert name == "slack"
        assert "PlatformMetadata" not in name


# =========================================================================
# 内部方法
# =========================================================================


class TestFetchMemoriesById:
    def test_fetch_by_ids(self, admin, plugin):
        id1 = admin.add_memory(user="fetch_user", memory="记忆一")
        id2 = admin.add_memory(user="fetch_user", memory="记忆二")
        rows = admin._fetch_memories_by_ids("fetch_user", [id1, id2])
        assert len(rows) == 2


class TestAutoMergeMemoryText:
    def test_dedup_and_join(self, admin):
        result = admin._auto_merge_memory_text(["喜欢咖啡", "喜欢咖啡", "喜欢茶"])
        assert "咖啡" in result
        assert "茶" in result

    def test_empty_input(self, admin):
        assert admin._auto_merge_memory_text([]) == ""

    def test_single_item(self, admin):
        assert admin._auto_merge_memory_text(["单条记忆"]) == "单条记忆"


class TestUpdateMemoryText:
    def test_updates_text_and_hash(self, admin, plugin):
        mem_id = admin.add_memory(user="text_user", memory="原始文本")
        admin._update_memory_text(mem_id, "更新后的文本")
        memories = admin.get_memories("text_user")
        updated = [m for m in memories if m["id"] == mem_id]
        assert updated[0]["memory"] == "更新后的文本"


# =========================================================================
# 工具函数
# =========================================================================


class TestUtilityFunctions:
    def test_safe_memory_type(self):
        from astrbot_plugin_tmemory.core.admin_service import _safe_memory_type
        assert _safe_memory_type("preference") == "preference"
        assert _safe_memory_type("FACT") == "fact"
        assert _safe_memory_type("invalid") == "fact"
        assert _safe_memory_type(None) == "fact"

    def test_clamp01(self):
        from astrbot_plugin_tmemory.core.admin_service import _clamp01
        assert _clamp01(0.5) == 0.5
        assert _clamp01(-1) == 0.0
        assert _clamp01(2.0) == 1.0
        assert _clamp01("invalid") == 0.0

    def test_normalize_text(self):
        from astrbot_plugin_tmemory.core.admin_service import _normalize_text
        assert _normalize_text("  hello   world  ") == "hello world"
        assert _normalize_text("") == ""
        assert _normalize_text(None) == ""
