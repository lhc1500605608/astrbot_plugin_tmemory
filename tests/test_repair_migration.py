from types import SimpleNamespace


def test_migration_quarantines_ambiguous_legacy_and_is_idempotent(plugin):
    from astrbot_plugin_tmemory.core.canonical_store import migrate_legacy
    old = plugin._insert_memory("u", "bot", "u", "用户喜欢使用Python语言", .8, "preference", .8, .8)
    with plugin._db() as conn:
        first = migrate_legacy(conn, plugin._now())
        second = migrate_legacy(conn, plugin._now())
        assert first == second
        assert first["quarantined"] == 1
        assert conn.execute("SELECT review_status FROM profile_items").fetchone()[0] == "pending"
        assert conn.execute("SELECT legacy_id FROM legacy_record_map").fetchone()[0] == old


def test_v2_exports_profile_only_user_and_restores_graph(plugin, plugin_module, tmp_path):
    from astrbot_plugin_tmemory.core.profile_ops import ProfileItemOps
    from astrbot_plugin_tmemory.core.portability import build_export, import_dataset
    from astrbot_plugin_tmemory.core.db import DatabaseManager
    ops = ProfileItemOps(plugin)
    item = ops.upsert_profile_item("profile-only", "fact", "职业", "用户是一名软件工程师", .9, .8, "bot|private", "p1")
    with plugin._db() as conn:
        cid = conn.execute("INSERT INTO conversation_cache(canonical_user_id,role,content,scope,persona_id,created_at) VALUES(?,?,?,?,?,?)", ("profile-only", "user", "用户是一名软件工程师", "bot|private", "p1", plugin._now())).lastrowid
    ops.add_evidence(item, "profile-only", [cid])
    exported = build_export(plugin)
    assert exported["schema_version"] == 2
    assert exported["users"][0]["tables"]["profile_items"][0]["id"] == item
    target = plugin_module.TMemoryPlugin(context=None, config={})
    target._db_mgr = DatabaseManager(str(tmp_path / "restore.sqlite3"))
    target._init_db()
    before = import_dataset(target, exported, dry_run=True)
    assert before["ok"]
    result = import_dataset(target, exported, dry_run=False, backup=False)
    assert result["ok"], result
    repeat = import_dataset(target, exported, dry_run=False, backup=False)
    assert repeat["ok"] and repeat["inserted"] == 0
    with target._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_item_evidence").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM profile_items").fetchone()[0] == 1
    target._close_db()
