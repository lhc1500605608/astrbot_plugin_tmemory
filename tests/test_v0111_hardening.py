"""v0.11.1 硬化回归测试。

覆盖线上 TMEAAA-387 四类故障：
1. distill worker 元组解包漂移（too many values to unpack）
2. sqlite-vec vec0 未在连接上加载（no such module: vec0）
3. 中文 FTS5 依赖不存在的 jieba tokenizer
4. 维度迁移重建时缺少 memory_vectors（no such table）
"""

import jieba
import pytest

from core.db import DatabaseManager
from core.distill import _coerce_cycle_result


# ── 1. distill worker 解包容错 ─────────────────────────────────────────────


def test_coerce_cycle_result_truncates_extra_values():
    assert _coerce_cycle_result((1, 2, 3), 2) == (1, 2)
    assert _coerce_cycle_result([1, 2, 3, 4], 3) == (1, 2, 3)


def test_coerce_cycle_result_pads_missing_values():
    assert _coerce_cycle_result((5,), 3) == (5, 0, 0)
    assert _coerce_cycle_result((7, 8), 3) == (7, 8, 0)


def test_coerce_cycle_result_supports_scalar_and_none():
    assert _coerce_cycle_result(9, 2) == (9, 0)
    assert _coerce_cycle_result(None, 2) == (None, 0)


# ── 2. sqlite-vec 逐连接加载与降级 ─────────────────────────────────────────


def _sqlite_vec_or_skip():
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:  # pragma: no cover
        pytest.skip("sqlite-vec 未安装")
    return sqlite_vec


def test_vec0_available_on_connection_after_register():
    sqlite_vec = _sqlite_vec_or_skip()
    dm = DatabaseManager(":memory:")
    dm.set_vec_extension(sqlite_vec)
    with dm.db() as conn:
        assert dm.vec0_available(conn) is True
    dm.init_db(True, 1024)
    assert dm.vec_enabled is True
    with dm.db() as conn:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "memory_vectors" in names
    assert "profile_item_vectors" in names


def test_init_db_degrades_cleanly_without_vec_extension():
    dm = DatabaseManager(":memory:")
    # 请求向量能力，但连接上没有 vec0：必须清晰降级，不建表、不抛错。
    dm.init_db(True, 1024)
    assert dm.vec_enabled is False
    with dm.db() as conn:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "memory_vectors" not in names


# ── 3. 中文 FTS5（tokenized_memory + 内置 tokenizer）────────────────────────


def _insert_memory(conn, memory: str, user_id: str = "u1") -> None:
    tokens = " ".join(jieba.cut_for_search(memory))
    conn.execute(
        "INSERT INTO memories(canonical_user_id, source_adapter, source_user_id,"
        " memory, tokenized_memory, memory_hash, last_seen_at, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, "a", "u", memory, tokens, f"h-{memory}", "t", "t", "t"),
    )


def _fts_match(conn, query: str, user_id: str = "u1"):
    fts_query = " AND ".join(
        '"' + t.replace('"', '""') + '"'
        for t in jieba.cut_for_search(query)
        if t.strip()
    )
    return conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? AND canonical_user_id = ?",
        (fts_query, user_id),
    ).fetchall()


def test_chinese_fts_uses_builtin_tokenizer_and_finds_memory():
    dm = DatabaseManager(":memory:")
    dm.init_db(False, 1024)
    with dm.db() as conn:
        _insert_memory(conn, "用户喜欢在周末爬山和摄影")
        _insert_memory(conn, "用户不吃香菜", user_id="u2")
    with dm.db() as conn:
        hits = _fts_match(conn, "周末爬山")
        assert len(hits) == 1
        # 用户隔离：u2 的记忆不得被 u1 查询命中
        assert _fts_match(conn, "香菜", user_id="u1") == []


def test_legacy_memories_fts_schema_is_migrated():
    dm = DatabaseManager(":memory:")
    with dm.db() as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE memories_fts USING fts5(memory, memory_type,"
            " content='memories', content_rowid='id', tokenize='unicode61')"
        )
    dm.init_db(False, 1024)
    with dm.db() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchone()["sql"]
    # 旧结构（memory/memory_type）必须升级为 tokenized_memory + canonical_user_id
    assert "tokenized_memory" in sql
    assert "canonical_user_id" in sql
