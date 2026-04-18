#!/usr/bin/env python3
"""
AstrBot tmemory 插件稳定性测试脚本

独立运行，无需 AstrBot 框架环境。
通过 mock 框架对象验证插件核心功能的正确性和稳定性。

用法:
    python test_plugin_stability.py
"""

import asyncio
import hashlib
import importlib
import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# ─── jieba 静默加载 ──────────────────────────────────────────────────────────
import jieba

jieba.setLogLevel(jieba.logging.WARNING)

# ─── 颜色输出 ─────────────────────────────────────────────────────────────────
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def _ok(msg: str):
    print(f"  {_GREEN}✓{_RESET} {msg}")


def _fail(msg: str):
    print(f"  {_RED}✗{_RESET} {msg}")


def _info(msg: str):
    print(f"  {_CYAN}ℹ{_RESET} {msg}")


def _section(title: str):
    print(f"\n{_BOLD}{_YELLOW}{'─' * 60}{_RESET}")
    print(f"{_BOLD}{_YELLOW}  {title}{_RESET}")
    print(f"{_BOLD}{_YELLOW}{'─' * 60}{_RESET}")


# ─── Mock AstrBot 框架 ───────────────────────────────────────────────────────

def _install_astrbot_mocks():
    """在 sys.modules 中注入 AstrBot 框架的 mock，使插件能被 import。"""

    # astrbot.api.logger
    import logging
    mock_logger = logging.getLogger("tmemory_test")
    mock_logger.setLevel(logging.WARNING)

    # 构建 mock 模块层级
    astrbot = MagicMock()
    astrbot.api = MagicMock()
    astrbot.api.logger = mock_logger

    # Event
    class FakeEventMessageType:
        ALL = "ALL"

    class FakeFilter:
        EventMessageType = FakeEventMessageType

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def command(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def on_llm_request(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def on_llm_response(*args, **kwargs):
            return lambda fn: fn

    astrbot.api.event = MagicMock()
    astrbot.api.event.filter = FakeFilter()
    astrbot.api.event.AstrMessageEvent = MagicMock

    # Provider
    astrbot.api.provider = MagicMock()
    astrbot.api.provider.LLMResponse = MagicMock
    astrbot.api.provider.ProviderRequest = MagicMock

    # Star
    class FakeStar:
        def __init__(self, context):
            self.context = context

    def fake_register(*args, **kwargs):
        return lambda cls: cls

    astrbot.api.star = MagicMock()
    astrbot.api.star.Star = FakeStar
    astrbot.api.star.Context = MagicMock
    astrbot.api.star.register = fake_register

    # 注入所有路径
    modules = {
        "astrbot": astrbot,
        "astrbot.api": astrbot.api,
        "astrbot.api.logger": mock_logger,
        "astrbot.api.event": astrbot.api.event,
        "astrbot.api.event.filter": astrbot.api.event.filter,
        "astrbot.api.provider": astrbot.api.provider,
        "astrbot.api.star": astrbot.api.star,
    }
    # 注入 filter 模块级属性，支持 from astrbot.api.event import filter
    filter_module = MagicMock()
    filter_module.event_message_type = FakeFilter.event_message_type
    filter_module.command = FakeFilter.command
    filter_module.permission_type = FakeFilter.permission_type
    filter_module.on_llm_request = FakeFilter.on_llm_request
    filter_module.on_llm_response = FakeFilter.on_llm_response
    filter_module.EventMessageType = FakeEventMessageType
    modules["astrbot.api.event.filter"] = filter_module
    astrbot.api.event.filter = filter_module

    sys.modules.update(modules)


def _create_mock_context() -> MagicMock:
    """创建一个 mock Context 对象。"""
    ctx = MagicMock()
    ctx.get_current_chat_provider_id = MagicMock(return_value="mock_provider")

    # llm_generate 返回一个带 completion_text 的 mock
    async def mock_llm_generate(**kwargs):
        resp = MagicMock()
        resp.completion_text = '[]'
        return resp

    ctx.llm_generate = AsyncMock(side_effect=mock_llm_generate)
    return ctx


def _create_plugin(db_dir: str, extra_config: Optional[Dict] = None) -> Any:
    """创建插件实例，使用临时 DB 路径。"""
    config = {
        "enable_auto_capture": True,
        "enable_memory_injection": True,
        "cache_max_rows": 20,
        "memory_max_chars": 220,
        "distill_interval_sec": 17280,
        "distill_min_batch_count": 2,
        "distill_batch_limit": 80,
        "enable_vector_search": False,
        **(extra_config or {}),
    }
    ctx = _create_mock_context()

    # Patch _load_web_server_class 和 _resolve_db_path
    from main import TMemoryPlugin

    with patch.object(TMemoryPlugin, "_load_web_server_class") as mock_ws:
        # mock web server
        ws_instance = MagicMock()
        ws_instance.start = AsyncMock()
        ws_instance.stop = AsyncMock()
        mock_ws.return_value = lambda plugin, cfg: ws_instance

        with patch.object(TMemoryPlugin, "_resolve_db_path",
                          return_value=os.path.join(db_dir, "tmemory.db")):
            plugin = TMemoryPlugin(ctx, config)

    plugin._web_server = ws_instance
    return plugin


# =============================================================================
# 测试用例
# =============================================================================

class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: List[str] = []

    def check(self, condition: bool, desc: str):
        if condition:
            self.passed += 1
            _ok(desc)
        else:
            self.failed += 1
            self.errors.append(desc)
            _fail(desc)

    def summary(self):
        total = self.passed + self.failed
        _section("测试结果汇总")
        print(f"  总计: {total}  通过: {_GREEN}{self.passed}{_RESET}  失败: {_RED}{self.failed}{_RESET}")
        if self.errors:
            print(f"\n  {_RED}失败项:{_RESET}")
            for e in self.errors:
                print(f"    - {e}")
        return self.failed == 0


results = TestResults()


# ─── 1. DB 连接管理测试 ──────────────────────────────────────────────────────

def test_db_connection_management():
    _section("1. DB 连接管理测试")

    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        plugin = _create_plugin(tmpdir)
        plugin._init_db()

        # 1.1 持久连接复用验证
        _info("持久连接复用验证...")
        conn1 = plugin._db()
        conn2 = plugin._db()
        results.check(conn1 is conn2, "多次 _db() 返回同一个持久连接对象")

        # 1.2 连接在 100 次操作后仍然有效
        _info("100 次操作后连接仍有效...")
        ok_count = 0
        for i in range(100):
            with plugin._db() as conn:
                result = conn.execute("SELECT 1").fetchone()
                assert result[0] == 1
            ok_count += 1
        results.check(ok_count == 100, "100 次 with _db() as conn 操作均成功")

        # 1.3 _close_db 后重新创建连接
        _info("_close_db 后重新创建连接...")
        plugin._close_db()
        # 关闭后再调用 _db() 应自动创建新连接
        conn_new = plugin._db()
        result = conn_new.execute("SELECT 1").fetchone()
        results.check(result[0] == 1, "_close_db 后 _db() 自动重建连接")
        results.check(conn_new is not conn1, "重建的连接是新对象")

        # 1.4 并发读写测试（线程安全）
        _info("并发读写测试（10 线程）...")
        errors_in_concurrent = []

        def concurrent_write(thread_id: int):
            try:
                for j in range(10):
                    with plugin._db() as conn:
                        now = time.strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute(
                            "INSERT INTO conversation_cache "
                            "(canonical_user_id, role, content, source_adapter, "
                            "source_user_id, unified_msg_origin, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (f"user_{thread_id}", "user", f"msg_{j}",
                             "test", "u1", "", now),
                        )
            except Exception as e:
                errors_in_concurrent.append(f"thread {thread_id}: {e}")

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(concurrent_write, i) for i in range(10)]
            for f in futures:
                f.result()

        results.check(
            len(errors_in_concurrent) == 0,
            f"并发写入无错误 (10 线程 × 10 次)"
            + (f" [错误: {errors_in_concurrent}]" if errors_in_concurrent else ""),
        )

        # 验证并发写入的数据完整性
        with plugin._db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM conversation_cache").fetchone()[0]
        results.check(count == 100, f"并发写入数据完整 (期望 100, 实际 {count})")


