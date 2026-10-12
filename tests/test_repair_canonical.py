import pytest


@pytest.fixture
def canonical(plugin):
    from astrbot_plugin_tmemory.core.canonical_store import activate
    with plugin._db() as conn:
        activate(conn, plugin._now())
    plugin._canonical_store_active = True
    return plugin


def test_compatibility_view_writes_to_correct_store(canonical):
    p = canonical
    fact = p._insert_memory("u", "bot1", "u", "用户偏好使用Python语言", .8, "preference", .8, .8, scope="bot1|private", persona_id="a")
    event = p._insert_memory("u", "bot1", "u", "用户2026-10-15参加考试", .8, "event", .8, .8, scope="bot1|private", persona_id="a", event_date="2026-10-15", valid_until="2026-10-16")
    with p._db() as conn:
        assert conn.execute("SELECT type FROM sqlite_master WHERE name='memories'").fetchone()[0] == "view"
        assert conn.execute("SELECT COUNT(*) FROM profile_items").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM event_items").fetchone()[0] == 1
        assert conn.execute("SELECT event_date FROM memories WHERE id=?", (event,)).fetchone()[0] == "2026-10-15"
        conn.execute("UPDATE memories SET is_pinned=1 WHERE id=?", (fact,))
        assert conn.execute("SELECT is_pinned FROM profile_items").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_canonical_recall_isolated_and_new_session_reuses_memory(canonical):
    p = canonical
    p._insert_memory("u", "bot1", "u", "用户偏好使用Python语言", .8, "preference", .8, .8, scope="bot1|private", persona_id="a")
    for scope, persona, expected in [("bot1|private", "a", True), ("bot1|group:g", "a", False), ("bot2|private", "a", False), ("bot1|private", "b", False)]:
        hits = await p._retrieve_memories("u", "Python", 5, scope=scope, persona_id=persona)
        assert bool(hits) is expected
        items = p._retrieval_mgr.retrieve_profile_items("u", "Python", 5, scope=scope, persona_id=persona)
        assert bool(items) is expected
    block = await p._injection_builder.build_profile_injection("u", "Python", "new-dialogue", scope="bot1|private", persona_id="a")
    assert "Python" in block


def test_canonical_can_reopen_and_purge(canonical):
    from astrbot_plugin_tmemory.core.canonical_store import install_functions
    p = canonical
    p._insert_memory("u", "bot1", "u", "用户偏好使用Python语言", .8, "preference", .8, .8, scope="bot1|private")
    p._close_db()
    p._init_db()
    p._migrate_schema()
    p._purge_user_data("u")
    with p._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_record_ids").fetchone()[0] == 0


def test_public_id_and_explicit_review(canonical):
    from astrbot_plugin_tmemory.core.admin_service import AdminService
    p = canonical
    alias = p._insert_memory("u", "webui", "u", "用户偏好使用Python语言", .8, "preference", .8, .8)
    public = p._public_memory_id(alias)
    assert public.startswith("p:")
    assert p._resolve_memory_id(public) == alias
    with p._db() as conn:
        assert conn.execute("SELECT is_active FROM memories WHERE id=?", (alias,)).fetchone()[0] == 0
    AdminService(p).review_memory(public, "bot1|private", "a")
    assert p._set_pinned(public, True)
    with p._db() as conn:
        assert conn.execute("SELECT is_active FROM memories WHERE id=?", (alias,)).fetchone()[0] == 1
    assert p._delete_memory(public)


def test_native_backup_roundtrip_remaps_ids(canonical, plugin_module, tmp_path):
    from astrbot_plugin_tmemory.core.canonical_store import activate
    from astrbot_plugin_tmemory.core.db import DatabaseManager
    from astrbot_plugin_tmemory.core.portability import build_export, import_dataset
    p = canonical
    p._insert_memory("u", "bot1", "u", "用户2026-10-15参加考试", .8, "event", .8, .8, scope="bot1|private", event_date="2026-10-15")
    payload = build_export(p, ["u"])
    target = plugin_module.TMemoryPlugin(context=None, config={})
    target._db_mgr = DatabaseManager(str(tmp_path / "target.sqlite3"))
    target._init_db()
    with target._db() as conn:
        activate(conn, target._now())
    target._canonical_store_active = True
    target._insert_memory("other", "bot1", "other", "用户另有自己的考试计划", .8, "event", .8, .8, scope="bot1|private")
    result = import_dataset(target, payload, dry_run=False, backup=False)
    assert result["ok"], result
    with target._db() as conn:
        assert conn.execute("SELECT event_date FROM event_items WHERE canonical_user_id='u'").fetchone()[0] == "2026-10-15"
        assert conn.execute("SELECT COUNT(*) FROM memory_record_ids").fetchone()[0] == 2
    target._close_db()


