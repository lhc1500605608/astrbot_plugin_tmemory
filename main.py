import asyncio
from typing import Optional, Dict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from .core.db import DatabaseManager
from .core.config import PluginConfig, PluginLifecycleMixin, parse_config
from .core.capture import CaptureFilter
from .core.distill import DistillManager, DistillRuntimeMixin
from .core.consolidation import ConsolidationRuntimeMixin
from .core.utils import MemoryLogger, PluginHelpersMixin, PluginHandlersMixin
from .core.identity import IdentityManager

from .search.retrieval import RetrievalManager
from .core.injection import InjectionBuilder

# 插件自身命令名集合，防止 AstrBot 剥离 wake_prefix(/)后控制命令文本进入 conversation_cache
_CMD_FIRST_WORDS = frozenset({
    "tm_distill_now", "tm_worker", "tm_memory", "tm_context", "tm_bind", "tm_merge",
    "tm_forget", "tm_stats", "tm_distill_history", "tm_purify", "tm_quality_refine",
    "tm_vec_rebuild", "tm_refine", "tm_mem_merge", "tm_mem_split", "tm_pin",
    "tm_unpin", "tm_export", "tm_purge",
})


@register(
    "tmemory",
    "shangtang",
    "AstrBot 用户长期记忆插件(自动采集 + 定时LLM蒸馏 + 跨适配器合并)",
    "0.7.1",
)
class TMemoryPlugin(PluginLifecycleMixin, DistillRuntimeMixin, ConsolidationRuntimeMixin, PluginHelpersMixin, PluginHandlersMixin, Star):
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
        self._injection_builder = InjectionBuilder(self._cfg, self._retrieval_mgr)
        self._memory_logger = MemoryLogger(self._db_mgr)
        self._identity_mgr = IdentityManager(self._db_mgr, self._cfg, self._memory_logger)

        # ── 敏感信息脱敏 ──────────────────────────────────────────────────────
        self._sanitize_patterns = self._build_sanitize_patterns()

        # ── WebUI 独立服务器(降级保护)────────────────────────────────────
        self._web_server = self._safe_load_web_server()






    # =========================================================================
    # AstrBot 生命周期
    # =========================================================================



    # =========================================================================
    # 消息采集 Hooks
    # =========================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """自动采集每条用户消息。"""
        return await self._handle_on_any_message(event)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """可选采集模型回复，作为后续批量蒸馏素材。"""
        return await self._handle_on_llm_response(event, resp)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 调用前注入记忆。"""
        return await self._handle_on_llm_request(event, req)

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
        return await self._handle_tool_remember(event, content, memory_type)

    @filter.llm_tool(name="recall")
    async def tool_recall(self, event: AstrMessageEvent, query: str):
        """检索与查询相关的用户记忆。当需要回忆用户的偏好、历史信息或之前提到的内容时调用。

        Args:
            query(string): 查询文本，描述想要回忆的内容
        """
        return await self._handle_tool_recall(event, query)

    # =========================================================================
    # 管理指令
    # =========================================================================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_distill_now")
    async def tm_distill_now(self, event: AstrMessageEvent):
        """手动触发一次批量蒸馏:/tm_distill_now"""
        async for result in self._handle_tm_distill_now(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_worker")
    async def tm_worker(self, event: AstrMessageEvent):
        """查看蒸馏 worker 状态:/tm_worker"""
        async for result in self._handle_tm_worker(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_memory")
    async def tm_memory(self, event: AstrMessageEvent):
        """查看当前用户的记忆:/tm_memory"""
        async for result in self._handle_tm_memory(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_context")
    async def tm_context(self, event: AstrMessageEvent):
        """预览记忆召回上下文:/tm_context 今天吃什么"""
        async for result in self._handle_tm_context(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_bind")
    async def tm_bind(self, event: AstrMessageEvent):
        """绑定当前账号到统一用户 ID:/tm_bind alice"""
        async for result in self._handle_tm_bind(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_merge")
    async def tm_merge(self, event: AstrMessageEvent):
        """合并两个统一用户 ID 的记忆:/tm_merge old_id new_id"""
        async for result in self._handle_tm_merge(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_forget")
    async def tm_forget(self, event: AstrMessageEvent):
        """删除一条记忆:/tm_forget 12"""
        async for result in self._handle_tm_forget(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_stats")
    async def tm_stats(self, event: AstrMessageEvent):
        """查看全局统计信息:/tm_stats"""
        async for result in self._handle_tm_stats(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_distill_history")
    async def tm_distill_history(self, event: AstrMessageEvent):
        """查看最近蒸馏历史（含 token 成本）:/tm_distill_history"""
        async for result in self._handle_tm_distill_history(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_purify")
    async def tm_purify(self, event: AstrMessageEvent):
        """手动触发一次记忆提纯:/tm_purify"""
        async for result in self._handle_tm_purify(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_quality_refine")
    async def tm_quality_refine(self, event: AstrMessageEvent):
        """兼容旧命令:/tm_quality_refine(等价 /tm_purify)"""
        async for result in self._handle_tm_quality_refine(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_vec_rebuild")
    async def tm_vec_rebuild(self, event: AstrMessageEvent):
        """重建向量索引:/tm_vec_rebuild 或 /tm_vec_rebuild force=true"""
        async for result in self._handle_tm_vec_rebuild(event):
            yield result

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
        async for result in self._handle_tm_refine(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_mem_merge")
    async def tm_mem_merge(self, event: AstrMessageEvent):
        """手动合并多条记忆。

        用法:
        /tm_mem_merge 12,18,33 用户偏好吃火锅但关注体重管理
        """
        async for result in self._handle_tm_mem_merge(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_mem_split")
    async def tm_mem_split(self, event: AstrMessageEvent):
        """手动拆分一条记忆。

        用法:
        /tm_mem_split 12 片段A|片段B|片段C
        /tm_mem_split 12   # 不给片段时自动调用 LLM 拆分
        """
        async for result in self._handle_tm_mem_split(event):
            yield result


























    @staticmethod



































    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_pin")
    async def tm_pin(self, event: AstrMessageEvent):
        """常驻一条记忆(不会被衰减/剪枝/冲突覆盖):/tm_pin 12"""
        async for result in self._handle_tm_pin(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_unpin")
    async def tm_unpin(self, event: AstrMessageEvent):
        """取消常驻一条记忆:/tm_unpin 12"""
        async for result in self._handle_tm_unpin(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_export")
    async def tm_export(self, event: AstrMessageEvent):
        """导出当前用户的所有记忆(JSON):/tm_export"""
        async for result in self._handle_tm_export(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("tm_purge")
    async def tm_purge(self, event: AstrMessageEvent):
        """删除当前用户的所有记忆和缓存:/tm_purge"""
        async for result in self._handle_tm_purge(event):
            yield result





