# ─── 2. 插件初始化容错测试 ───────────────────────────────────────────────────

def test_init_fault_tolerance():
    _section("2. 插件初始化容错测试")

    from main import TMemoryPlugin

    # 2.1 模拟配置缺失 — 使用空 config
    _info("模拟空配置初始化...")
    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        try:
            plugin = _create_plugin(tmpdir, extra_config={})
            plugin._init_db()
            results.check(True, "空配置初始化成功")
        except Exception as e:
            results.check(False, f"空配置初始化失败: {e}")

    # 2.2 模拟配置类型错误 — 传入非法类型值
    _info("模拟配置类型错误...")
    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        try:
            plugin = _create_plugin(tmpdir, extra_config={
                "cache_max_rows": "not_a_number_but_int_handles",
                "distill_interval_sec": -999,
                "memory_scope": "invalid_scope",
                "inject_position": "nonexistent",
                "capture_skip_regex": "[invalid(regex",
            })
            # 检查 fallback 生效
            results.check(
                plugin.memory_scope == "user",
                f"非法 memory_scope 回退到 'user' (实际: '{plugin.memory_scope}')",
            )
            results.check(
                plugin.inject_position == "system_prompt",
                f"非法 inject_position 回退到 'system_prompt' (实际: '{plugin.inject_position}')",
            )
        except Exception as e:
            results.check(False, f"配置类型错误导致崩溃: {e}")

    # 2.3 模拟 web_server.py 加载失败
    _info("模拟 WebUI 加载失败...")
    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        ctx = _create_mock_context()
        config = {"enable_auto_capture": True}

        try:
            with patch.object(TMemoryPlugin, "_load_web_server_class",
                              side_effect=ImportError("web_server.py not found")):
                with patch.object(TMemoryPlugin, "_resolve_db_path",
                                  return_value=os.path.join(tmpdir, "tmemory.db")):
                    plugin = TMemoryPlugin(ctx, config)
            # 如果不崩溃就是好事（但实际当前代码可能崩溃）
            results.check(True, "WebUI 加载失败但插件仍可初始化")
        except ImportError:
            results.check(False, "WebUI 加载失败导致整个插件不可用 (已知问题, __init__ 无容错)")
        except Exception as e:
            results.check(False, f"WebUI 加载失败导致意外错误: {type(e).__name__}: {e}")

    # 2.4 验证核心功能在 WebUI 失败后仍可用
    _info("验证核心功能不受 WebUI 影响...")
    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        plugin = _create_plugin(tmpdir)
        plugin._init_db()
        try:
            # 基本 DB 操作
            conn = plugin._db()
            conn.execute(
                "INSERT INTO conversation_cache "
                "(canonical_user_id, role, content, source_adapter, "
                "source_user_id, unified_msg_origin, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("test_user", "user", "hello", "test", "u1", "", plugin._now()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT COUNT(*) FROM conversation_cache WHERE canonical_user_id='test_user'"
            ).fetchone()
            conn.close()
            results.check(row[0] == 1, "核心 DB 操作正常 (WebUI mock 场景)")
        except Exception as e:
            results.check(False, f"核心 DB 操作失败: {e}")


