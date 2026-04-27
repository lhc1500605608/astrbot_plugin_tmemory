import hashlib
import importlib.util
import jieba
import json
import os
import re
import sqlite3
import threading
import time
import asyncio
from contextlib import contextmanager
from typing import Optional, List, Dict, Tuple, Sequence
from collections import Counter

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from .hybrid_search import HybridMemorySystem


class _NullWebServer:
    """WebUI 降级替身(NullObject 模式)。

    当 web_server.py 加载失败时作为 self._web_server 的替代，
    保证所有对 self._web_server.start() / .stop() 的调用安全无操作。
    """

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


from .core.db import DatabaseManager, _LockedConnection
from .core.config import PluginConfig, parse_config
from .core.capture import CaptureFilter
from .core.distill import DistillManager
from .core.utils import MemoryLogger
from .core.identity import IdentityManager
from .core.style_manager import StyleManager
from .search.retrieval import RetrievalManager

@register(
    "tmemory",
    "shangtang",
    "AstrBot 用户长期记忆插件(自动采集 + 定时LLM蒸馏 + 跨适配器合并)",
    "0.4.0",
)
class TMemoryPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_name = "astrbot_plugin_tmemory"
        self.db_path = self._resolve_db_path()

        # ── DB Manager ─────────────────────────────────────────────────────
        self._db_mgr = DatabaseManager(self.db_path)

        # ── 配置解析 ─────────────────────────────────────────────────────
        try:
            self._cfg = parse_config(self.config)
        except Exception as e:
            logger.warning("[tmemory] 配置解析部分失败，使用安全默认值: %s", e)
            self._cfg = PluginConfig()

        # ── 其他运行时状态 ─────────────────────────────────────────────────────
        self._vector_manager: Optional["VectorManager"] = None
        self._sqlite_vec = None
        self._vec_available = False
        self._sanitize_patterns = []
        self._distill_task: Optional[asyncio.Task] = None
        self._worker_running = False
        self._merge_needs_vector_rebuild = False
        self._fts5_needs_rebuild = False
        self._last_purify_ts = 0.0
        self._embed_ok_count = 0
        self._embed_fail_count = 0
        self._embed_last_error = ""
        self._vec_query_count = 0
        self._vec_hit_count = 0
        self._embed_semaphore = asyncio.Semaphore(4)
        self._http_session = None
        self._distill_skipped_rows: int = 0
        self._user_last_distilled_ts: Dict[str, float] = {}

        # ── CaptureFilter & DistillManager ──────────────────────────────────────────────────────
        self._capture_filter = CaptureFilter(self._cfg)
        self._distill_mgr = DistillManager(self._cfg)
        self._retrieval_mgr = RetrievalManager(self._cfg, self._db_mgr)
        self._memory_logger = MemoryLogger(self._db_mgr)
        self._identity_mgr = IdentityManager(self._db_mgr, self._cfg, self._memory_logger)
        self._style_mgr = StyleManager(self._db_mgr)

        # ── 敏感信息脱敏 ──────────────────────────────────────────────────────
        self._sanitize_patterns = self._build_sanitize_patterns()

        # ── WebUI 独立服务器(降级保护)────────────────────────────────────
        self._web_server = self._safe_load_web_server()

    def _set_safe_defaults(self):
        """设置所有配置属性的安全默认值，确保任何配置解析失败都不会导致 AttributeError。
        
        实现细节见 core.config.apply_safe_defaults。
        """
        from .core.config import apply_safe_defaults
        apply_safe_defaults(self)

    def _get_vector_retrieval_config(self) -> Dict:
        """兼容旧平铺配置和新嵌套配置的向量检索配置读取。"""
        vector_cfg = self.config.get("vector_retrieval", {})
        if not isinstance(vector_cfg, dict):
            vector_cfg = {}

        merged = dict(vector_cfg)
        legacy_keys = (
            "enable_vector_search",
            "embedding_provider",
            "embedding_api_key",
            "embedding_model",
            "embedding_base_url",
            "vector_dim",
            "auto_rebuild_on_dim_change",
        )
        for key in legacy_keys:
            if key not in merged and key in self.config:
                merged[key] = self.config.get(key)
        return merged

    def _get_vector_retrieval_config_from_cfg(self) -> Dict:
        """从 self._cfg 返回 vector_retrieval 字典用于传递给 VectorManager"""
        return {
            "enable_vector_search": self._cfg.enable_vector_search,
            "embedding_provider": self._cfg.embed_provider_id,
            "embedding_api_key": self._cfg.embed_api_key,
            "embedding_model": self._cfg.embed_model_id,
            "embedding_base_url": self._cfg.embed_base_url,
            "vector_dim": self._cfg.embed_dim,
        }

    def _safe_load_web_server(self):
        """安全加载 WebUI 服务器，失败时降级为 _NullWebServer。"""
        try:
            TMemoryWebServer = self._load_web_server_class()
            webui_cfg = dict(self.config)
            webui_sub = self.config.get("webui_settings", {})
            if isinstance(webui_sub, dict):
                webui_cfg.update(webui_sub)
            return TMemoryWebServer(self, webui_cfg)
        except Exception as e:
            logger.warning(
                "[tmemory] WebUI 加载失败，核心功能不受影响: %s", e
            )
            return _NullWebServer()

    def _load_web_server_class(self):
        """通过文件路径动态加载 web_server.py，避免 `No module named 'web_server'`。"""
        web_server_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "web_server.py"
        )
        if not os.path.exists(web_server_path):
            raise ImportError(f"web_server.py not found: {web_server_path}")

        module_prefix = __name__.rsplit(".", 1)[0] if "." in __name__ else self.plugin_name
        module_name = f"{module_prefix}.web_server"
        spec = importlib.util.spec_from_file_location(module_name, web_server_path)
        if spec is None or spec.loader is None:
            raise ImportError("failed to create module spec for web_server.py")

        module = importlib.util.module_from_spec(spec)
        sys_modules = __import__("sys").modules
        sys_modules[module_name] = module
        spec.loader.exec_module(module)

        cls = getattr(module, "TMemoryWebServer", None)
        if cls is None:
            raise ImportError("TMemoryWebServer not found in web_server.py")
        return cls

    # =========================================================================
    # AstrBot 生命周期
    # =========================================================================

    async def initialize(self):
        self._init_db()
        self._migrate_schema()

        # 初始化 VectorManager(如果向量检索启用)
        if self._cfg.enable_vector_search:
            try:
                from .vector_manager import VectorManager
                vr = self._get_vector_retrieval_config_from_cfg()
                self._vector_manager = VectorManager(self.db_path, vr)
                await self._vector_manager.initialize()
                logger.info("[tmemory] VectorManager initialized")
            except Exception as e:
                logger.error("[tmemory] Failed to initialize VectorManager: %s", e)
                self._vector_manager = None

        self._worker_running = True
        self._distill_task = asyncio.create_task(self._distill_worker_loop())

        # 启动独立 WebUI 服务器
        try:
            await self._web_server.start()
        except Exception as e:
            logger.warning("[tmemory] WebUI 启动失败，核心功能不受影响: %s", e)
            self._web_server = _NullWebServer()

        logger.info(
            "[tmemory] initialized, db=%s, auto_capture=%s, memory_injection=%s, distill_interval=%ss, memory_mode=%s",
            self.db_path,
            self._cfg.enable_auto_capture,
            self._cfg.enable_memory_injection,
            self._cfg.distill_interval_sec,
            self._cfg.memory_mode,
        )

    async def terminate(self):
        self._worker_running = False
        if self._distill_task and not self._distill_task.done():
            self._distill_task.cancel()
            try:
                await self._distill_task
            except asyncio.CancelledError:
                pass
        # 关闭 VectorManager
        if self._vector_manager:
            try:
                await self._vector_manager.close()
                logger.info("[tmemory] VectorManager closed")
            except Exception as e:
                logger.warning("[tmemory] VectorManager close exception: %s", e)
        # 关闭 WebUI 服务器
        try:
            await self._web_server.stop()
        except Exception as e:
            logger.warning("[tmemory] WebUI 关闭异常: %s", e)
        # 关闭 aiohttp session（向量检索启用时防止连接泄漏）
        if self._http_session and not self._http_session.closed:
            try:
                await self._http_session.close()
            except Exception as e:
                logger.warning("[tmemory] http_session 关闭异常: %s", e)
        # 关闭持久 DB 连接
        self._close_db()
        logger.info("[tmemory] terminated")

    # =========================================================================
    # 消息采集 Hooks
    # =========================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """自动采集每条用户消息，仅写入 conversation_cache，不实时蒸馏。"""
        if not (self._cfg.enable_auto_capture or self._cfg.enable_style_distill):
            return

        text = self._normalize_text(getattr(event, "message_str", "") or "")
        if not text:
            return

        # 跳过插件指令，避免把控制命令当作记忆素材。
        if text.startswith("/"):
            return

        # 三层过滤:协议标记 → 前缀 → 正则
        if self._capture_filter.should_skip_capture(text):
            return

        canonical_id, adapter, adapter_user = self._identity_mgr.resolve_current_identity(event)
        umo = self._safe_get_unified_msg_origin(event)
        await self._insert_conversation(
            canonical_id=canonical_id,
            role="user",
            content=self._sanitize_text(text),
            source_adapter=adapter,
            source_user_id=adapter_user,
            unified_msg_origin=umo,
            scope=self._get_memory_scope(event),
            persona_id=self._get_current_persona(event),
        )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """可选采集模型回复，作为后续批量蒸馏素材。"""
        if not self._cfg.enable_auto_capture or not self._cfg.capture_assistant_reply:
            return

        text = self._normalize_text(getattr(resp, "completion_text", "") or "")
        if not text:
            return

        # 三层过滤:协议标记 → 前缀 → 正则
        if self._capture_filter.should_skip_capture(text):
            return

        try:
            canonical_id, adapter, adapter_user = self._identity_mgr.resolve_current_identity(event)
            umo = self._safe_get_unified_msg_origin(event)
            await self._insert_conversation(
                canonical_id=canonical_id,
                role="assistant",
                content=text,
                source_adapter=adapter,
                source_user_id=adapter_user,
                unified_msg_origin=umo,
            )
        except Exception as e:
            logger.warning("[tmemory] on_llm_response capture failed: %s", e)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 调用前注入记忆与风格补充。

        风格补充（人格档案 + 对话风格）始终追加到 system_prompt 尾部，
        不受 inject_position 配置影响。知识记忆按 inject_position 注入。
        """
        if not self._cfg.enable_memory_injection and not self._cfg.enable_style_injection:
            return

        try:
            canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
            query = self._normalize_text(getattr(req, "prompt", "") or "")
            scope = self._get_memory_scope(event)
            persona_id = await self._get_current_persona_async(event)
            is_group = self._is_group_event(event)
            exclude_private = (is_group and not self._cfg.private_memory_in_group)

            # ── 风格注入：始终追加到 system_prompt ──
            if self._cfg.enable_style_injection:
                style_block = await self._build_style_injection(
                    canonical_id, query, event,
                    scope=scope, persona_id=persona_id, exclude_private=exclude_private,
                )
                if style_block:
                    existing = getattr(req, "system_prompt", "") or ""
                    req.system_prompt = existing + ("\n\n" if existing else "") + style_block

            # ── 知识记忆注入：遵循 inject_position ──
            if self._cfg.enable_memory_injection:
                knowledge_block = await self._build_knowledge_injection(
                    canonical_id, query, self._cfg.inject_memory_limit,
                    scope=scope, persona_id=persona_id, exclude_private=exclude_private,
                )
                if knowledge_block:
                    self._inject_block_by_position(req, knowledge_block)
        except Exception as e:
            logger.warning("[tmemory] on_llm_request inject failed: %s", e)

    # =========================================================================
    # AI 主动工具模式: remember / recall
    # =========================================================================

    @filter.llm_tool(name="remember")
    async def tool_remember(
        self, event: AstrMessageEvent, content: str, memory_type: str
    ):
        """记住用户的重要信息。当对话中出现值得长期记住的用户偏好、事实、任务、限制或风格时，主动调用此工具保存。

        Args:
            content(string): 要记住的内容，用简洁的陈述句描述
            memory_type(string): 记忆类型，可选值：preference（偏好）、fact（事实）、task（任务）、restriction（限制）、style（风格）
        """
        if self._cfg.memory_mode == "distill_only":
            return "记忆工具当前已禁用（模式为 distill_only）。"

        content = self._normalize_text(content or "")
        if not content or len(content) < 4:
            return "内容过短，未保存。"

        memory_type = self._safe_memory_type(memory_type)

        # 安全审计
        if self._is_unsafe_memory(content):
            return "内容未通过安全审计，未保存。"
        if self._is_junk_memory(content):
            return "内容信息量过低，未保存。"

        canonical_id, adapter, adapter_user = self._identity_mgr.resolve_current_identity(event)
        scope = self._get_memory_scope(event)
        persona_id = self._get_current_persona(event)

        new_id = self._insert_memory(
            canonical_id=canonical_id,
            adapter=adapter,
            adapter_user=adapter_user,
            memory=self._sanitize_text(content),
            score=0.80,
            memory_type=memory_type,
            importance=0.70,
            confidence=0.85,
            source_channel="active_tool",
            persona_id=persona_id,
            scope=scope,
        )

        if self._vec_available and new_id:
            try:
                await self._upsert_vector(new_id, content)
            except Exception:
                pass  # 向量写入失败不影响核心流程

        return f"已记住（id={new_id}, type={memory_type}）。"

    @filter.llm_tool(name="recall")
    async def tool_recall(self, event: AstrMessageEvent, query: str):
        """检索与查询相关的用户记忆。当需要回忆用户的偏好、历史信息或之前提到的内容时调用。

        Args:
            query(string): 查询文本，描述想要回忆的内容
        """
        if self._cfg.memory_mode == "distill_only":
            return "记忆工具当前已禁用（模式为 distill_only）。"

        query = self._normalize_text(query or "")
        if not query:
            return "查询内容为空。"

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        scope = self._get_memory_scope(event)
        persona_id = self._get_current_persona(event)
        is_group = self._is_group_event(event)

        try:
            rows = await self._retrieve_memories(
                canonical_id,
                query,
                limit=self._cfg.inject_memory_limit,
                scope=scope,
                persona_id=persona_id,
                exclude_private=(is_group and not self._cfg.private_memory_in_group),
            )
        except Exception as e:
            logger.warning("[tmemory] recall tool retrieval failed: %s", e)
            return "检索记忆时出现错误。"

        if not rows:
            return "未找到相关记忆。"

        lines = []
        for row in rows:
            mtype = row["memory_type"]
            mem = row["memory"]
            lines.append(f"- ({mtype}) {mem}")
        return "\n".join(lines)

    # =========================================================================
    # 管理指令
    # =========================================================================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_distill_now")
    async def tm_distill_now(self, event: AstrMessageEvent):
        """手动触发一次批量蒸馏:/tm_distill_now"""
        processed_users, total_memories = await self._run_distill_cycle(
            force=True, trigger="manual_cmd"
        )
        yield event.plain_result(
            f"批量蒸馏完成:处理用户 {processed_users} 个，新增/更新记忆 {total_memories} 条。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_worker")
    async def tm_worker(self, event: AstrMessageEvent):
        """查看蒸馏 worker 状态:/tm_worker"""
        pending_users = self._count_pending_users()
        pending_rows = self._count_pending_rows()
        lines = [
            f"worker_running={self._worker_running}",
            f"distill_interval_sec={self._cfg.distill_interval_sec}",
            f"distill_min_batch_count={self._cfg.distill_min_batch_count}",
            f"distill_batch_limit={self._cfg.distill_batch_limit}",
            f"pending_users={pending_users}",
            f"pending_rows={pending_rows}",
            f"--- gate stats ---",
            f"capture_min_content_len={self._cfg.capture_min_content_len}",
            f"capture_dedup_window={self._cfg.capture_dedup_window}",
            f"distill_user_throttle_sec={self._cfg.distill_user_throttle_sec}",
            f"distill_skipped_rows(lifetime)={self._distill_skipped_rows}",
            f"throttled_users={sum(1 for ts in self._user_last_distilled_ts.values() if time.time() - ts < self._cfg.distill_user_throttle_sec) if self._cfg.distill_user_throttle_sec > 0 else 'N/A'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_memory")
    async def tm_memory(self, event: AstrMessageEvent):
        """查看当前用户的记忆:/tm_memory"""
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        memories = self._list_memories(canonical_id, limit=8)
        if not memories:
            yield event.plain_result("当前还没有已保存记忆。")
            return

        lines = [f"canonical_id={canonical_id}"]
        for row in memories:
            pin = "📌 " if row.get("is_pinned") else ""
            lines.append(
                f"[{row['id']}] {pin}[{row['memory_type']}] s={row['score']:.2f} i={row['importance']:.2f} c={row['confidence']:.2f} r={row['reinforce_count']} | {row['memory']}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_context")
    async def tm_context(self, event: AstrMessageEvent):
        """预览记忆召回上下文:/tm_context 今天吃什么"""
        raw = (event.message_str or "").strip()
        query = re.sub(r"^/tm_context\s*", "", raw, flags=re.IGNORECASE).strip()
        if not query:
            yield event.plain_result("用法: /tm_context <当前问题>")
            return

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        context_block = await self.build_memory_context(canonical_id, query, limit=6)
        yield event.plain_result(context_block)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_bind")
    async def tm_bind(self, event: AstrMessageEvent):
        """绑定当前账号到统一用户 ID:/tm_bind alice"""
        raw = (event.message_str or "").strip()
        canonical_id = re.sub(r"^/tm_bind\s*", "", raw, flags=re.IGNORECASE).strip()
        if not canonical_id:
            yield event.plain_result("用法: /tm_bind <统一用户ID>")
            return

        adapter = self._get_adapter_name(event)
        adapter_user = self._get_adapter_user_id(event)
        self._identity_mgr.bind_identity(adapter, adapter_user, canonical_id)
        yield event.plain_result(
            f"绑定成功:{adapter}:{adapter_user} -> {canonical_id}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_merge")
    async def tm_merge(self, event: AstrMessageEvent):
        """合并两个统一用户 ID 的记忆:/tm_merge old_id new_id"""
        raw = (event.message_str or "").strip()
        args = re.sub(r"^/tm_merge\s*", "", raw, flags=re.IGNORECASE).strip().split()
        if len(args) != 2:
            yield event.plain_result(
                "用法: /tm_merge <from_canonical_id> <to_canonical_id>"
            )
            return

        from_id, to_id = args[0], args[1]
        if from_id == to_id:
            yield event.plain_result("两个 ID 相同，无需合并。")
            return

        moved = self._identity_mgr.merge_identity(from_id, to_id)
        self._delete_vectors_for_user(from_id)
        self._merge_needs_vector_rebuild = True
        yield event.plain_result(
            f"合并完成:{from_id} -> {to_id}，迁移记忆 {moved} 条。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_forget")
    async def tm_forget(self, event: AstrMessageEvent):
        """删除一条记忆:/tm_forget 12"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_forget\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("用法: /tm_forget <记忆ID>")
            return

        deleted = self._delete_memory(int(arg))
        if deleted:
            yield event.plain_result(f"已删除记忆 {arg}")
            return
        yield event.plain_result(f"未找到记忆 {arg}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_stats")
    async def tm_stats(self, event: AstrMessageEvent):
        """查看全局统计信息:/tm_stats"""
        stats = self._get_global_stats()
        lines = [
            f"total_users: {stats['total_users']}",
            f"total_active_memories: {stats['total_active_memories']}",
            f"total_deactivated_memories: {stats['total_deactivated_memories']}",
            f"pending_cached_rows: {stats['pending_cached_rows']}",
            f"total_events: {stats['total_events']}",
        ]
        if self._vec_available:
            lines.append(f"vector_index_rows: {stats.get('vector_index_rows', 0)}")
            lines.append(
                f"embed_ok/fail: {self._embed_ok_count}/{self._embed_fail_count}"
            )
            hit_rate = (
                f"{self._vec_hit_count}/{self._vec_query_count}"
                f" ({self._vec_hit_count * 100 // max(1, self._vec_query_count)}%)"
                if self._vec_query_count > 0
                else "N/A"
            )
            lines.append(f"vector_hit_rate: {hit_rate}")
            if self._embed_last_error:
                lines.append(f"embed_last_error: {self._embed_last_error[:80]}")
        elif self._cfg.enable_vector_search:
            lines.append("vector_search: enabled but sqlite-vec not installed")

        # 最近 10 轮蒸馏 token 累计
        distill_cost = self._get_distill_cost_summary(last_n=10)
        lines.append("--- distill cost (last 10 runs) ---")
        if distill_cost["has_usage"]:
            lines.append(f"distill_runs: {distill_cost['runs']}")
            lines.append(f"distill_tokens_input: {distill_cost['tokens_input']}")
            lines.append(f"distill_tokens_output: {distill_cost['tokens_output']}")
            lines.append(f"distill_tokens_total: {distill_cost['tokens_total']}")
        else:
            lines.append(
                f"distill_runs: {distill_cost['runs']} (no usage data from provider)"
            )

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_distill_history")
    async def tm_distill_history(self, event: AstrMessageEvent):
        """查看最近蒸馏历史（含 token 成本）:/tm_distill_history"""
        rows = self._get_distill_history(limit=10)
        if not rows:
            yield event.plain_result("暂无蒸馏历史记录。")
            return

        lines = [f"最近 {len(rows)} 轮蒸馏历史（最新优先）:"]
        for r in rows:
            tok_in = r.get("tokens_input", -1)
            tok_out = r.get("tokens_output", -1)
            tok_total = r.get("tokens_total", -1)
            tok_str = (
                f"in={tok_in} out={tok_out} total={tok_total}"
                if tok_total >= 0
                else "tokens=N/A"
            )
            lines.append(
                f"[{r['id']}] {r['started_at'][:16]} trigger={r['trigger_type']}"
                f" users={r['users_processed']} mems={r['memories_created']}"
                f" failed={r['users_failed']} dur={r['duration_sec']:.1f}s"
                f" {tok_str}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_purify")
    async def tm_purify(self, event: AstrMessageEvent):
        """手动触发一次记忆提纯:/tm_purify"""
        yield event.plain_result("开始记忆提纯，请稍候…")
        pruned, kept = await self._run_memory_purify()
        yield event.plain_result(
            f"记忆提纯完成:失活低质量记忆 {pruned} 条，保留 {kept} 条。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_quality_refine")
    async def tm_quality_refine(self, event: AstrMessageEvent):
        """兼容旧命令:/tm_quality_refine(等价 /tm_purify)"""
        async for msg in self.tm_purify(event):
            yield msg

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_vec_rebuild")
    async def tm_vec_rebuild(self, event: AstrMessageEvent):
        """重建向量索引:/tm_vec_rebuild 或 /tm_vec_rebuild force=true"""
        if not self._vec_available:
            yield event.plain_result(
                "向量检索未启用或 sqlite-vec 未安装。\n"
                "请先安装:pip install sqlite-vec，并在配置中开启 enable_vector_search。"
            )
            return
        if not self._cfg.embed_provider_id:
            yield event.plain_result("未配置 embed_provider_id，无法生成向量。")
            return

        raw = (event.message_str or "").strip()
        force = "force=true" in raw.lower() or "force" in raw.lower()

        if force:
            yield event.plain_result("全量重建模式:清空现有向量后重建，请稍候...")
            with self._db() as conn:
                try:
                    conn.execute("DELETE FROM memory_vectors")
                except Exception:
                    pass
        else:
            yield event.plain_result("增量补全模式:只补缺失向量，请稍候...")

        ok, fail = await self._rebuild_vector_index()
        yield event.plain_result(
            f"向量索引重建完成:成功 {ok} 条，跳过/失败 {fail} 条。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_refine")
    async def tm_refine(self, event: AstrMessageEvent):
        """手动提纯已产生记忆。

        用法:
        /tm_refine mode=both limit=20 dry_run=false include_pinned=false <附加要求>

        参数:
        - mode: merge | split | both
        - limit: 处理记忆条数上限
        - dry_run: true/false 仅预览不落库
        - include_pinned: 是否允许处理常驻记忆
        """
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_refine\s*", "", raw, flags=re.IGNORECASE).strip()

        opts = {
            "mode": self._cfg.manual_purify_default_mode,
            "limit": str(self._cfg.manual_purify_default_limit),
            "dry_run": "false",
            "include_pinned": "false",
        }
        for m in re.finditer(
            r"(mode|limit|dry_run|include_pinned)=([^\s]+)",
            body,
            flags=re.IGNORECASE,
        ):
            opts[m.group(1).lower()] = m.group(2)
        extra = re.sub(
            r"(mode|limit|dry_run|include_pinned)=([^\s]+)",
            "",
            body,
            flags=re.IGNORECASE,
        ).strip()

        mode = str(opts["mode"]).lower()
        if mode not in {"merge", "split", "both"}:
            yield event.plain_result("mode 仅支持 merge|split|both")
            return

        try:
            limit = max(1, min(200, int(opts["limit"])))
        except Exception:
            limit = 20
        dry_run = str(opts["dry_run"]).lower() in {"1", "true", "yes", "y", "on"}
        include_pinned = str(opts["include_pinned"]).lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        result = await self._manual_purify_memories(
            event=event,
            canonical_id=canonical_id,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra,
        )

        yield event.plain_result(
            "\n".join(
                [
                    f"manual_purify done (dry_run={dry_run})",
                    f"user={canonical_id}",
                    f"mode={mode}, limit={limit}, include_pinned={include_pinned}",
                    f"updates={result['updates']}, adds={result['adds']}, deletes={result['deletes']}",
                    f"note={result.get('note', '')}",
                ]
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_mem_merge")
    async def tm_mem_merge(self, event: AstrMessageEvent):
        """手动合并多条记忆。

        用法:
        /tm_mem_merge 12,18,33 用户偏好吃火锅但关注体重管理
        """
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_mem_merge\s*", "", raw, flags=re.IGNORECASE).strip()
        if not body:
            yield event.plain_result(
                "用法: /tm_mem_merge <id1,id2,...> <合并后的记忆文本>"
            )
            return

        parts = body.split(None, 1)
        ids_part = parts[0]
        merged_text = parts[1].strip() if len(parts) > 1 else ""
        ids = [int(x) for x in re.split(r"[,，]", ids_part) if x.strip().isdigit()]
        if len(ids) < 2:
            yield event.plain_result(
                "请至少提供两个记忆ID，例如 /tm_mem_merge 12,18 新记忆内容"
            )
            return

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        rs = self._fetch_memories_by_ids(canonical_id, ids)
        if len(rs) < 2:
            yield event.plain_result("这些ID中可用记忆不足两条(可能不属于当前用户)")
            return

        if not merged_text:
            merged_text = self._auto_merge_memory_text([str(r["memory"]) for r in rs])

        keep_id = int(rs[0]["id"])
        self._update_memory_text(keep_id, merged_text)
        if self._vec_available:
            await self._upsert_vector(keep_id, merged_text)

        for r in rs[1:]:
            self._delete_memory(int(r["id"]))

        yield event.plain_result(f"合并完成:保留 #{keep_id}，删除 {len(rs) - 1} 条")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_mem_split")
    async def tm_mem_split(self, event: AstrMessageEvent):
        """手动拆分一条记忆。

        用法:
        /tm_mem_split 12 片段A|片段B|片段C
        /tm_mem_split 12   # 不给片段时自动调用 LLM 拆分
        """
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_mem_split\s*", "", raw, flags=re.IGNORECASE).strip()
        if not body:
            yield event.plain_result("用法: /tm_mem_split <id> [片段1|片段2|...]")
            return

        parts = body.split(None, 1)
        if not parts[0].isdigit():
            yield event.plain_result("第一个参数必须是记忆ID")
            return
        mem_id = int(parts[0])
        custom = parts[1].strip() if len(parts) > 1 else ""

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        row = self._fetch_memory_by_id(canonical_id, mem_id)
        if not row:
            yield event.plain_result(f"未找到记忆 {mem_id}")
            return

        if custom:
            segments = [
                self._normalize_text(x)
                for x in custom.split("|")
                if self._normalize_text(x)
            ]
        else:
            segments = await self._llm_split_memory(event, str(row["memory"]))

        if len(segments) < 2:
            yield event.plain_result("拆分结果不足两段，未执行写入")
            return

        self._update_memory_text(mem_id, segments[0])
        if self._vec_available:
            await self._upsert_vector(mem_id, segments[0])

        added = 0
        for seg in segments[1:]:
            new_id = self._insert_memory(
                canonical_id=canonical_id,
                adapter=str(row["source_adapter"]),
                adapter_user=str(row["source_user_id"]),
                memory=seg,
                score=float(row["score"]),
                memory_type=str(row["memory_type"]),
                importance=float(row["importance"]),
                confidence=float(row["confidence"]),
                source_channel="manual_split",
            )
            if self._vec_available and new_id:
                await self._upsert_vector(new_id, seg)
            added += 1

        yield event.plain_result(f"拆分完成:原记忆#{mem_id} + 新增 {added} 条")

    # =========================================================================
    # 定时蒸馏 Worker
    # =========================================================================

    async def _distill_worker_loop(self):
        """后台定时蒸馏循环。"""
        await asyncio.sleep(8)
        while self._worker_running:
            if not self._cfg.distill_pause and self._cfg.memory_mode != "active_only":
                try:
                    users, memories = await self._run_distill_cycle(
                        force=False, trigger="auto"
                    )
                    if users > 0:
                        logger.info(
                            "[tmemory] distill cycle done: users=%s memories=%s",
                            users,
                            memories,
                        )
                except Exception as e:
                    logger.warning("[tmemory] distill worker error: %s", e)

            # 如有用户合并待处理，在下一轮 sleep 前补全向量索引
            if self._merge_needs_vector_rebuild and self._vec_available:
                try:
                    ok, fail = await self._rebuild_vector_index()
                    if ok > 0:
                        logger.info(
                            "[tmemory] post-merge vector rebuild: ok=%s fail=%s",
                            ok,
                            fail,
                        )
                except Exception as _e:
                    logger.debug("[tmemory] post-merge vector rebuild error: %s", _e)
                self._merge_needs_vector_rebuild = False

            # 提纯调度:每隔 purify_interval_days 天对全部记忆做质量重评
            if self._cfg.purify_interval_days > 0:
                now_ts = time.time()
                interval_sec = self._cfg.purify_interval_days * 86400
                if now_ts - self._last_purify_ts >= interval_sec:
                    try:
                        pruned, kept = await self._run_memory_purify()
                        logger.info(
                            "[tmemory] memory purify done: pruned=%s kept=%s",
                            pruned,
                            kept,
                        )
                        self._last_purify_ts = now_ts
                    except Exception as _qe:
                        logger.warning("[tmemory] memory purify error: %s", _qe)

            await asyncio.sleep(max(3600, self._cfg.distill_interval_sec))

    async def _run_distill_cycle(
        self, force: bool = False, trigger: str = "manual"
    ) -> Tuple[int, int]:
        from .core.memory_ops import MemoryOps
        return await MemoryOps(self).run_distill_cycle(force, trigger)

    # =========================================================================
    # LLM 蒸馏
    # =========================================================================

    def _prefilter_distill_rows(self, rows: List[Dict]) -> List[Dict]:
        """蒸馏前预过滤：去除低信息量行，减少送入 LLM 的无效 token。

        过滤规则（满足任一则跳过该行）：
        - content 在 _is_low_info_content 判定为低信息量
        - role 为 'summary'（规则摘要行，已浓缩，不重复蒸馏）

        保留规则：
        - 如果过滤后为空，返回空列表（由调用方决定跳过 LLM 调用）
        - 始终保留 role=assistant 行与其配对的 user 行（上下文完整性）
          → 实现上采用宽松策略：只过滤掉纯噪声 user 行，不做配对强制保留
        """
        if not rows:
            return []

        filtered = []
        for row in rows:
            role = str(row.get("role", ""))
            content = str(row.get("content", ""))

            # 跳过规则摘要行（已是浓缩形式，无需再蒸馏）
            if role == "summary":
                continue

            # 跳过低信息量行
            if self._capture_filter.is_low_info_content(content):
                continue

            filtered.append(row)

        return filtered

    async def _distill_rows_with_llm(
        self, rows: List[Dict]
    ) -> Tuple[List[Dict[str, object]], int, int]:
        from .core.memory_ops import MemoryOps
        return await MemoryOps(self).distill_rows_with_llm(rows)

    # 匹配 thinking 模型的推理块(Gemma / Claude extended thinking 等)
    _THINK_RE = re.compile(
        r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE
    )



    def _parse_llm_json_memories(self, raw_text: str) -> List[Dict[str, object]]:
        from .core.llm_helpers import LLMHelpers
        return LLMHelpers.parse_llm_json_memories(
            raw_text, self._normalize_text, self._safe_memory_type, self._clamp01
        )

    def _strip_think_tags(self, text: str) -> str:
        from .core.llm_helpers import LLMHelpers
        return LLMHelpers.strip_think_tags(text)

    # =========================================================================
    # 记忆召回与上下文构建
    # =========================================================================

    async def _build_knowledge_injection(
        self,
        canonical_user_id: str,
        query: str,
        limit: int,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> str:
        """构建知识/偏好记忆注入块（不含 style 类型）。"""
        rows = await self._retrieve_memories(
            canonical_user_id,
            query,
            limit,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
        )
        if not rows:
            return ""

        knowledge_rows = [r for r in rows if r["memory_type"] != "style"]
        if not knowledge_rows:
            return ""

        lines = ["[用户记忆]"]
        for row in knowledge_rows:
            mtype = row["memory_type"]
            mem = row["memory"]
            lines.append(f"- ({mtype}) {mem}")
        block = "\n".join(lines)
        if self._cfg.inject_max_chars > 0 and len(block) > self._cfg.inject_max_chars:
            cutoff = max(self._cfg.inject_max_chars - 3, 1)
            block = block[:cutoff] + "…"
        return block

    async def _build_style_injection(
        self,
        canonical_user_id: str,
        query: str,
        event: AstrMessageEvent,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> str:
        """构建风格注入块（人格档案 + 对话风格记忆），仅在命中绑定时注入。

        风格蒸馏 v3 双开关逻辑:
        - enable_style_injection=ON 且 adapter+conversation 命中 active profile → 注入
        - 未绑定或 profile_id=NULL → 回退 AstrBot 默认人格，不注入任何风格内容
        """
        adapter_name = self._get_adapter_name(event)
        conversation_id = await self._get_conversation_id(event)
        if not adapter_name or not conversation_id:
            return ""

        binding = self._style_mgr.get_binding(adapter_name, conversation_id)
        if not binding or binding.get("profile_id") is None:
            # 未绑定或显式 NULL：回退 AstrBot 默认人格，不注入额外风格内容
            return ""

        prompt_supplement = binding.get("prompt_supplement", "")

        rows = await self._retrieve_memories(
            canonical_user_id,
            query,
            self._cfg.inject_memory_limit,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private,
        )
        style_memories = [r["memory"] for r in rows if r["memory_type"] == "style"]

        return self._distill_mgr.build_style_injection_block(
            persona_profile=prompt_supplement,
            style_memories=style_memories,
            inject_max_chars=self._cfg.inject_max_chars,
        )

    def _inject_block_by_position(self, req: ProviderRequest, block: str) -> None:
        """按 inject_position 配置将知识记忆块注入到正确位置。"""
        if self._cfg.inject_position == "slot":
            existing = getattr(req, "system_prompt", "") or ""
            if self._cfg.inject_slot_marker in existing:
                req.system_prompt = existing.replace(
                    self._cfg.inject_slot_marker, block, 1
                )
            else:
                req.system_prompt = existing + ("\n\n" if existing else "") + block
        elif self._cfg.inject_position == "user_message_before":
            original_prompt = getattr(req, "prompt", "") or ""
            req.prompt = block + "\n\n" + original_prompt if original_prompt else block
        elif self._cfg.inject_position == "user_message_after":
            original_prompt = getattr(req, "prompt", "") or ""
            req.prompt = original_prompt + ("\n\n" if original_prompt else "") + block
        else:  # system_prompt
            existing = getattr(req, "system_prompt", "") or ""
            req.system_prompt = existing + ("\n\n" if existing else "") + block

    async def _get_conversation_id(self, event: AstrMessageEvent) -> str:
        """获取当前会话的 conversation_id，用于 style_bindings 查询。"""
        try:
            umo = self._safe_get_unified_msg_origin(event)
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr and umo:
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid:
                    return str(cid)
        except Exception:
            pass
        return ""

    async def build_memory_context(
        self, canonical_user_id: str, query: str, limit: int = 6
    ) -> str:
        """构建完整的调试用记忆上下文块(供 /tm_context 指令使用)。"""
        rows = await self._retrieve_memories(canonical_user_id, query, limit)
        recent = self._fetch_recent_conversation(canonical_user_id, limit=6)

        recent_lines = []
        for role, content in recent[-4:]:
            recent_lines.append(f"- {role}: {content}")

        memory_lines = []
        for row in rows:
            display_score = float(
                row.get("final_score", row.get("_retrieval_score", row.get("score", 0.0)))
            )
            memory_lines.append(
                f"- ({row['memory_type']}, score={display_score:.3f}) {row['memory']}"
            )

        if not memory_lines:
            memory_lines = ["- (none) 暂无匹配长期记忆"]

        return "\n".join(
            [
                "[Memory Context]",
                f"canonical_user_id={canonical_user_id}",
                f"query={query}",
                "",
                "Recent Session:",
                *(recent_lines if recent_lines else ["- (none)"]),
                "",
                "Relevant Long-Term Memories:",
                *memory_lines,
            ]
        )

    # =========================================================================
    # 数据库初始化
    # =========================================================================

    def _resolve_db_path(self) -> str:
        cwd = os.getcwd()
        candidates = [
            os.path.join(cwd, "data", "plugin_data", self.plugin_name),
            os.path.join(cwd, "plugin_data", self.plugin_name),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
        ]

        for path in candidates:
            try:
                os.makedirs(path, exist_ok=True)
                return os.path.join(path, "tmemory.db")
            except OSError:
                continue

        fallback_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(fallback_dir, "tmemory.db")



    def _init_db(self):
        self._db_mgr.init_db(self._vec_available, getattr(self, "embed_dim", 768))

    def _migrate_schema(self, conn: Optional[sqlite3.Connection] = None):
        if conn is None:
            # For backward compatibility with tests calling this manually
            with self._db() as _conn:
                self._db_mgr.migrate_schema(_conn)
        else:
            self._db_mgr.migrate_schema(conn)

    def _db(self) -> _LockedConnection:
        return self._db_mgr.db()

    def _close_db(self) -> None:
        self._db_mgr.close()

    # =========================================================================
    # 向量检索辅助方法（实现见 core.vector）
    # =========================================================================

    async def _get_http_session(self):
        from .core import vector as _vec
        return await _vec.get_http_session(self)

    async def _embed_text(self, text: str) -> Optional[List[float]]:
        from .core import vector as _vec
        return await _vec.embed_text(self, text)

    async def _upsert_vector(self, memory_id: int, text: str) -> bool:
        from .core import vector as _vec
        return await _vec.upsert_vector(self, memory_id, text)

    def _delete_vector(self, memory_id: int, conn=None) -> None:
        from .core import vector as _vec
        _vec.delete_vector(self, memory_id, conn)

    def _delete_vectors_for_user(self, canonical_id: str, conn=None) -> None:
        from .core import vector as _vec
        _vec.delete_vectors_for_user(self, canonical_id, conn)

    async def _rebuild_vector_index(self) -> Tuple[int, int]:
        from .core import vector as _vec
        return await _vec.rebuild_vector_index(self)

    def _log_memory_event(
        self, canonical_user_id: str, event_type: str,
        payload: Dict[str, object], conn: Optional[sqlite3.Connection] = None,
    ):
        """记录记忆相关事件到审计日志 memory_events。"""
        from .core import memory_ops as _mo
        _mo.log_memory_event(self, canonical_user_id, event_type, payload, conn)

    # =========================================================================
    # 身份管理
    # =========================================================================

    def _safe_get_unified_msg_origin(self, event: AstrMessageEvent) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _platform_str(val):
        try:
            from astrbot.core.platform.platform_metadata import PlatformMetadata  # type: ignore
            if isinstance(val, PlatformMetadata):
                return val.id or val.name
        except ImportError:
            pass
        return str(val)

    def _get_adapter_name(self, event: AstrMessageEvent) -> str:
        for name in ("get_platform_name", "get_adapter_name", "get_client_name"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    val = fn()
                    if val:
                        return str(val)
                except Exception:
                    pass

        for attr in ("platform_name", "adapter_name", "adapter", "platform"):
            val = getattr(event, attr, None)
            if val:
                return self._platform_str(val)

        return "unknown_adapter"

    def _get_adapter_user_id(self, event: AstrMessageEvent) -> str:
        for name in ("get_sender_id", "get_user_id"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    val = fn()
                    if val:
                        return str(val)
                except Exception:
                    pass

        sender_name = getattr(event, "get_sender_name", None)
        if callable(sender_name):
            try:
                val = sender_name()
                if val:
                    return str(val)
            except Exception:
                pass

        return "unknown_user"

    def _get_memory_scope(self, event: AstrMessageEvent) -> str:
        """根据 memory_scope 配置和消息类型确定本次的 scope 标签。"""
        if self._cfg.memory_scope == "session":
            try:
                from astrbot.core.platform import MessageType  # type: ignore

                if event.get_message_type() == MessageType.FRIEND_MESSAGE:
                    return "private"
                gid = event.get_group_id()
                return f"group:{gid}" if gid else "private"
            except Exception:
                return "private"
        return "user"

    async def _get_current_persona_async(self, event: AstrMessageEvent) -> str:
        """使用 AstrBot conversation_manager 异步获取当前人格 ID。"""
        try:
            umo = self._safe_get_unified_msg_origin(event)
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr and umo:
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid:
                    conv = await conv_mgr.get_conversation(umo, cid)
                    if conv and getattr(conv, "persona_id", None):
                        return str(conv.persona_id)
        except Exception:
            pass
        return self._get_current_persona(event)

    def _get_current_persona(self, event: AstrMessageEvent) -> str:
        """同步获取人格 ID(fallback)。优先从 event extras 获取，否则返回空。"""
        try:
            # AstrBot 在某些版本中将 conversation 挂到 event extras
            extras = getattr(event, "_extras", {}) or {}
            conv = extras.get("conversation") or getattr(event, "conversation", None)
            if conv:
                persona = getattr(conv, "persona_id", None)
                if persona:
                    return str(persona)
        except Exception:
            pass
        return ""

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        """是否为群聊事件。"""
        try:
            from astrbot.core.platform import MessageType  # type: ignore

            return event.get_message_type() != MessageType.FRIEND_MESSAGE
        except Exception:
            gid = event.get_group_id()
            return bool(gid)


    # =========================================================================
    # 记忆 CRUD
    # =========================================================================

    def _insert_memory(
        self,
        canonical_id: str,
        adapter: str,
        adapter_user: str,
        memory: str,
        score: float,
        memory_type: str,
        importance: float,
        confidence: float,
        source_channel: str = "default",
        persona_id: str = "",
        scope: str = "user",
    ) -> int:
        from .core.memory_ops import MemoryOps
        return MemoryOps(self).insert_memory(
            canonical_id=canonical_id,
            adapter=adapter,
            adapter_user=adapter_user,
            memory=memory,
            score=score,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            source_channel=source_channel,
            persona_id=persona_id,
            scope=scope,
        )

    def _delete_memory(self, memory_id: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            deleted = cur.rowcount > 0
            if deleted and row:
                self._memory_logger.log_memory_event(
                    canonical_user_id=str(row["canonical_user_id"]),
                    event_type="delete",
                    payload={"memory_id": memory_id},
                    conn=conn,
                )
                if self._vec_available:
                    self._delete_vector(memory_id, conn=conn)
            return deleted

    def _list_memories(
        self, canonical_id: str, limit: int = 8
    ) -> List[Dict[str, object]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, updated_at, is_pinned
                FROM memories
                WHERE canonical_user_id=? AND is_active=1
                ORDER BY importance DESC, score DESC, updated_at DESC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()

        return [
            {
                "id": int(r["id"]),
                "memory_type": str(r["memory_type"]),
                "memory": str(r["memory"]),
                "score": float(r["score"]),
                "importance": float(r["importance"]),
                "confidence": float(r["confidence"]),
                "reinforce_count": int(r["reinforce_count"]),
                "updated_at": str(r["updated_at"]),
                "is_pinned": int(r["is_pinned"]),
            }
            for r in rows
        ]

    async def _retrieve_memories(
        self,
        canonical_id: str,
        query: str,
        limit: int,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> List[Dict[str, object]]:
        """从 memories 表中检索最相关的记忆，按综合评分排序。只返回 is_active=1 的有效记忆。"""
        # 步骤 1:获取查询向量
        query_vec: Optional[List[float]] = None
        if self._vec_available and query:
            query_vec = await self._embed_text(query)

        # 步骤 2:底层 DB 检索
        scored, _ = await self._retrieval_mgr.retrieve_memories(
            canonical_id=canonical_id,
            query=query,
            limit=limit,
            query_vec=query_vec,
            scope=scope,
            persona_id=persona_id,
            exclude_private=exclude_private
        )

        # 去重:高语义重叠的记忆只保留分数最高的那条
        deduped = self._retrieval_mgr.deduplicate_results(scored, limit * 2)

        # 可选 Reranker:对候选结果做精排
        if self._cfg.enable_reranker and self._cfg.rerank_base_url and query and len(deduped) > 1:
            top_result = await self._rerank_results(query, deduped, limit)
        else:
            top_result = deduped[:limit]

        # 对命中的 top 结果进行强化:reinforce_count += 1，批量更新减少 DB 开销
        if top_result:
            reinforce_now = self._now()
            reinforce_ids = [int(item["id"]) for item in top_result]
            placeholders = ",".join(["?"] * len(reinforce_ids))
            with self._db() as conn:
                conn.execute(
                    f"UPDATE memories SET reinforce_count = reinforce_count + 1,"
                    f" last_seen_at = ? WHERE id IN ({placeholders})",
                    [reinforce_now, *reinforce_ids],
                )

        return top_result

    async def _manual_purify_memories(
        self,
        event: AstrMessageEvent,
        canonical_id: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
    ) -> Dict[str, object]:
        from .core.memory_ops import MemoryOps
        return await MemoryOps(self).manual_purify_memories(
            event=event,
            canonical_id=canonical_id,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra_instruction,
        )

    async def _llm_purify_operations(
        self,
        event: AstrMessageEvent,
        rows: List[Dict[str, object]],
        mode: str,
        extra_instruction: str,
    ) -> Dict[str, object]:
        from .core import maintenance as _m
        return await _m.llm_purify_operations(self, event, rows, mode, extra_instruction)

    async def _manual_refine_memories(
        self,
        event: AstrMessageEvent,
        canonical_id: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
    ) -> Dict[str, object]:
        """兼容旧方法名，等价 _manual_purify_memories。"""
        return await self._manual_purify_memories(
            event=event,
            canonical_id=canonical_id,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra_instruction,
        )

    async def _llm_split_memory(
        self, event: AstrMessageEvent, memory_text: str
    ) -> List[str]:
        from .core import maintenance as _m
        return await _m.llm_split_memory(self, event, memory_text)

    def _parse_json_object(self, text: str) -> Optional[Dict[str, object]]:
        """从文本中提取 JSON 对象。"""
        if not text:
            return None
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                    return data if isinstance(data, dict) else None
                except Exception:
                    return None
        return None

    def _list_memories_for_purify(
        self, canonical_id: str, limit: int, include_pinned: bool
    ) -> List[Dict[str, object]]:
        with self._db() as conn:
            sql = (
                "SELECT id, memory, memory_type, score, importance, confidence, reinforce_count, is_pinned "
                "FROM memories WHERE canonical_user_id=? AND is_active=1 "
                + ("" if include_pinned else "AND is_pinned=0 ")
                + "ORDER BY importance DESC, score DESC, updated_at DESC LIMIT ?"
            )
            rows = conn.execute(sql, (canonical_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def _fetch_memory_by_id(
        self, canonical_id: str, memory_id: int
    ) -> Optional[Dict[str, object]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, canonical_user_id, source_adapter, source_user_id, memory, memory_type, score, importance, confidence "
                "FROM memories WHERE id=? AND canonical_user_id=?",
                (memory_id, canonical_id),
            ).fetchone()
        return dict(row) if row else None

    def _fetch_memories_by_ids(
        self, canonical_id: str, ids: List[int]
    ) -> List[Dict[str, object]]:
        if not ids:
            return []
        placeholders = ",".join(["?"] * len(ids))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT id, memory, memory_type, score, importance, confidence, source_adapter, source_user_id "
                f"FROM memories WHERE canonical_user_id=? AND id IN ({placeholders}) ORDER BY id",
                [canonical_id, *ids],
            ).fetchall()
        return [dict(r) for r in rows]

    def _update_memory_text(self, memory_id: int, memory: str) -> None:
        now = self._now()
        mhash = hashlib.sha256(self._normalize_text(memory).encode("utf-8")).hexdigest()
        tokenized_memory = " ".join(jieba.cut_for_search(memory))
        with self._db() as conn:
            conn.execute(
                "UPDATE memories SET memory=?, tokenized_memory=?, memory_hash=?, updated_at=? WHERE id=?",
                (memory, tokenized_memory, mhash, now, memory_id),
            )

    def _update_memory_full(
        self, memory_id: int, memory: str, memory_type: str,
        score: float, importance: float, confidence: float,
    ) -> None:
        from .core import memory_ops as _mo
        _mo.update_memory_full(self, memory_id, memory, memory_type, score, importance, confidence)

    def _auto_merge_memory_text(self, memories: List[str]) -> str:
        """无 LLM 时的简单合并策略:去重后拼接。"""
        uniq: List[str] = []
        seen = set()
        for m in memories:
            n = self._normalize_text(m)
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        if not uniq:
            return ""
        if len(uniq) == 1:
            return uniq[0]
        merged = ";".join(uniq)
        if not merged.startswith("用户"):
            merged = f"用户{merged}"
        return merged[:300]

    async def _rerank_results(
        self, query: str, candidates: List[Dict[str, object]], top_n: int
    ) -> List[Dict[str, object]]:
        from .core import vector as _vec
        return await _vec.rerank_results(self, query, candidates, top_n)

    # =========================================================================
    # 对话缓存
    # =========================================================================

    async def _insert_conversation(
        self,
        canonical_id: str,
        role: str,
        content: str,
        source_adapter: str,
        source_user_id: str,
        unified_msg_origin: str,
        scope: str = "user",
        persona_id: str = "",
    ):
        await asyncio.to_thread(
            self._insert_conversation_sync,
            canonical_id,
            role,
            content,
            source_adapter,
            source_user_id,
            unified_msg_origin,
            scope,
            persona_id,
        )

    def _insert_conversation_sync(
        self, canonical_id: str, role: str, content: str, source_adapter: str,
        source_user_id: str, unified_msg_origin: str, scope: str = "user", persona_id: str = "",
    ):
        from .core import maintenance as _m
        _m.insert_conversation_sync(
            self, canonical_id, role, content, source_adapter,
            source_user_id, unified_msg_origin, scope, persona_id,
        )

    def _fetch_recent_conversation(
        self, canonical_id: str, limit: int = 20
    ) -> List[Tuple[str, str]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM conversation_cache
                WHERE canonical_user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()
        return [(str(r["role"]), str(r["content"])) for r in reversed(rows)]

    def _pending_distill_users(
        self, limit: int, min_batch_count: Optional[int] = None
    ) -> List[str]:
        min_required = (
            self._cfg.distill_min_batch_count
            if min_batch_count is None
            else max(1, int(min_batch_count))
        )
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT canonical_user_id, COUNT(*) as cnt
                FROM conversation_cache
                WHERE distilled=0
                GROUP BY canonical_user_id
                HAVING cnt >= ?
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (min_required, limit),
            ).fetchall()
        return [str(r["canonical_user_id"]) for r in rows]

    def _fetch_pending_rows(self, canonical_id: str, limit: int) -> List[Dict]:
        """获取待蒸馏的对话行，返回 dict 列表(避免 sqlite3.Row 在 async 上下文中的问题)。"""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, canonical_user_id, role, content, source_adapter, source_user_id, unified_msg_origin, scope, persona_id
                FROM conversation_cache
                WHERE canonical_user_id=? AND distilled=0
                ORDER BY id ASC
                LIMIT ?
                """,
                (canonical_id, limit),
            ).fetchall()
            # 立即转换为普通 dict，防止 Connection 关闭后访问失效
            return [dict(r) for r in rows]

    def _mark_rows_distilled(self, ids: Sequence[int]):
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        params = [self._now(), *ids]
        with self._db() as conn:
            conn.execute(
                f"UPDATE conversation_cache SET distilled=1, distilled_at=? WHERE id IN ({placeholders})",
                params,
            )

    def _count_pending_users(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS n FROM (SELECT canonical_user_id FROM conversation_cache WHERE distilled=0 GROUP BY canonical_user_id)"
            ).fetchone()
        return int(row["n"] if row else 0)

    def _count_pending_rows(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS n FROM conversation_cache WHERE distilled=0"
            ).fetchone()
        return int(row["n"] if row else 0)

    # =========================================================================
    # 上下文压缩(规则摘要，不触发 LLM)
    # =========================================================================

    def _trim_conversation(self, canonical_id: str, keep_last: int):
        with self._db() as conn:
            conn.execute(
                """
                DELETE FROM conversation_cache
                WHERE canonical_user_id=?
                AND id NOT IN (
                    SELECT id FROM conversation_cache
                    WHERE canonical_user_id=?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (canonical_id, canonical_id, keep_last),
            )

    def _optimize_context(self, canonical_id: str):
        """对超出阈值的历史做轻量规则摘要压缩，不触发 LLM，以节省 token。"""
        from .core import maintenance as _m
        _m.optimize_context(self, canonical_id)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_pin")
    async def tm_pin(self, event: AstrMessageEvent):
        """常驻一条记忆(不会被衰减/剪枝/冲突覆盖):/tm_pin 12"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_pin\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("用法: /tm_pin <记忆ID>")
            return
        ok = self._set_pinned(int(arg), True)
        yield event.plain_result(
            f"记忆 {arg} 已设为常驻" if ok else f"未找到记忆 {arg}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_unpin")
    async def tm_unpin(self, event: AstrMessageEvent):
        """取消常驻一条记忆:/tm_unpin 12"""
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_unpin\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("用法: /tm_unpin <记忆ID>")
            return
        ok = self._set_pinned(int(arg), False)
        yield event.plain_result(
            f"记忆 {arg} 已取消常驻" if ok else f"未找到记忆 {arg}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_export")
    async def tm_export(self, event: AstrMessageEvent):
        """导出当前用户的所有记忆(JSON):/tm_export"""
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        data = self._export_user_data(canonical_id)
        yield event.plain_result(json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_purge")
    async def tm_purge(self, event: AstrMessageEvent):
        """删除当前用户的所有记忆和缓存:/tm_purge"""
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        deleted = self._purge_user_data(canonical_id)
        yield event.plain_result(
            f"已清除 {canonical_id} 的所有数据:{deleted['memories']} 条记忆，{deleted['cache']} 条缓存。"
        )

    def _set_pinned(self, memory_id: int, pinned: bool) -> bool:
        """设置/取消常驻标记。常驻记忆不会被衰减、剪枝、冲突覆盖。"""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE memories SET is_pinned = ? WHERE id = ?",
                (1 if pinned else 0, memory_id),
            )
            return cur.rowcount > 0

    # =========================================================================
    # 风格蒸馏管理指令 (v3: style_profiles + style_bindings)
    # =========================================================================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("style_distill")
    async def style_distill(self, event: AstrMessageEvent):
        """控制风格蒸馏采集开关: /style_distill on|off

        on  — 启用风格蒸馏素材收集和档案更新
        off — 停用风格蒸馏采集，不影响普通记忆整理
        WebUI 中 enable_style_distill 为只读状态，唯一写入口即此命令。
        """
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/style_distill\s*", "", raw, flags=re.IGNORECASE).strip().lower()
        if arg not in ("on", "off"):
            yield event.plain_result("用法: /style_distill on|off\n当前状态: " + ("开启" if self._cfg.enable_style_distill else "关闭"))
            return

        enabled = arg == "on"
        if enabled == self._cfg.enable_style_distill:
            yield event.plain_result(f"风格蒸馏采集已处于 {'开启' if enabled else '关闭'} 状态，无需重复设置。")
            return

        self._cfg.enable_style_distill = enabled
        # 持久化到插件自身配置，确保重启后状态保留
        try:
            style_settings = self.config.get("style_distill_settings", {})
            if not isinstance(style_settings, dict):
                style_settings = {}
            style_settings["enable_style_distill"] = enabled
            self.config["style_distill_settings"] = style_settings
            self.config.save_config()
        except Exception as e:
            logger.exception("[tmemory] style_distill 配置持久化失败: %s", e)
        yield event.plain_result(f"风格蒸馏采集已{'开启' if enabled else '关闭'}（不影响普通记忆整理）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("style_profile_create")
    async def style_profile_create(self, event: AstrMessageEvent):
        """创建人格档案: /style_profile_create <name> | <prompt_supplement> [| description]"""
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/style_profile_create\s*", "", raw, flags=re.IGNORECASE).strip()
        parts = [p.strip() for p in body.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            yield event.plain_result("用法: /style_profile_create <名称> | <人格补充提示词> [| 描述]")
            return

        name = parts[0]
        prompt_supplement = parts[1]
        description = parts[2] if len(parts) > 2 else ""

        existing = self._style_mgr.get_profile_by_name(name)
        if existing:
            yield event.plain_result(f"人格档案 '{name}' 已存在 (id={existing['id']})。")
            return

        pid = self._style_mgr.create_profile(name, prompt_supplement, description)
        yield event.plain_result(f"人格档案已创建: id={pid}, name={name}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("style_profile_delete")
    async def style_profile_delete(self, event: AstrMessageEvent):
        """删除人格档案: /style_profile_delete <name>"""
        raw = (event.message_str or "").strip()
        name = re.sub(r"^/style_profile_delete\s*", "", raw, flags=re.IGNORECASE).strip()
        if not name:
            yield event.plain_result("用法: /style_profile_delete <名称>")
            return

        profile = self._style_mgr.get_profile_by_name(name)
        if not profile:
            yield event.plain_result(f"未找到人格档案: {name}")
            return

        self._style_mgr.delete_profile(int(profile["id"]))
        yield event.plain_result(f"人格档案 '{name}' 已删除（相关绑定已解绑）。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("style_profile_list")
    async def style_profile_list(self, event: AstrMessageEvent):
        """列出所有人格档案: /style_profile_list"""
        profiles = self._style_mgr.list_profiles()
        if not profiles:
            yield event.plain_result("暂无自定义人格档案。")
            return
        lines = [f"人格档案 ({len(profiles)} 个):"]
        for p in profiles:
            lines.append(
                f"  [{p['id']}] {p['profile_name']}"
                f" | supplement={p['prompt_supplement'][:40]}..."
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("style_bind")
    async def style_bind(self, event: AstrMessageEvent):
        """绑定当前会话到人格档案: /style_bind <profile_name>"""
        raw = (event.message_str or "").strip()
        name = re.sub(r"^/style_bind\s*", "", raw, flags=re.IGNORECASE).strip()
        if not name:
            yield event.plain_result("用法: /style_bind <人格档案名称>")
            return

        profile = self._style_mgr.get_profile_by_name(name)
        if not profile:
            yield event.plain_result(f"未找到人格档案: {name}。请先 /style_profile_create")
            return

        adapter_name = self._get_adapter_name(event)
        conversation_id = await self._get_conversation_id(event)
        if not adapter_name or not conversation_id:
            yield event.plain_result("无法获取当前会话信息。")
            return

        self._style_mgr.set_binding(adapter_name, conversation_id, int(profile["id"]))
        yield event.plain_result(
            f"已绑定: adapter={adapter_name}, conversation={conversation_id} -> profile={name}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("style_unbind")
    async def style_unbind(self, event: AstrMessageEvent):
        """解除当前会话的人格档案绑定: /style_unbind"""
        adapter_name = self._get_adapter_name(event)
        conversation_id = await self._get_conversation_id(event)
        if not adapter_name or not conversation_id:
            yield event.plain_result("无法获取当前会话信息。")
            return

        removed = self._style_mgr.remove_binding(adapter_name, conversation_id)
        if removed:
            yield event.plain_result(
                f"已解绑: adapter={adapter_name}, conversation={conversation_id}"
            )
        else:
            yield event.plain_result("当前会话未绑定人格档案。")

    # =========================================================================
    # 蒸馏历史与健康监测
    # =========================================================================

    async def _run_memory_purify(self) -> tuple[int, int]:
        """对全量已蒸馏记忆进行提纯。见 core.maintenance.run_memory_purify。"""
        from .core import maintenance as _m
        return await _m.run_memory_purify(self)

    async def _llm_purify_judge(
        self, provider_id: str, memories: List[Dict]
    ) -> List[int]:
        from .core import maintenance as _m
        return await _m.llm_purify_judge(self, provider_id, memories)

    async def _run_quality_refinement(self) -> tuple[int, int]:
        """兼容旧方法名，等价 _run_memory_purify。"""
        return await self._run_memory_purify()

    def _record_distill_history(self, **kwargs):
        from .core import distill_validator as _dv
        _dv.record_distill_history(self, **kwargs)

    def _get_distill_history(self, limit: int = 20) -> List[Dict]:
        from .core import distill_validator as _dv
        return _dv.get_distill_history(self, limit=limit)

    def _get_distill_cost_summary(self, last_n: int = 10) -> Dict:
        from .core import distill_validator as _dv
        return _dv.get_distill_cost_summary(self, last_n=last_n)

    # =========================================================================
    # 蒸馏输出校验器
    # =========================================================================

    def _validate_distill_output(
        self, items: List[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        from .core import distill_validator as _dv
        return _dv.validate_distill_output(self, items)

    def _is_junk_memory(self, text: str) -> bool:
        from .core import distill_validator as _dv
        return _dv.is_junk_memory(text)

    def _is_unsafe_memory(self, text: str) -> bool:
        from .core import distill_validator as _dv
        return _dv.is_unsafe_memory(text)

    # =========================================================================
    # 记忆生命周期衰减
    # =========================================================================

    def _decay_stale_memories(self):
        from .core import maintenance as _m
        _m.decay_stale_memories(self)

    def _auto_prune_low_quality(self):
        from .core import maintenance as _m
        _m.auto_prune_low_quality(self)

    # =========================================================================
    # 数据导出与清除
    # =========================================================================

    def _export_user_data(self, canonical_id: str) -> Dict:
        from .core import maintenance as _m
        return _m.export_user_data(self, canonical_id)

    def _purge_user_data(self, canonical_id: str) -> Dict[str, int]:
        from .core import maintenance as _m
        return _m.purge_user_data(self, canonical_id)

    # =========================================================================
    # 敏感信息脱敏
    # =========================================================================

    def _build_sanitize_patterns(self) -> list:
        """构建脱敏正则列表。"""
        return [
            (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
            (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱]"),
            (re.compile(r"\d{17}[\dXx]"), "[身份证]"),
            (re.compile(r"\d{15,19}"), "[长数字]"),
        ]

    def _sanitize_text(self, text: str) -> str:
        """对文本进行敏感信息脱敏。"""
        for pattern, replacement in self._sanitize_patterns:
            text = pattern.sub(replacement, text)
        return text



    # =========================================================================
    # 工具方法
    # =========================================================================

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    # 对话行前缀正则(user: / assistant: 等)
    _TRANSCRIPT_PREFIX_RE = re.compile(
        r"^(user|assistant|summary)\s*:\s*", re.IGNORECASE | re.MULTILINE
    )
    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _safe_memory_type(self, value: object) -> str:
        s = str(value or "fact").strip().lower()
        if s in {"preference", "fact", "task", "restriction", "style"}:
            return s
        return "fact"

    def _clamp01(self, value: object) -> float:  # type: ignore[arg-type]
        try:
            num = float(value)  # type: ignore[arg-type]
        except Exception:
            num = 0.0
        return max(0.0, min(1.0, num))

    def _get_global_stats(self) -> Dict[str, int]:
        """获取全局统计信息。"""
        from .core import maintenance as _m
        return _m.get_global_stats(self)
