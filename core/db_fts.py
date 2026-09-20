"""FTS5 schema repair and initialization mixin for DatabaseManager.

Extracted from ``core/db.py`` per ADR-009 (module boundary split). Physical move
only — no behavior change.
"""

import logging
import sqlite3

import jieba

from .db_schema import (
    _DDL_MEMORY_FTS,
    _DDL_TRIGGER_AD,
    _DDL_TRIGGER_AI,
    _DDL_TRIGGER_AU,
    _FTS_MEMORY_TOKENIZER,
    _MEMORY_EPISODES_FTS_TRIGGERS,
    _PROFILE_ITEMS_FTS_TRIGGERS,
    _memory_episodes_fts_ddl,
    _profile_items_fts_ddl,
    _select_text_fts_tokenizer,
)

logger = logging.getLogger("astrbot")


class DbFtsMixin:
    def _migrate_fts5_to_content_sync(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone()
        if not row:
            return

        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone()
        if row:
            create_sql = str(row[0] or row["sql"] if isinstance(row, sqlite3.Row) else row[0])
            if "content=" in create_sql or "content =" in create_sql:
                return

        logger.info("[tmemory] 正在将 FTS5 表迁移到 content-sync 模式...")
        conn.execute("DROP TRIGGER IF EXISTS t_memories_ai")
        conn.execute("DROP TRIGGER IF EXISTS t_memories_ad")
        conn.execute("DROP TRIGGER IF EXISTS t_memories_au")
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        self._fts5_needs_rebuild = True

    @staticmethod
    def _fts_schema_stale(
        conn: sqlite3.Connection,
        table: str,
        expected_tokenizer: str,
        required_cols: tuple,
    ) -> bool:
        """返回 True 表示已存在的 FTS 表结构与当前策略不符，需要重建。"""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row:
            # 表缺失 = 需要新建 + rebuild；绝不能当作“无需重建”，否则会留下
            # 空索引 + 触发器的半初始化状态，后续 UPDATE 触发 malformed。
            return True
        sql = str(row["sql"] or "")
        if expected_tokenizer not in sql:
            return True
        return any(col not in sql for col in required_cols)

    def _drop_fts(self, conn: sqlite3.Connection, table: str, triggers: tuple) -> None:
        for trigger in triggers:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute(f"DROP TABLE IF EXISTS {table}")

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return bool(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        )

    @staticmethod
    def _fts_index_inconsistent(
        conn: sqlite3.Connection, fts_table: str, content_table: str
    ) -> bool:
        """检测外部内容 FTS 索引与内容表行数是否不一致（半初始化/损坏）。

        FTS5 external-content 表若在 CREATE 后未执行 `'rebuild'`，索引为空但
        触发器已建立；此后对内容表任何 UPDATE/DELETE 都会在空索引上执行
        'delete' 并抛出 ``database disk image is malformed``。这里用 docsize
        影子表行数与内容表行数比对来识别该状态（内容表为空则无需重建）。
        """
        if not DbFtsMixin._table_exists(conn, fts_table):
            return False
        if not DbFtsMixin._table_exists(conn, content_table):
            return False
        content_count = conn.execute(
            f"SELECT count(*) FROM {content_table}"
        ).fetchone()[0]
        if not content_count:
            return False
        try:
            indexed = conn.execute(
                f"SELECT count(*) FROM {fts_table}_docsize"
            ).fetchone()[0]
        except sqlite3.Error:
            return True
        return int(indexed) != int(content_count)

    def _repair_inconsistent_fts(self, conn: sqlite3.Connection) -> None:
        """拆除半初始化的 FTS 表，交由 ``_init_fts`` 重建，避免触发器在空索引
        上执行 'delete' 导致 ``database disk image is malformed``。"""
        pairs = (
            (
                "memories_fts",
                "memories",
                ("t_memories_ai", "t_memories_ad", "t_memories_au"),
            ),
            (
                "memory_episodes_fts",
                "memory_episodes",
                (
                    "t_memory_episodes_ai",
                    "t_memory_episodes_ad",
                    "t_memory_episodes_au",
                ),
            ),
            (
                "profile_items_fts",
                "profile_items",
                (
                    "t_profile_items_ai",
                    "t_profile_items_ad",
                    "t_profile_items_au",
                ),
            ),
        )
        for fts_table, content_table, triggers in pairs:
            if self._fts_index_inconsistent(conn, fts_table, content_table):
                logger.warning(
                    "[tmemory] %s 索引与 %s 行数不一致（疑似半初始化），拆除以便重建",
                    fts_table,
                    content_table,
                )
                self._drop_fts(conn, fts_table, triggers)
                self._fts5_needs_rebuild = True

    def _init_fts(self, conn: sqlite3.Connection) -> bool:
        """初始化 FTS5（内置 tokenizer）。失败时清晰降级，绝不留下半成品。"""
        text_tokenizer = _select_text_fts_tokenizer(conn)
        if text_tokenizer is None:
            logger.warning(
                "[tmemory] FTS5 不可用（当前 SQLite 无内置可用 tokenizer），中文检索降级为 LIKE。"
            )
            return False
        memory_triggers = ("t_memories_ai", "t_memories_ad", "t_memories_au")
        episode_triggers = (
            "t_memory_episodes_ai",
            "t_memory_episodes_ad",
            "t_memory_episodes_au",
        )
        profile_triggers = (
            "t_profile_items_ai",
            "t_profile_items_ad",
            "t_profile_items_au",
        )
        try:
            conn.execute("SAVEPOINT tmem_fts_init")
            # memories_fts：索引 jieba 预处理后的 tokenized_memory，unicode61。
            memories_rebuild = self._fts5_needs_rebuild or self._fts_schema_stale(
                conn, "memories_fts", _FTS_MEMORY_TOKENIZER,
                ("tokenized_memory", "canonical_user_id"),
            )
            if memories_rebuild:
                self._drop_fts(conn, "memories_fts", memory_triggers)

            # 历史记忆补齐 jieba 词元（FTS 索引源）。必须在触发器建立前完成：
            # 否则对空 FTS 索引执行 UPDATE 会经 t_memories_au 抛 malformed。
            untokenized = conn.execute(
                "SELECT id, memory FROM memories WHERE tokenized_memory = ''"
            ).fetchall()
            if untokenized:
                logger.info("[tmemory] 正在为 %s 条历史记忆生成全文检索词元...", len(untokenized))
                for row in untokenized:
                    tokens = " ".join(jieba.cut_for_search(str(row["memory"])))
                    conn.execute(
                        "UPDATE memories SET tokenized_memory = ? WHERE id = ?",
                        (tokens, int(row["id"])),
                    )
                memories_rebuild = True

            conn.execute(_DDL_MEMORY_FTS)
            conn.execute(_DDL_TRIGGER_AI)
            conn.execute(_DDL_TRIGGER_AD)
            conn.execute(_DDL_TRIGGER_AU)

            # 原始中文文本 FTS：trigram（或 unicode61 回退）
            episodes_rebuild = self._fts_schema_stale(
                conn, "memory_episodes_fts", text_tokenizer, ("canonical_user_id",)
            )
            if episodes_rebuild:
                self._drop_fts(conn, "memory_episodes_fts", episode_triggers)
            conn.execute(_memory_episodes_fts_ddl(text_tokenizer))
            for trigger in _MEMORY_EPISODES_FTS_TRIGGERS:
                conn.execute(trigger)

            profile_rebuild = self._fts_schema_stale(
                conn, "profile_items_fts", text_tokenizer, ("canonical_user_id",)
            )
            if profile_rebuild:
                self._drop_fts(conn, "profile_items_fts", profile_triggers)
            conn.execute(_profile_items_fts_ddl(text_tokenizer))
            for trigger in _PROFILE_ITEMS_FTS_TRIGGERS:
                conn.execute(trigger)

            if memories_rebuild:
                conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            if episodes_rebuild:
                conn.execute(
                    "INSERT INTO memory_episodes_fts(memory_episodes_fts) VALUES('rebuild')"
                )
            if profile_rebuild:
                conn.execute(
                    "INSERT INTO profile_items_fts(profile_items_fts) VALUES('rebuild')"
                )
            conn.execute("RELEASE SAVEPOINT tmem_fts_init")
            self._fts5_needs_rebuild = False
            logger.info(
                "[tmemory] FTS5 ready: memories=tokenized+jieba/%s, text=%s",
                _FTS_MEMORY_TOKENIZER,
                text_tokenizer,
            )
            return True
        except sqlite3.Error as e:
            # 原子回滚：绝不留下“空索引 + 触发器”的半成品。
            try:
                conn.execute("ROLLBACK TO SAVEPOINT tmem_fts_init")
                conn.execute("RELEASE SAVEPOINT tmem_fts_init")
            except sqlite3.Error:
                pass
            self._fts5_needs_rebuild = True
            logger.warning(
                "[tmemory] FTS5 初始化失败，中文检索降级为 LIKE（不影响启动）: %s", e
            )
            return False