# ─── 3. 记忆存储/检索测试 ────────────────────────────────────────────────────

def test_memory_crud():
    _section("3. 记忆存储/检索测试")

    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        plugin = _create_plugin(tmpdir)
        plugin._init_db()
        # 注: 不调用 _migrate_schema()，它在空 DB 上是安全的但在测试中不需要

        canonical_id = "test_user_001"

        # 3.1 插入记忆
        _info("插入记忆...")
        mem_id = plugin._insert_memory(
            canonical_id=canonical_id,
            adapter="test_adapter",
            adapter_user="u1",
            memory="用户喜欢吃四川火锅，尤其是麻辣口味",
            score=0.8,
            memory_type="preference",
            importance=0.7,
            confidence=0.9,
        )
        results.check(isinstance(mem_id, int) and mem_id > 0, f"记忆插入成功 (id={mem_id})")

        # 3.2 插入重复记忆 — 应该更新而非新增
        # 注: 当前 FTS5 UPDATE 触发器存在已知 bug — UPDATE 触发器中的
        # DELETE 操作要求 FTS5 索引中存在精确匹配的旧值，但跨连接场景下
        # 可能无法匹配，导致 "SQL logic error"。这是需要修复的真实问题。
        _info("插入重复记忆（去重验证）...")
        try:
            dup_id = plugin._insert_memory(
                canonical_id=canonical_id,
                adapter="test_adapter",
                adapter_user="u1",
                memory="用户喜欢吃四川火锅，尤其是麻辣口味",
                score=0.9,
                memory_type="preference",
                importance=0.8,
                confidence=0.95,
            )
            results.check(dup_id == mem_id, f"重复记忆去重成功 (返回相同 id={dup_id})")
        except sqlite3.OperationalError as e:
            if "SQL logic error" in str(e):
                results.check(False, f"重复记忆更新触发 FTS5 错误 (已知 bug): {e}")
            else:
                raise

        # 3.3 插入更多记忆用于检索测试
        test_memories = [
            ("用户是一名 Python 开发者，工作经验 5 年", "fact"),
            ("用户不喜欢加班，每天下班后跑步健身", "preference"),
            ("用户要求回答简洁直接，不要啰嗦", "style"),
            ("用户养了一只猫，名叫小橘", "fact"),
            ("用户正在学习 Rust 编程语言", "fact"),
        ]
        for mem_text, mem_type in test_memories:
            plugin._insert_memory(
                canonical_id=canonical_id,
                adapter="test_adapter",
                adapter_user="u1",
                memory=mem_text,
                score=0.7,
                memory_type=mem_type,
                importance=0.6,
                confidence=0.8,
            )

        # 3.4 FTS5 全文检索
        _info("FTS5 全文检索...")
        with plugin._db() as conn:
            try:
                query_tokens = [t for t in jieba.cut_for_search("Python 开发者") if len(t.strip()) >= 2]
                fts_query = " OR ".join(f'"{t}"' for t in query_tokens)
                rows = conn.execute(
                    "SELECT rowid, rank FROM memories_fts WHERE memories_fts MATCH ?",
                    (fts_query,),
                ).fetchall()
                results.check(len(rows) > 0, f"FTS5 检索 'Python 开发者' 命中 {len(rows)} 条")
            except Exception as e:
                results.check(False, f"FTS5 检索失败: {e}")

            # 3.5 通过关键词检索
            _info("关键词检索...")
            try:
                cat_tokens = [t for t in jieba.cut_for_search("小橘 猫") if len(t.strip()) >= 2]
                cat_query = " OR ".join(f'"{t}"' for t in cat_tokens)
                rows = conn.execute(
                    "SELECT rowid, rank FROM memories_fts WHERE memories_fts MATCH ?",
                    (cat_query,),
                ).fetchall()
                results.check(len(rows) > 0, f"FTS5 检索 '小橘 猫' 命中 {len(rows)} 条")
            except Exception as e:
                results.check(False, f"关键词检索失败: {e}")

            # 3.6 验证 memories 表数据一致性
            _info("数据一致性验证...")
            total = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE canonical_user_id=? AND is_active=1",
                (canonical_id,),
            ).fetchone()[0]
            fts_total = conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE canonical_user_id=?",
                (canonical_id,),
            ).fetchone()[0]
            results.check(
                total == fts_total,
                f"memories ({total}) 与 memories_fts ({fts_total}) 行数一致",
            )

            # 3.7 删除记忆
            _info("删除记忆后验证 FTS5 同步...")
            try:
                conn.execute("DELETE FROM memories WHERE id=?", (mem_id,))
                # with block 会自动 commit
                delete_ok = True
            except sqlite3.OperationalError as e:
                if "SQL logic error" in str(e):
                    results.check(False, f"删除触发 FTS5 错误 (已知 bug): {e}")
                    delete_ok = False
                else:
                    raise

        if delete_ok:
            with plugin._db() as conn:
                fts_after_del = conn.execute(
                    "SELECT COUNT(*) FROM memories_fts WHERE canonical_user_id=?",
                    (canonical_id,),
                ).fetchone()[0]
                active_after_del = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE canonical_user_id=? AND is_active=1",
                    (canonical_id,),
                ).fetchone()[0]
                results.check(
                    active_after_del == fts_after_del,
                    f"删除后 memories ({active_after_del}) 与 FTS5 ({fts_after_del}) 同步",
                )


