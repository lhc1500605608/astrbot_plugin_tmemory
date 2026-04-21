import asyncio


def test_init_db_creates_core_tables(plugin):
    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()

    table_names = {row["name"] for row in rows}
    assert "identity_bindings" in table_names
    assert "memories" in table_names
    assert "conversation_cache" in table_names
    assert "memory_events" in table_names
    assert "distill_history" in table_names


def test_insert_conversation_truncates_content(plugin):
    plugin._insert_conversation(
        canonical_id="user-1",
        role="user",
        content="x" * 1205,
        source_adapter="qq",
        source_user_id="10001",
        unified_msg_origin="origin-1",
    )

    rows = plugin._fetch_recent_conversation("user-1", limit=5)
    assert rows == [("user", "x" * 1000)]


def test_insert_memory_duplicate_reinforces_existing_row(plugin):
    first_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="10001",
        memory="likes green tea every morning",
        score=0.7,
        memory_type="preference",
        importance=0.8,
        confidence=0.6,
    )
    second_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="10001",
        memory="likes green tea every morning",
        score=0.9,
        memory_type="preference",
        importance=0.9,
        confidence=0.9,
    )

    with plugin._db() as conn:
        row = conn.execute(
            "SELECT reinforce_count, score, importance, confidence FROM memories WHERE id=?",
            (first_id,),
        ).fetchone()
        total_rows = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]

    assert second_id == first_id
    assert total_rows == 1
    assert row["reinforce_count"] == 2
    assert row["score"] == 0.9
    assert row["importance"] == 0.9
    assert row["confidence"] == 0.9


def test_insert_memory_conflict_deactivates_previous_memory(plugin):
    old_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="10001",
        memory="likes green tea every morning",
        score=0.7,
        memory_type="preference",
        importance=0.8,
        confidence=0.6,
    )
    new_id = plugin._insert_memory(
        canonical_id="user-1",
        adapter="qq",
        adapter_user="10001",
        memory="likes green tea with honey morning",
        score=0.9,
        memory_type="preference",
        importance=0.9,
        confidence=0.95,
    )

    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT id, is_active FROM memories WHERE canonical_user_id=? ORDER BY id ASC",
            ("user-1",),
        ).fetchall()
        event = conn.execute(
            "SELECT event_type FROM memory_events WHERE canonical_user_id=? ORDER BY id DESC LIMIT 1",
            ("user-1",),
        ).fetchone()

    assert [row["id"] for row in rows] == [old_id, new_id]
    assert [row["is_active"] for row in rows] == [0, 1]
    assert event["event_type"] == "create_with_conflict"


def test_merge_identity_moves_memories_bindings_and_cache(plugin):
    plugin._bind_identity("qq", "from-user", "from-id")
    plugin._bind_identity("wx", "to-user", "to-id")
    plugin._insert_memory(
        canonical_id="from-id",
        adapter="qq",
        adapter_user="from-user",
        memory="works remotely from shanghai",
        score=0.8,
        memory_type="fact",
        importance=0.8,
        confidence=0.9,
    )
    plugin._insert_conversation(
        canonical_id="from-id",
        role="user",
        content="hello from old identity",
        source_adapter="qq",
        source_user_id="from-user",
        unified_msg_origin="origin-merge",
    )

    moved = plugin._merge_identity("from-id", "to-id")

    with plugin._db() as conn:
        memory_row = conn.execute(
            "SELECT canonical_user_id FROM memories WHERE memory=?",
            ("works remotely from shanghai",),
        ).fetchone()
        cache_row = conn.execute(
            "SELECT canonical_user_id FROM conversation_cache WHERE unified_msg_origin=?",
            ("origin-merge",),
        ).fetchone()
        binding_row = conn.execute(
            "SELECT canonical_user_id FROM identity_bindings WHERE adapter=? AND adapter_user_id=?",
            ("qq", "from-user"),
        ).fetchone()

    assert moved == 1
    assert memory_row["canonical_user_id"] == "to-id"
    assert cache_row["canonical_user_id"] == "to-id"
    assert binding_row["canonical_user_id"] == "to-id"


def test_terminate_closes_resources(plugin):
    class FakeWebServer:
        def __init__(self):
            self.stopped = False

        async def stop(self):
            self.stopped = True

    class FakeVectorManager:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def run_test():
        plugin._db()
        plugin._web_server = FakeWebServer()
        plugin._vector_manager = FakeVectorManager()
        plugin._distill_task = asyncio.create_task(asyncio.sleep(60))
        await plugin.terminate()
        return plugin._web_server.stopped, plugin._vector_manager.closed, plugin._conn

    stopped, closed, conn = asyncio.run(run_test())

    assert stopped is True
    assert closed is True
    assert conn is None
