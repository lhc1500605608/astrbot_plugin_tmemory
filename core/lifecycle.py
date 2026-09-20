"""Plugin lifecycle mixin and safe-default application.

Extracted from ``core/config.py`` per ADR-009 (module boundary split). Kept as a
thin physical move only — no behavior change; ``core.config`` re-exports these
symbols so existing imports keep working.
"""

import asyncio
import importlib.util
import logging
import os
from typing import Dict

logger = logging.getLogger("astrbot")


def apply_safe_defaults(plugin) -> None:
    """为 plugin 实例应用所有配置与运行时属性的安全默认值。
    
    抽取自 main.TmemoryPlugin._set_safe_defaults，供主类在 __init__ 中调用。
    保持原先语义，不改变任何字段默认值。
    """
    c = plugin._cfg
    c.cache_max_rows = 20
    c.memory_max_chars = 220
    c.enable_auto_capture = True
    c.capture_assistant_reply = True
    c.no_memory_marker = "\x00[astrbot:no-memory]\x00"
    c.capture_skip_prefixes = ["提醒 #"]
    c.capture_skip_regex = None
    c.distill_interval_sec = 17280
    c.distill_min_batch_count = 20
    c.distill_batch_limit = 80
    c.distill_model_id = ""
    c.distill_provider_id = ""
    c.enable_memory_injection = True
    c.inject_enable_vector_search = False
    c.manual_purify_default_mode = "both"
    c.manual_purify_default_limit = 20
    plugin.manual_refine_default_mode = "both"
    plugin.manual_refine_default_limit = 20
    c.distill_pause = False
    c.distill_rule_gating = False
    c.distill_rule_gate_min_chars = 40
    c.distill_prompt_cache = True
    c.distill_prompt_cache_max_rows = 1000
    c.purify_interval_days = 0
    c.purify_model_id = ""
    c.purify_min_score = 0.0
    # ── 向量检索管理器 ──────────────────────────────────────────
    plugin._vector_manager = None
    c.embedding_source = "provider"
    c.embedding_provider_id = ""
    c.embed_provider_id = ""
    c.embed_model_id = ""
    plugin.embed_model = ""
    c.embed_dim = 1024
    c.auto_rebuild_on_dim_change = True
    plugin.vector_weight = 0.4
    plugin.min_vector_sim = 0.15
    plugin._sqlite_vec = None
    plugin._vec_available = False
    c.embed_base_url = ""
    c.embed_api_key = ""
    c.local_embedding_path = "data/bge-small-zh-v1.5"
    c.local_embedding_model_file = "model_quantized.onnx"
    c.local_embedding_max_length = 512
    c.enable_reranker = False
    c.rerank_provider_id = ""
    c.rerank_model_id = ""
    plugin.rerank_model = ""
    c.rerank_top_n = 5
    c.rerank_base_url = ""
    c.memory_scope = "user"
    c.memory_mode = "hybrid"
    c.private_memory_in_group = False
    c.inject_position = "system_prompt"
    c.inject_slot_marker = "{{tmemory}}"
    c.inject_memory_limit = 5
    c.inject_max_chars = 0
    c.enable_layered_injection = False
    c.inject_working_turns = 5
    c.inject_episode_limit = 3
    c.inject_episode_max_chars = 600
    c.inject_style_max_chars = 400
    c.session_reset_policy = "keep"
    # ── 主动记忆（Plan TMEAAA-379 B2 / BC-3）──
    c.proactive_enabled = False
    c.proactive_interval_sec = 300
    c.proactive_max_candidates_per_cycle = 5
    c.proactive_per_user_window_sec = 3600
    c.proactive_per_user_max_per_window = 3
    c.proactive_min_interval_sec = 300
    c.proactive_daily_budget = 20
    c.proactive_reminder_enabled = True
    c.proactive_recall_enabled = True
    c.proactive_opt_in_default = False
    c.proactive_max_message_chars = 200
    plugin._proactive_task = None
    plugin._proactive_engine = None
    c.enable_consolidation_pipeline = False
    c.enable_episodic_summarization = True
    c.enable_episode_semantic_distill = True
    c.profile_extraction_enabled = False
    c.profile_extraction_min_messages = 8
    c.profile_extraction_max_users_per_cycle = 10
    c.profile_extraction_timeout_sec = 120
    c.profile_stability_default = 0.5
    c.profile_auto_archive_threshold = 0.0
    c.profile_max_items_per_user = 200
    c.distill_max_users_per_cycle = 10
    c.stage_timeout_sec = 120
    c.use_independent_consolidation_model = False
    c.consolidation_provider_id = ""
    c.consolidation_model_id = ""
    c.episode_summary_min_messages = 5
    c.episode_summary_max_input_tokens = 3000
    c.episode_session_gap_minutes = 60
    plugin._sanitize_patterns = []
    plugin._distill_task = None
    plugin._worker_running = False
    plugin._merge_needs_vector_rebuild = False
    plugin._fts5_needs_rebuild = False
    plugin._last_purify_ts = 0.0
    plugin._embed_ok_count = 0
    plugin._embed_fail_count = 0
    plugin._embed_provider_fail_count = 0
    plugin._embed_last_source = ""
    plugin._embed_last_error = ""
    plugin._vec_query_count = 0
    plugin._vec_hit_count = 0
    plugin._embed_cache_hit_count = 0
    plugin._embed_cache_miss_count = 0
    plugin._embed_semaphore = None
    plugin._http_session = None
    # ── 触发门控与批处理效率 ──────────────────────────────────────────────
    c.capture_min_content_len = 5
    c.capture_dedup_window = 10
    c.distill_user_throttle_sec = 0
    # 运行时统计：每次蒸馏周期中因门控被跳过的行数
    plugin._distill_skipped_rows = 0
    # ── 蒸馏降本运行时统计（Plan TMEAAA-379 B3）──
    plugin._distill_rule_gated_batches = 0
    plugin._distill_rule_gated_rows = 0
    plugin._distill_rule_deferred_batches = 0
    plugin._distill_prompt_cache_hits = 0
    # 内存缓存：per-user 最近蒸馏完成时间戳（用于节流）
    plugin._user_last_distilled_ts = {}
    # ── 会话生命周期（Phase 4a）─────────────────────────────────────────
    plugin._session_conv_ids = {}
    plugin._agent_begin_count = 0
    plugin._agent_done_count = 0
    # ── 兼容性打点（Plan TMEAAA-354 Phase 1）────────────────────────────
    plugin._extra_user_temp_fallback_count = 0
    plugin._persona_private_fallback_count = 0