# ─── 4. 蒸馏流程测试 ─────────────────────────────────────────────────────────

def test_distill_pipeline():
    _section("4. 蒸馏流程测试")

    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        plugin = _create_plugin(tmpdir, extra_config={
            "distill_min_batch_count": 2,
            "distill_batch_limit": 80,
        })
        plugin._init_db()

        canonical_id = "distill_user_001"

        # 4.1 插入对话缓存
        _info("插入对话缓存...")
        messages = [
            ("user", "我最近在学 Rust，感觉很有意思"),
            ("assistant", "Rust 是一门很好的系统编程语言，你可以从 The Book 开始学习"),
            ("user", "我之前一直用 Python，Rust 的所有权系统让我很不习惯"),
            ("assistant", "所有权系统是 Rust 的核心特性，需要时间适应"),
            ("user", "对了，我明天要去北京出差"),
            ("assistant", "祝你出差顺利！北京最近天气不错"),
        ]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with plugin._db() as conn:
            for role, content in messages:
                conn.execute(
                    "INSERT INTO conversation_cache "
                    "(canonical_user_id, role, content, source_adapter, "
                    "source_user_id, unified_msg_origin, distilled, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (canonical_id, role, content, "test", "u1", "", now),
                )

        # 验证缓存已写入
        with plugin._db() as conn:
            cache_count = conn.execute(
                "SELECT COUNT(*) FROM conversation_cache WHERE canonical_user_id=? AND distilled=0",
                (canonical_id,),
            ).fetchone()[0]
        results.check(cache_count == 6, f"对话缓存写入 {cache_count} 条 (期望 6)")

        # 4.2 获取待蒸馏用户
        _info("检查待蒸馏用户...")
        pending = plugin._pending_distill_users(limit=10, min_batch_count=2)
        results.check(
            canonical_id in pending,
            f"待蒸馏用户包含 '{canonical_id}' (列表: {pending})",
        )

        # 4.3 获取待蒸馏行
        _info("获取待蒸馏行...")
        rows = plugin._fetch_pending_rows(canonical_id, limit=80)
        results.check(len(rows) == 6, f"待蒸馏行数: {len(rows)} (期望 6)")

        # 4.4 触发蒸馏（使用规则蒸馏 fallback，因为没有真实 LLM）
        _info("触发蒸馏流程...")

        # Mock llm_generate 返回结构化蒸馏结果
        async def mock_distill_llm(**kwargs):
            import json
            resp = MagicMock()
            resp.completion_text = json.dumps([
                {
                    "memory": "用户正在学习 Rust 编程语言，之前使用 Python",
                    "memory_type": "fact",
                    "importance": 0.7,
                    "confidence": 0.8,
                    "score": 0.75,
                },
                {
                    "memory": "用户计划去北京出差",
                    "memory_type": "fact",
                    "importance": 0.5,
                    "confidence": 0.9,
                    "score": 0.65,
                },
            ])
            return resp

        plugin.context.llm_generate = AsyncMock(side_effect=mock_distill_llm)

        try:
            users, memories = asyncio.get_event_loop().run_until_complete(
                plugin._run_distill_cycle(force=True, trigger="test")
            )
            results.check(users >= 0, f"蒸馏完成: 处理 {users} 用户, 产出 {memories} 条记忆")
        except Exception as e:
            # 如果 LLM 调用链路上出问题，验证规则蒸馏 fallback
            results.check(False, f"蒸馏流程异常: {type(e).__name__}: {e}")

        # 4.5 验证对话已标记为已蒸馏
        _info("验证蒸馏标记...")
        with plugin._db() as conn:
            undistilled = conn.execute(
                "SELECT COUNT(*) FROM conversation_cache WHERE canonical_user_id=? AND distilled=0",
                (canonical_id,),
            ).fetchone()[0]
            distilled = conn.execute(
                "SELECT COUNT(*) FROM conversation_cache WHERE canonical_user_id=? AND distilled=1",
                (canonical_id,),
            ).fetchone()[0]
        results.check(
            distilled > 0 or undistilled < cache_count,
            f"蒸馏标记更新: 未蒸馏 {undistilled}, 已蒸馏 {distilled}",
        )

        # 4.6 验证蒸馏历史记录
        _info("验证蒸馏历史...")
        with plugin._db() as conn:
            history = conn.execute(
                "SELECT * FROM distill_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
        results.check(
            history is not None,
            f"蒸馏历史已记录"
            + (f" (耗时 {history['duration_sec']}s)" if history else ""),
        )


# ─── 5. 性能基准测试 ─────────────────────────────────────────────────────────

def test_performance_benchmark():
    _section("5. 性能基准测试")

    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        plugin = _create_plugin(tmpdir)
        plugin._init_db()

        canonical_id = "perf_user_001"

        # 5.1 批量记忆插入
        _info("100 次记忆插入基准...")
        t0 = time.perf_counter()
        insert_ok = 0
        insert_err = 0
        for i in range(100):
            try:
                plugin._insert_memory(
                    canonical_id=canonical_id,
                    adapter="test",
                    adapter_user="u1",
                    memory=f"性能测试记忆条目第 {i} 条，包含唯一关键词 keyword_{i} 和内容描述",
                    score=0.5 + (i % 5) * 0.1,
                    memory_type=["fact", "preference", "style"][i % 3],
                    importance=0.5,
                    confidence=0.7,
                )
                insert_ok += 1
            except sqlite3.OperationalError:
                insert_err += 1
        insert_elapsed = time.perf_counter() - t0
        insert_ops = insert_ok / max(insert_elapsed, 0.001)
        results.check(
            insert_ok > 0,
            f"记忆插入: {insert_ok} 成功, {insert_err} 失败, "
            f"耗时 {insert_elapsed:.3f}s ({insert_ops:.1f} ops/sec)"
            + (f" [FTS5 冲突导致 {insert_err} 条失败]" if insert_err else ""),
        )

        # 5.2 批量 FTS5 检索
        _info("100 次 FTS5 检索基准...")
        queries = [f"关键词 keyword_{i}" for i in range(100)]
        t0 = time.perf_counter()
        hit_count = 0
        for q in queries:
            with plugin._db() as conn:
                try:
                    tokens = list(jieba.cut_for_search(q))
                    fts_q = " AND ".join(tokens)
                    if fts_q:
                        rows = conn.execute(
                            "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? LIMIT 10",
                            (fts_q,),
                        ).fetchall()
                        hit_count += len(rows)
                except Exception:
                    pass
        search_elapsed = time.perf_counter() - t0
        search_ops = 100 / search_elapsed
        results.check(
            True,
            f"100 次检索耗时 {search_elapsed:.3f}s ({search_ops:.1f} ops/sec, 总命中 {hit_count})",
        )

        # 5.3 对话缓存批量写入
        _info("100 次对话缓存写入基准...")
        t0 = time.perf_counter()
        now = plugin._now()
        with plugin._db() as conn:
            for i in range(100):
                conn.execute(
                    "INSERT INTO conversation_cache "
                    "(canonical_user_id, role, content, source_adapter, "
                    "source_user_id, unified_msg_origin, distilled, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (canonical_id, "user", f"对话消息 {i}", "test", "u1", "", now),
                )
        cache_elapsed = time.perf_counter() - t0
        cache_ops = 100 / cache_elapsed
        results.check(True, f"100 次缓存写入耗时 {cache_elapsed:.3f}s ({cache_ops:.1f} ops/sec)")

        # 5.4 性能汇总
        print(f"\n  {_CYAN}性能汇总:{_RESET}")
        print(f"    记忆插入: {_BOLD}{insert_ops:.1f}{_RESET} ops/sec")
        print(f"    FTS5 检索: {_BOLD}{search_ops:.1f}{_RESET} ops/sec")
        print(f"    缓存写入: {_BOLD}{cache_ops:.1f}{_RESET} ops/sec")


# ─── 6. 工具方法测试 ─────────────────────────────────────────────────────────

def test_utility_methods():
    _section("6. 工具方法测试")

    with tempfile.TemporaryDirectory(prefix="tmemory_test_") as tmpdir:
        plugin = _create_plugin(tmpdir)

        # 6.1 _normalize_text
        results.check(
            plugin._normalize_text("  hello   world  ") == "hello world",
            "_normalize_text 去除多余空白",
        )
        results.check(
            plugin._normalize_text(None) == "",
            "_normalize_text 处理 None",
        )

        # 6.2 _clamp01
        results.check(plugin._clamp01(1.5) == 1.0, "_clamp01(1.5) == 1.0")
        results.check(plugin._clamp01(-0.5) == 0.0, "_clamp01(-0.5) == 0.0")
        results.check(plugin._clamp01("abc") == 0.0, "_clamp01('abc') == 0.0")

        # 6.3 _safe_memory_type
        results.check(
            plugin._safe_memory_type("preference") == "preference",
            "_safe_memory_type('preference') 有效",
        )
        results.check(
            plugin._safe_memory_type("unknown_type") in {"fact", "preference", "task", "restriction", "style"},
            "_safe_memory_type('unknown_type') fallback 到有效类型",
        )

        # 6.4 _should_skip_capture
        results.check(
            plugin._should_skip_capture("\x00[astrbot:no-memory]\x00test"),
            "_should_skip_capture 识别协议标记",
        )
        results.check(
            plugin._should_skip_capture("提醒 #123 test"),
            "_should_skip_capture 识别前缀",
        )
        results.check(
            not plugin._should_skip_capture("普通消息"),
            "_should_skip_capture 不误判普通消息",
        )

        # 6.5 _sanitize_text
        results.check(
            "[手机号]" in plugin._sanitize_text("我的手机号是13812345678"),
            "_sanitize_text 脱敏手机号",
        )
        results.check(
            "[邮箱]" in plugin._sanitize_text("联系邮箱 test@example.com"),
            "_sanitize_text 脱敏邮箱",
        )

        # 6.6 _now 格式验证
        now_str = plugin._now()
        results.check(
            re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", now_str) is not None,
            f"_now() 格式正确: {now_str}",
        )


# =============================================================================
# 主入口
# =============================================================================

def main():
    print(f"\n{_BOLD}{_CYAN}╔══════════════════════════════════════════════════════════╗{_RESET}")
    print(f"{_BOLD}{_CYAN}║    AstrBot tmemory 插件稳定性测试                       ║{_RESET}")
    print(f"{_BOLD}{_CYAN}╚══════════════════════════════════════════════════════════╝{_RESET}")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python: {sys.version.split()[0]}")

    # 确保 import 路径正确
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    # 安装 mock
    _install_astrbot_mocks()

    # 处理 main.py 的相对导入：先把 hybrid_search 作为独立模块导入，
    # 然后 patch main.py 中的 `from .hybrid_search` 为绝对导入
    import importlib
    import hybrid_search as _hs_module
    # 创建一个假的 package，使 main.py 的相对导入能解析
    # 方法：将 main.py 所在目录注册为包
    _fake_pkg_name = "astrbot_plugin_tmemory"
    _fake_pkg = MagicMock()
    _fake_pkg.__path__ = [plugin_dir]
    _fake_pkg.__file__ = os.path.join(plugin_dir, "__init__.py")
    _fake_pkg.__name__ = _fake_pkg_name
    _fake_pkg.__package__ = _fake_pkg_name
    _fake_pkg.hybrid_search = _hs_module
    sys.modules[_fake_pkg_name] = _fake_pkg
    sys.modules[f"{_fake_pkg_name}.hybrid_search"] = _hs_module

    # 以包内模块方式加载 main.py
    spec = importlib.util.spec_from_file_location(
        f"{_fake_pkg_name}.main",
        os.path.join(plugin_dir, "main.py"),
        submodule_search_locations=[],
    )
    main_module = importlib.util.module_from_spec(spec)
    main_module.__package__ = _fake_pkg_name
    sys.modules[f"{_fake_pkg_name}.main"] = main_module
    sys.modules["main"] = main_module
    spec.loader.exec_module(main_module)

    # 运行测试
    tests = [
        ("DB 连接管理", test_db_connection_management),
        ("初始化容错", test_init_fault_tolerance),
        ("记忆 CRUD", test_memory_crud),
        ("蒸馏流程", test_distill_pipeline),
        ("性能基准", test_performance_benchmark),
        ("工具方法", test_utility_methods),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            results.check(False, f"{name} 测试异常: {type(e).__name__}: {e}")
            traceback.print_exc()

    # 结果汇总
    ok = results.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