def test_completed_tasks_and_expired_events_excluded(canonical):
    from astrbot_plugin_tmemory.core.admin_service import AdminService
    p = canonical
    task = p._insert_memory("u", "bot1", "u", "用户需要在周末完成报告", .8, "task", .8, .8, scope="bot1|private")
    AdminService(p).update_memory(p._public_memory_id(task), {"task_status":"completed"})
    event = p._insert_memory("u", "bot1", "u", "用户曾参加2020年的会议", .8, "event", .8, .8, scope="bot1|private", valid_until="2020-01-01")
    with p._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories WHERE is_active=1").fetchone()[0] == 0
        assert conn.execute("SELECT task_status FROM event_items WHERE id=(SELECT object_id FROM memory_record_ids WHERE id=?)", (task,)).fetchone()[0] == "completed"


@pytest.mark.parametrize("boundary", ["scope", "persona"])
def test_v2_import_rejects_cross_boundary_episode_source(canonical, plugin_module, tmp_path, boundary):
    from astrbot_plugin_tmemory.core.canonical_store import activate
    from astrbot_plugin_tmemory.core.db import DatabaseManager
    from astrbot_plugin_tmemory.core.portability import build_export, import_dataset

    with canonical._db() as conn:
        episode = conn.execute(
            "INSERT INTO memory_episodes(canonical_user_id,scope,persona_id,episode_title,episode_summary,first_source_at,last_source_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("u", "bot1|private", "a", "title", "summary", "2026-01-01", "2026-01-01", "2026-01-01", "2026-01-01"),
        ).lastrowid
        cache = conn.execute(
            "INSERT INTO conversation_cache(canonical_user_id,role,content,created_at,scope,persona_id) VALUES(?,?,?,?,?,?)",
            ("u", "user", "private source", "2026-01-01", "bot1|group:g" if boundary == "scope" else "bot1|private", "a" if boundary == "scope" else "b"),
        ).lastrowid
        conn.execute("INSERT INTO episode_sources(episode_id,conversation_cache_id,canonical_user_id,created_at) VALUES(?,?,?,?)", (episode, cache, "u", "2026-01-01"))
    target = plugin_module.TMemoryPlugin(context=None, config={})
    target._db_mgr = DatabaseManager(str(tmp_path / f"{boundary}.sqlite3"))
    target._init_db()
    with target._db() as conn:
        activate(conn, target._now())
    result = import_dataset(target, build_export(canonical, ["u"]), dry_run=False, backup=False)
    assert result["rolled_back"] is True
    assert "episode source crosses isolation boundary" in result["error"]
    with target._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM episode_sources").fetchone()[0] == 0
    target._close_db()


def test_v2_import_rejects_cross_boundary_profile_relation(canonical, plugin_module, tmp_path):
    from astrbot_plugin_tmemory.core.canonical_store import activate
    from astrbot_plugin_tmemory.core.db import DatabaseManager
    from astrbot_plugin_tmemory.core.portability import build_export, import_dataset

    first = canonical._insert_memory("u", "bot1", "u", "用户喜欢蓝色", .8, "preference", .8, .8, scope="bot1|private", persona_id="a")
    second = canonical._insert_memory("u", "bot1", "u", "用户喜欢绿色", .8, "preference", .8, .8, scope="bot1|group:g", persona_id="a")
    with canonical._db() as conn:
        left = conn.execute("SELECT object_id FROM memory_record_ids WHERE id=?", (first,)).fetchone()[0]
        right = conn.execute("SELECT object_id FROM memory_record_ids WHERE id=?", (second,)).fetchone()[0]
        conn.execute("INSERT INTO profile_relations(canonical_user_id,from_item_id,to_item_id,relation_type,created_at,updated_at) VALUES(?,?,?,?,?,?)", ("u", left, right, "supports", "2026-01-01", "2026-01-01"))
    target = plugin_module.TMemoryPlugin(context=None, config={})
    target._db_mgr = DatabaseManager(str(tmp_path / "relations.sqlite3"))
    target._init_db()
    with target._db() as conn:
        activate(conn, target._now())
    result = import_dataset(target, build_export(canonical, ["u"]), dry_run=False, backup=False)
    assert result["rolled_back"] is True
    assert "profile relation crosses isolation boundary" in result["error"]
    with target._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile_relations").fetchone()[0] == 0
    target._close_db()


@pytest.mark.asyncio
async def test_admin_split_keeps_scope_persona_and_merge_rejects_cross_scope(canonical):
    from astrbot_plugin_tmemory.core.admin_service import AdminService
    admin = AdminService(canonical)
    first = canonical._insert_memory("u", "bot1", "u", "用户喜欢蓝色和绿色", .8, "preference", .8, .8, scope="bot1|private", persona_id="a")
    result = await admin.split_memory("u", first, ["用户喜欢蓝色", "用户喜欢绿色"])
    with canonical._db() as conn:
        row = conn.execute("SELECT scope,persona_id FROM memories WHERE id=?", (result["added_ids"][0],)).fetchone()
        assert tuple(row) == ("bot1|private", "a")
    other = canonical._insert_memory("u", "bot1", "u", "用户喜欢红色", .8, "preference", .8, .8, scope="bot1|group:g", persona_id="a")
    with pytest.raises(ValueError, match="isolation boundary"):
        await admin.merge_memories("u", [first, other])