# =============================================================================
# Plugin lifecycle mixin
# =============================================================================


class _NullWebServer:
    """WebUI 降级替身，保证核心功能不受 WebUI 加载失败影响。"""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class PluginLifecycleMixin:
    def _set_safe_defaults(self):
        """设置所有配置属性的安全默认值，确保任何配置解析失败都不会导致 AttributeError。
        
        实现细节见 core.lifecycle.apply_safe_defaults。
        """
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
            "embedding_source": self._cfg.embedding_source,
            "embedding_provider_id": self._cfg.embedding_provider_id,
            "embedding_provider": self._cfg.embed_provider_id,
            "embedding_api_key": self._cfg.embed_api_key,
            "embedding_model": self._cfg.embed_model_id,
            "embedding_base_url": self._cfg.embed_base_url,
            "vector_dim": self._cfg.embed_dim,
            "auto_rebuild_on_dim_change": self._cfg.auto_rebuild_on_dim_change,
            "rerank_provider_id": self._cfg.rerank_provider_id,
            "local_embedding_path": self._cfg.local_embedding_path,
            "local_embedding_model_file": self._cfg.local_embedding_model_file,
            "local_embedding_max_length": self._cfg.local_embedding_max_length,
        }

    def _legacy_webui_config(self) -> Dict:
        """合并顶层配置与 webui_settings 嵌套配置。"""
        webui_cfg = dict(self.config)
        webui_sub = self.config.get("webui_settings", {})
        if isinstance(webui_sub, dict):
            webui_cfg.update(webui_sub)
        return webui_cfg

    def _safe_load_web_server(self):
        """安全加载 legacy WebUI 服务器，失败或未启用时降级为 _NullWebServer。

        Plan TMEAAA-354 Phase 3：默认由 Dashboard 托管的 Pages bridge 取代
        legacy aiohttp 面板；``webui_legacy_enabled=true`` 时保留一版回滚。
        """
        try:
            webui_cfg = self._legacy_webui_config()
            if not bool(webui_cfg.get("webui_legacy_enabled", False)):
                logger.info(
                    "[tmemory] legacy WebUI 未启用（webui_legacy_enabled=false），"
                    "使用 Dashboard Plugin Pages bridge。"
                )
                return _NullWebServer()
            TMemoryWebServer = self._load_web_server_class()
            return TMemoryWebServer(self, webui_cfg)
        except Exception as e:
            logger.warning(
                "[tmemory] WebUI 加载失败，核心功能不受影响: %s", e
            )
            return _NullWebServer()

    def _safe_register_pages_bridge(self):
        """注册 Dashboard Plugin Pages bridge（能力不可用时返回 None）。"""
        try:
            from ..adapters import version as _version_adapter

            if not _version_adapter.has_plugin_pages():
                logger.info(
                    "[tmemory] 当前 AstrBot 无 Plugin Pages 能力（需 >=4.28），跳过 bridge 注册。"
                )
                return None

            context = getattr(self, "context", None)
            if context is None or not callable(
                getattr(context, "register_web_api", None)
            ):
                return None

            from ..web.bridge import PluginPagesBridge

            bridge = PluginPagesBridge(self)
            count = bridge.register(context)
            logger.info("[tmemory] Plugin Pages bridge 已注册 %s 条路由", count)
            return bridge
        except Exception as e:
            logger.warning("[tmemory] Plugin Pages bridge 注册失败: %s", e)
            return None

    def _load_web_server_class(self):
        """通过文件路径动态加载 web/legacy_server.py。

        避免 `No module named 'web_server'`，同时自建父包上下文，使
        ``from ..web_handlers import ...`` 等相对 import 在脱离 sys.path 时仍可解析。
        """
        import types

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        web_server_path = os.path.join(root_dir, "web", "legacy_server.py")
        if not os.path.exists(web_server_path):
            raise ImportError(f"web/legacy_server.py not found: {web_server_path}")

        plugin_module = self.__class__.__module__
        module_prefix = plugin_module.rsplit(".", 1)[0] if "." in plugin_module else self.plugin_name
        module_name = f"{module_prefix}.web.legacy_server"

        # 保证父包 `module_prefix` / `module_prefix.web` 可在相对 import 时被解析。
        sys_modules = __import__("sys").modules
        parent_pkg = sys_modules.get(module_prefix)
        if parent_pkg is None:
            parent_pkg = types.ModuleType(module_prefix)
            parent_pkg.__path__ = [root_dir]  # type: ignore[attr-defined]
            sys_modules[module_prefix] = parent_pkg
        web_pkg_name = f"{module_prefix}.web"
        if web_pkg_name not in sys_modules:
            web_pkg = types.ModuleType(web_pkg_name)
            web_pkg.__path__ = [os.path.join(root_dir, "web")]  # type: ignore[attr-defined]
            sys_modules[web_pkg_name] = web_pkg
            setattr(parent_pkg, "web", web_pkg)

        spec = importlib.util.spec_from_file_location(module_name, web_server_path)
        if spec is None or spec.loader is None:
            raise ImportError("failed to create module spec for web/legacy_server.py")

        module = importlib.util.module_from_spec(spec)
        sys_modules[module_name] = module
        spec.loader.exec_module(module)

        cls = getattr(module, "TMemoryWebServer", None)
        if cls is None:
            raise ImportError("TMemoryWebServer not found in web/legacy_server.py")
        return cls

    async def initialize(self):
        self._load_sqlite_vec()
        self._init_db()
        self._migrate_schema()

        # 上游能力探测：集中记录一次，并在 extra_user_temp 不可用时显式告警。
        try:
            from ..adapters import version as _version_adapter

            caps = _version_adapter.log_capabilities()
            if (
                self._cfg.inject_position == "extra_user_temp"
                and not caps.has_mark_as_temp
            ):
                logger.warning(
                    "[tmemory] 当前 inject_position=extra_user_temp，但 %s",
                    _version_adapter.extra_user_temp_hint(),
                )
        except Exception as e:  # 能力探测绝不应阻断启动
            logger.debug("[tmemory] capability probe skipped: %s", e)

        # 初始化 VectorManager(如果向量检索启用)
        if self._cfg.enable_vector_search:
            try:
                from ..vector_manager import VectorManager
                vr = self._get_vector_retrieval_config_from_cfg()
                self._vector_manager = VectorManager(self.db_path, vr)
                # 注入 AstrBot Context（Provider 解析用）；兼容只有两参构造的 VectorManager。
                try:
                    self._vector_manager.context = getattr(self, "context", None)
                except Exception:
                    pass
                await self._vector_manager.initialize()
                logger.info(
                    "[tmemory] VectorManager initialized: %s",
                    self._vector_manager.status()
                    if hasattr(self._vector_manager, "status")
                    else "source=standalone",
                )
            except Exception as e:
                logger.error("[tmemory] Failed to initialize VectorManager: %s", e)
                self._vector_manager = None

        # Provider 维度变化 → 更新维度、失效 query 缓存、重建索引（BC-2 回滚见 auto_rebuild_on_dim_change）
        if self._vector_manager is not None:
            try:
                from . import vector as _vector

                dim_result = await _vector.apply_provider_dim_change(self)
                if dim_result.get("changed"):
                    logger.warning("[tmemory] embedding dim reconciled: %s", dim_result)
            except Exception as e:
                logger.warning("[tmemory] provider dim reconcile failed: %s", e)

        self._worker_running = True
        self._distill_task = asyncio.create_task(self._distill_worker_loop())

        # 主动记忆 worker（Plan TMEAAA-379 B2）：默认关闭时零调度、零副作用。
        try:
            self._proactive_task = await self._start_proactive_worker()
        except Exception as e:
            logger.warning("[tmemory] proactive worker 启动失败，核心功能不受影响: %s", e)
            self._proactive_task = None

        # 启动独立 WebUI 服务器
        try:
            await self._web_server.start()
        except Exception as e:
            logger.warning("[tmemory] WebUI 启动失败，核心功能不受影响: %s", e)
            self._web_server = _NullWebServer()

        # 注册会话删除钩子（Phase 4a；能力缺失自动跳过）
        try:
            self._register_session_lifecycle_hooks()
        except Exception as e:
            logger.warning("[tmemory] 会话删除钩子注册失败，核心功能不受影响: %s", e)

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
        # 主动记忆 worker（默认未启动，即 None）
        proactive_task = getattr(self, "_proactive_task", None)
        if proactive_task and not proactive_task.done():
            proactive_task.cancel()
            try:
                await proactive_task
            except asyncio.CancelledError:
                pass
        self._proactive_task = None
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
