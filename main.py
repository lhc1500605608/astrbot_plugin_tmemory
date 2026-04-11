import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import time
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register


@register(
    "tmemory",
    "shangtang",
    "AstrBot 用户长期记忆插件（自动采集 + 定时LLM蒸馏 + 跨适配器合并）",
    "0.3.0",
)
class TMemoryPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.plugin_name = "astrbot_plugin_tmemory"
        self.db_path = self._resolve_db_path()

        # ── 基础配置 ──────────────────────────────────────────────────────────
        self.cache_max_rows = int(self.config.get("cache_max_rows", 20))
        self.memory_max_chars = int(self.config.get("memory_max_chars", 220))

        # ── 自动采集 ──────────────────────────────────────────────────────────
        self.enable_auto_capture = bool(self.config.get("enable_auto_capture", True))
        self.capture_assistant_reply = bool(
            self.config.get("capture_assistant_reply", True)
        )

        # ── 蒸馏调度 ──────────────────────────────────────────────────────────
        # distill_interval_sec: 两次蒸馏之间的最小间隔（秒），默认 17280s（约 4.8 小时，每天约 5 次）。
        # 只有积累了 distill_min_batch_count 条未蒸馏消息的用户才会被蒸馏，
        # 从而避免实时蒸馏造成的 token 浪费。
        self.distill_interval_sec = max(4 * 3600, int(self.config.get("distill_interval_sec", 17280)))
        self.distill_min_batch_count = max(8, int(self.config.get("distill_min_batch_count", 20)))
        self.distill_batch_limit = max(20, int(self.config.get("distill_batch_limit", 80)))
        # 指定用于蒸馏的 provider ID；留空时自动从消息上下文推断。
        self.distill_provider_id = str(
            self.config.get("distill_provider_id", "")
        ).strip()

        # ── 记忆召回注入 ──────────────────────────────────────────────────────
        # enable_memory_injection: 是否在 LLM 调用前将记忆上下文注入 system prompt
        self.enable_memory_injection = bool(
            self.config.get("enable_memory_injection", True)
        )
        self.inject_memory_limit = int(self.config.get("inject_memory_limit", 5))

        # ── 蒸馏暂停开关 ──────────────────────────────────────────────────────
        self.distill_pause = bool(self.config.get("distill_pause", False))

        # ── 敏感信息脱敏 ──────────────────────────────────────────────────────
        self._sanitize_patterns = self._build_sanitize_patterns()

        # ── 内部状态 ──────────────────────────────────────────────────────────
        self._distill_task: Optional[asyncio.Task] = None
        self._worker_running = False

        # ── WebUI 独立服务器 ──────────────────────────────────────────────────
        TMemoryWebServer = self._load_web_server_class()
        # webui_settings 是嵌套 object，展平到顶层供 server 读取
        webui_cfg = dict(self.config)
        webui_sub = self.config.get("webui_settings", {})
        if isinstance(webui_sub, dict):
            webui_cfg.update(webui_sub)
        self._web_server = TMemoryWebServer(self, webui_cfg)

    def _load_web_server_class(self):
        """通过文件路径动态加载 web_server.py，避免 `No module named 'web_server'`。"""
        web_server_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "web_server.py"
        )
        if not os.path.exists(web_server_path):
            raise ImportError(f"web_server.py not found: {web_server_path}")

        spec = importlib.util.spec_from_file_location(
            "tmemory_web_server", web_server_path
        )
        if spec is None or spec.loader is None:
            raise ImportError("failed to create module spec for web_server.py")

        module = importlib.util.module_from_spec(spec)
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

        self._worker_running = True
        self._distill_task = asyncio.create_task(self._distill_worker_loop())

        # 启动独立 WebUI 服务器
        await self._web_server.start()

        logger.info(
            "[tmemory] initialized, db=%s, auto_capture=%s, memory_injection=%s, distill_interval=%ss",
            self.db_path,
            self.enable_auto_capture,
            self.enable_memory_injection,
            self.distill_interval_sec,
        )

    async def terminate(self):
        self._worker_running = False
        if self._distill_task and not self._distill_task.done():
            self._distill_task.cancel()
            try:
                await self._distill_task
            except asyncio.CancelledError:
                pass
        # 关闭 WebUI 服务器
        await self._web_server.stop()
        logger.info("[tmemory] terminated")

    # =========================================================================
    # 消息采集 Hooks
    # =========================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """自动采集每条用户消息，仅写入 conversation_cache，不实时蒸馏。"""
        if not self.enable_auto_capture:
            return

        text = self._normalize_text(getattr(event, "message_str", "") or "")
        if not text:
            return

        # 跳过插件指令，避免把控制命令当作记忆素材。
        if text.startswith("/"):
            return

        canonical_id, adapter, adapter_user = self._resolve_current_identity(event)
        umo = self._safe_get_unified_msg_origin(event)
        self._insert_conversation(
            canonical_id=canonical_id,
            role="user",
            content=self._sanitize_text(text),
            source_adapter=adapter,
            source_user_id=adapter_user,
            unified_msg_origin=umo,
        )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """可选采集模型回复，作为后续批量蒸馏素材。"""
        if not self.enable_auto_capture or not self.capture_assistant_reply:
            return

        text = self._normalize_text(getattr(resp, "completion_text", "") or "")
        if not text:
            return

        canonical_id, adapter, adapter_user = self._resolve_current_identity(event)
        umo = self._safe_get_unified_msg_origin(event)
        self._insert_conversation(
            canonical_id=canonical_id,
            role="assistant",
            content=text,
            source_adapter=adapter,
            source_user_id=adapter_user,
            unified_msg_origin=umo,
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 调用前，将召回的用户记忆注入 system_prompt。

        只追加到 system_prompt 尾部，不替换原有内容，确保 persona 等配置不受影响。
        """
        if not self.enable_memory_injection:
            return

        try:
            canonical_id, _, _ = self._resolve_current_identity(event)
            query = self._normalize_text(getattr(req, "prompt", "") or "")
            memory_block = self._build_injection_block(
                canonical_id, query, self.inject_memory_limit
            )
            if not memory_block:
                return

            existing = getattr(req, "system_prompt", "") or ""
            sep = "\n\n" if existing else ""
            req.system_prompt = existing + sep + memory_block
        except Exception as e:
            logger.warning("[tmemory] on_llm_request inject failed: %s", e)

    # =========================================================================
    # 管理指令
    # =========================================================================

    @filter.command("tm_distill_now")
    async def tm_distill_now(self, event: AstrMessageEvent):
        """手动触发一次批量精馏：/tm_distill_now"""
        processed_users, total_memories = await self._run_distill_cycle(force=True, trigger="manual_cmd")
        yield event.plain_result(
            f"批量精馏完成：处理用户 {processed_users} 个，新增/更新记忆 {total_memories} 条。"
        )

    @filter.command("tm_worker")
    async def tm_worker(self, event: AstrMessageEvent):
        """查看蒸馏 worker 状态：/tm_worker"""
        pending_users = self._count_pending_users()
        pending_rows = self._count_pending_rows()
        yield event.plain_result(
            "\n".join(
                [
                    f"worker_running={self._worker_running}",
                    f"distill_interval_sec={self.distill_interval_sec}",
                    f"distill_min_batch_count={self.distill_min_batch_count}",
                    f"pending_users={pending_users}",
                    f"pending_rows={pending_rows}",
                ]
            )
        )

    @filter.command("tm_memory")
    async def tm_memory(self, event: AstrMessageEvent):
        """查看当前用户的记忆：/tm_memory"""
        canonical_id, _, _ = self._resolve_current_identity(event)
        memories = self._list_memories(canonical_id, limit=8)
        if not memories:
            yield event.plain_result("当前还没有已保存记忆。")
            return

        lines = [f"canonical_id={canonical_id}"]
        for row in memories:
            lines.append(
                f"[{row['id']}] [{row['memory_type']}] s={row['score']:.2f} i={row['importance']:.2f} c={row['confidence']:.2f} r={row['reinforce_count']} | {row['memory']}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("tm_context")
    async def tm_context(self, event: AstrMessageEvent):
        """预览记忆召回上下文：/tm_context 今天吃什么"""
        raw = (event.message_str or "").strip()
        query = re.sub(r"^/tm_context\s*", "", raw, flags=re.IGNORECASE).strip()
        if not query:
            yield event.plain_result("用法: /tm_context <当前问题>")
            return

        canonical_id, _, _ = self._resolve_current_identity(event)
        context_block = self.build_memory_context(canonical_id, query, limit=6)
        yield event.plain_result(context_block)

    @filter.command("tm_bind")
    async def tm_bind(self, event: AstrMessageEvent):
        """绑定当前账号到统一用户 ID：/tm_bind alice"""
        raw = (event.message_str or "").strip()
        canonical_id = re.sub(r"^/tm_bind\s*", "", raw, flags=re.IGNORECASE).strip()
        if not canonical_id:
            yield event.plain_result("用法: /tm_bind <统一用户ID>")
            return

        adapter = self._get_adapter_name(event)
        adapter_user = self._get_adapter_user_id(event)
        self._bind_identity(adapter, adapter_user, canonical_id)
        yield event.plain_result(
            f"绑定成功：{adapter}:{adapter_user} -> {canonical_id}"
        )

    @filter.command("tm_merge")
    async def tm_merge(self, event: AstrMessageEvent):
        """合并两个统一用户 ID 的记忆：/tm_merge old_id new_id"""
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

        moved = self._merge_identity(from_id, to_id)
        yield event.plain_result(
            f"合并完成：{from_id} -> {to_id}，迁移记忆 {moved} 条。"
        )

    @filter.command("tm_forget")
    async def tm_forget(self, event: AstrMessageEvent):
        """删除一条记忆：/tm_forget 12"""
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

    @filter.command("tm_stats")
    async def tm_stats(self, event: AstrMessageEvent):
        """查看全局统计信息：/tm_stats"""
        stats = self._get_global_stats()
        lines = [
            f"total_users: {stats['total_users']}",
            f"total_active_memories: {stats['total_active_memories']}",
            f"total_deactivated_memories: {stats['total_deactivated_memories']}",
            f"pending_cached_rows: {stats['pending_cached_rows']}",
            f"total_events: {stats['total_events']}",
        ]
        yield event.plain_result("\n".join(lines))

    # =========================================================================
    # 定时蒸馏 Worker
    # =========================================================================

    async def _distill_worker_loop(self):
        """后台定时蒸馏循环。"""
        await asyncio.sleep(8)
        while self._worker_running:
            if not self.distill_pause:
                try:
                    users, memories = await self._run_distill_cycle(force=False, trigger="auto")
                    if users > 0:
                        logger.info(
                            "[tmemory] distill cycle done: users=%s memories=%s",
                            users,
                            memories,
                        )
                except Exception as e:
                    logger.warning("[tmemory] distill worker error: %s", e)

            await asyncio.sleep(max(3600, self.distill_interval_sec))

    async def _run_distill_cycle(
        self, force: bool = False, trigger: str = "manual"
    ) -> Tuple[int, int]:
        """执行一轮蒸馏，记录历史，单用户失败不中断整轮。"""
        started_at = self._now()
        t0 = time.time()
        min_required = 1 if force else self.distill_min_batch_count
        pending_users = self._pending_distill_users(
            limit=(100 if force else 20), min_batch_count=min_required
        )
        processed_users = 0
        total_memories = 0
        failed_users = 0
        errors: list = []

        for canonical_id in pending_users:
            try:
                rows = self._fetch_pending_rows(canonical_id, self.distill_batch_limit)
                if (not force) and len(rows) < self.distill_min_batch_count:
                    continue

                llm_items = await self._distill_rows_with_llm(rows)
                if not llm_items:
                    self._mark_rows_distilled([int(r["id"]) for r in rows])
                    continue

                valid_items = self._validate_distill_output(llm_items)
                for item in valid_items:
                    mem_text = self._sanitize_text(
                        self._normalize_text(str(item.get("memory", "")))
                    )
                    if not mem_text:
                        continue
                    self._insert_memory(
                        canonical_id=canonical_id,
                        adapter=str(rows[0]["source_adapter"]),
                        adapter_user=str(rows[0]["source_user_id"]),
                        memory=mem_text,
                        score=self._clamp01(item.get("score", 0.7)),
                        memory_type=str(item.get("memory_type", "fact")),
                        importance=self._clamp01(item.get("importance", 0.6)),
                        confidence=self._clamp01(item.get("confidence", 0.7)),
                        source_channel="scheduled_distill",
                    )
                    total_memories += 1

                self._mark_rows_distilled([int(r["id"]) for r in rows])
                self._optimize_context(canonical_id)
                processed_users += 1
            except Exception as e:
                failed_users += 1
                errors.append(f"{canonical_id}: {type(e).__name__}: {e}")
                logger.warning("[tmemory] distill failed for user %s: %s", canonical_id, e)

        # 记录蒸馏历史
        duration = round(time.time() - t0, 2)
        self._record_distill_history(
            started_at=started_at,
            trigger=trigger,
            users_processed=processed_users,
            memories_created=total_memories,
            users_failed=failed_users,
            errors=errors,
            duration=duration,
        )

        # 顺便执行记忆衰减
        self._decay_stale_memories()

        return processed_users, total_memories

    # =========================================================================
    # LLM 蒸馏
    # =========================================================================

    async def _distill_rows_with_llm(self, rows: List[Dict]) -> List[Dict[str, object]]:
        """用 LLM 对一批对话行进行结构化蒸馏，失败时回退到规则蒸馏。"""
        transcript_lines = []
        for row in rows:
            role = str(row["role"])
            content = str(row["content"])
            transcript_lines.append(f"{role}: {content}")

        transcript = "\n".join(transcript_lines)
        prompt = self._build_distill_prompt(transcript)

        chat_provider_id = await self._resolve_distill_provider_id(rows)
        if not chat_provider_id:
            # 无法确定 provider 时，回退到规则蒸馏。
            return [
                {
                    "memory": self._distill_text(transcript),
                    "memory_type": "fact",
                    "importance": 0.55,
                    "confidence": 0.50,
                    "score": 0.60,
                }
            ]

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=prompt,
            )
            completion_text = self._normalize_text(
                getattr(llm_resp, "completion_text", "") or ""
            )
            parsed = self._parse_llm_json_memories(completion_text)
            if parsed:
                return parsed
        except Exception as e:
            logger.warning("[tmemory] llm distill failed, fallback to rule: %s", e)

        return [
            {
                "memory": self._distill_text(transcript),
                "memory_type": "fact",
                "importance": 0.55,
                "confidence": 0.50,
                "score": 0.60,
            }
        ]

    def _build_distill_prompt(self, transcript: str) -> str:
        return (
            "你是记忆蒸馏器。请从对话中提炼出稳定、长期有价值的用户记忆。\n"
            "仅输出 JSON，不要输出任何解释文字。\n\n"
            "输出格式（必须严格遵守）：\n"
            "{\n"
            '  "memories": [\n'
            "    {\n"
            '      "memory": "字符串，简洁明确",\n'
            '      "memory_type": "preference|fact|task|restriction|style",\n'
            '      "importance": 0.0到1.0,\n'
            '      "confidence": 0.0到1.0,\n'
            '      "score": 0.0到1.0\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "规则：\n"
            "1. 不要提炼一次性寒暄、无意义闲聊。\n"
            "2. 优先提炼偏好、长期目标、约束、稳定事实。\n"
            "3. 最多返回 6 条。\n"
            "4. 数值字段必须是数字。\n\n"
            "对话如下：\n" + transcript
        )

    async def _resolve_distill_provider_id(self, rows: List[Dict]) -> str:
        """解析要用于蒸馏的 provider ID。优先使用配置，其次从消息 UMO 推断。"""
        if self.distill_provider_id:
            return self.distill_provider_id

        umo = ""
        for row in rows:
            maybe = str(row["unified_msg_origin"] or "")
            if maybe:
                umo = maybe
                break

        if not umo:
            return ""

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            return str(provider_id or "")
        except Exception:
            # 尝试同步方式
            try:
                prov = self.context.get_using_provider(umo=umo)
                if prov:
                    return str(prov.meta().id)
            except Exception:
                pass
            return ""

    def _parse_llm_json_memories(self, raw_text: str) -> List[Dict[str, object]]:
        if not raw_text:
            return []

        data = None
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                chunk = raw_text[start : end + 1]
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    return []
            else:
                return []

        items = data.get("memories") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        result = []
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            mem = self._normalize_text(str(item.get("memory", "")))
            if not mem:
                continue
            result.append(
                {
                    "memory": mem,
                    "memory_type": self._safe_memory_type(
                        item.get("memory_type", "fact")
                    ),
                    "importance": self._clamp01(item.get("importance", 0.6)),
                    "confidence": self._clamp01(item.get("confidence", 0.7)),
                    "score": self._clamp01(item.get("score", 0.7)),
                }
            )
        return result

    # =========================================================================
    # 记忆召回与上下文构建
    # =========================================================================

    def _build_injection_block(
        self, canonical_user_id: str, query: str, limit: int
    ) -> str:
        """构建注入到 system_prompt 的记忆块。

        格式简洁，专为 token 节省设计。返回空字符串表示无有效记忆。
        """
        rows = self._retrieve_memories(canonical_user_id, query, limit)
        if not rows:
            return ""

        lines = ["[用户记忆]"]
        for row in rows:
            mtype = row["memory_type"]
            mem = row["memory"]
            lines.append(f"- ({mtype}) {mem}")

        return "\n".join(lines)

    def build_memory_context(
        self, canonical_user_id: str, query: str, limit: int = 6
    ) -> str:
        """构建完整的调试用记忆上下文块（供 /tm_context 指令使用）。"""
        rows = self._retrieve_memories(canonical_user_id, query, limit)
        recent = self._fetch_recent_conversation(canonical_user_id, limit=6)

        recent_lines = []
        for role, content in recent[-4:]:
            recent_lines.append(f"- {role}: {content}")

        memory_lines = []
        for row in rows:
            memory_lines.append(
                f"- ({row['memory_type']}, score={row['final_score']:.3f}) {row['memory']}"
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
        with self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identity_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    adapter TEXT NOT NULL,
                    adapter_user_id TEXT NOT NULL,
                    canonical_user_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(adapter, adapter_user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_user_id TEXT NOT NULL,
                    source_adapter TEXT NOT NULL,
                    source_user_id TEXT NOT NULL,
                    source_channel TEXT NOT NULL DEFAULT 'default',
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    memory TEXT NOT NULL,
                    memory_hash TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0.5,
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    reinforce_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(canonical_user_id, memory_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_adapter TEXT NOT NULL DEFAULT 'unknown',
                    source_user_id TEXT NOT NULL DEFAULT 'unknown',
                    unified_msg_origin TEXT NOT NULL DEFAULT '',
                    distilled INTEGER NOT NULL DEFAULT 0,
                    distilled_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS distill_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    trigger_type TEXT NOT NULL DEFAULT 'auto',
                    users_processed INTEGER NOT NULL DEFAULT 0,
                    memories_created INTEGER NOT NULL DEFAULT 0,
                    users_failed INTEGER NOT NULL DEFAULT 0,
                    errors TEXT NOT NULL DEFAULT '',
                    duration_sec REAL NOT NULL DEFAULT 0
                )
                """
            )

    def _migrate_schema(self):
        self._ensure_columns(
            "memories",
            {
                "source_channel": "TEXT NOT NULL DEFAULT 'default'",
                "memory_type": "TEXT NOT NULL DEFAULT 'fact'",
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "confidence": "REAL NOT NULL DEFAULT 0.5",
                "reinforce_count": "INTEGER NOT NULL DEFAULT 0",
                "last_seen_at": "TEXT NOT NULL DEFAULT ''",
                "is_active": "INTEGER NOT NULL DEFAULT 1",
            },
        )
        self._ensure_columns(
            "conversation_cache",
            {
                "source_adapter": "TEXT NOT NULL DEFAULT 'unknown'",
                "source_user_id": "TEXT NOT NULL DEFAULT 'unknown'",
                "unified_msg_origin": "TEXT NOT NULL DEFAULT ''",
                "distilled": "INTEGER NOT NULL DEFAULT 0",
                "distilled_at": "TEXT NOT NULL DEFAULT ''",
            },
        )

        with self._db() as conn:
            conn.execute(
                "UPDATE memories SET last_seen_at=COALESCE(NULLIF(last_seen_at, ''), updated_at, created_at)"
            )
            # Ensure memory_events table exists when upgrading from older versions
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS distill_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    trigger_type TEXT NOT NULL DEFAULT 'auto',
                    users_processed INTEGER NOT NULL DEFAULT 0,
                    memories_created INTEGER NOT NULL DEFAULT 0,
                    users_failed INTEGER NOT NULL DEFAULT 0,
                    errors TEXT NOT NULL DEFAULT '',
                    duration_sec REAL NOT NULL DEFAULT 0
                )
                """
            )

    def _ensure_columns(self, table_name: str, wanted: Dict[str, str]):
        with self._db() as conn:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            existing = {str(row["name"]) for row in rows}
            for col, ddl in wanted.items():
                if col in existing:
                    continue
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {ddl}")

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _log_memory_event(
        self,
        canonical_user_id: str,
        event_type: str,
        payload: Dict[str, object],
        conn: Optional[sqlite3.Connection] = None,
    ):
        """记录记忆相关事件到审计日志 memory_events。

        当已在一个 with self._db() 块内时，传入 conn 以复用连接，
        避免嵌套打开第二个写事务导致 database is locked。
        """
        row = (
            canonical_user_id,
            event_type,
            json.dumps(payload, ensure_ascii=False),
            self._now(),
        )
        sql = (
            "INSERT INTO memory_events(canonical_user_id, event_type, payload_json, created_at)"
            " VALUES(?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, row)
        else:
            with self._db() as _conn:
                _conn.execute(sql, row)

    # =========================================================================
    # 身份管理
    # =========================================================================

    def _safe_get_unified_msg_origin(self, event: AstrMessageEvent) -> str:
        try:
            return str(getattr(event, "unified_msg_origin", "") or "")
        except Exception:
            return ""

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

        for attr in ("platform_name", "adapter_name", "adapter"):
            val = getattr(event, attr, None)
            if val:
                return str(val)

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

    def _resolve_current_identity(
        self, event: AstrMessageEvent
    ) -> Tuple[str, str, str]:
        adapter = self._get_adapter_name(event)
        adapter_user = self._get_adapter_user_id(event)

        with self._db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM identity_bindings WHERE adapter=? AND adapter_user_id=?",
                (adapter, adapter_user),
            ).fetchone()

        if row:
            return row["canonical_user_id"], adapter, adapter_user

        canonical = f"{adapter}:{adapter_user}"
        self._bind_identity(adapter, adapter_user, canonical)
        return canonical, adapter, adapter_user

    def _bind_identity(self, adapter: str, adapter_user: str, canonical_id: str):
        now = self._now()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO identity_bindings(adapter, adapter_user_id, canonical_user_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(adapter, adapter_user_id)
                DO UPDATE SET canonical_user_id=excluded.canonical_user_id, updated_at=excluded.updated_at
                """,
                (adapter, adapter_user, canonical_id, now),
            )
        self._log_memory_event(
            canonical_user_id=canonical_id,
            event_type="bind",
            payload={"adapter": adapter, "adapter_user_id": adapter_user},
        )

    def _merge_identity(self, from_id: str, to_id: str) -> int:
        now = self._now()
        moved = 0
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT source_adapter, source_user_id, source_channel, memory_type, memory, memory_hash,
                       score, importance, confidence, reinforce_count, last_seen_at, is_active
                FROM memories WHERE canonical_user_id=?
                """,
                (from_id,),
            ).fetchall()
            for row in rows:
                try:
                    conn.execute(
                        """
                        INSERT INTO memories(
                            canonical_user_id, source_adapter, source_user_id, source_channel,
                            memory_type, memory, memory_hash, score, importance, confidence,
                            reinforce_count, last_seen_at, created_at, updated_at, is_active
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            to_id,
                            row["source_adapter"],
                            row["source_user_id"],
                            row["source_channel"],
                            row["memory_type"],
                            row["memory"],
                            row["memory_hash"],
                            row["score"],
                            row["importance"],
                            row["confidence"],
                            row["reinforce_count"],
                            row["last_seen_at"],
                            now,
                            now,
                            row["is_active"],
                        ),
                    )
                    moved += 1
                except sqlite3.IntegrityError:
                    conn.execute(
                        """
                        UPDATE memories
                        SET importance = MAX(importance, ?),
                            confidence = MAX(confidence, ?),
                            reinforce_count = reinforce_count + ?,
                            updated_at = ?
                        WHERE canonical_user_id=? AND memory_hash=?
                        """,
                        (
                            row["importance"],
                            row["confidence"],
                            row["reinforce_count"],
                            now,
                            to_id,
                            row["memory_hash"],
                        ),
                    )

            conn.execute("DELETE FROM memories WHERE canonical_user_id=?", (from_id,))
            conn.execute(
                "UPDATE identity_bindings SET canonical_user_id=?, updated_at=? WHERE canonical_user_id=?",
                (to_id, now, from_id),
            )
            conn.execute(
                "UPDATE conversation_cache SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )
        self._log_memory_event(
            canonical_user_id=to_id,
            event_type="merge",
            payload={"from_id": from_id, "to_id": to_id, "moved_count": moved},
        )
        return moved

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
    ) -> int:
        normalized = self._normalize_text(memory)
        mhash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = self._now()
        memory_type_safe = self._safe_memory_type(memory_type)
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, reinforce_count FROM memories WHERE canonical_user_id=? AND memory_hash=?",
                (canonical_id, mhash),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE memories
                    SET score=?, memory_type=?, importance=MAX(importance, ?), confidence=MAX(confidence, ?),
                        reinforce_count=?, last_seen_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        self._clamp01(score),
                        memory_type_safe,
                        self._clamp01(importance),
                        self._clamp01(confidence),
                        int(row["reinforce_count"]) + 1,
                        now,
                        now,
                        int(row["id"]),
                    ),
                )
                return int(row["id"])

            # ── 冲突检测 ─────────────────────────────────────────────────────
            # 同一类型下，如果已有记忆和新记忆有高重叠，说明这是更新旧信息，标记旧记忆失效。
            new_words = set(self._tokenize(normalized))
            candidate_rows = conn.execute(
                """
                SELECT id, memory FROM memories
                WHERE canonical_user_id=? AND memory_type=? AND is_active=1
                ORDER BY created_at DESC
                LIMIT 15
                """,
                (canonical_id, memory_type_safe),
            ).fetchall()

            deactivated = 0
            for cand in candidate_rows:
                cand_words = set(self._tokenize(str(cand["memory"])))
                overlap = len(new_words.intersection(cand_words))
                # 当超过一半关键词重叠，且新记忆 confidence >= 旧记忆时，标记旧记忆失效
                if overlap >= max(1, min(len(new_words), len(cand_words)) * 0.5):
                    conn.execute(
                        """
                        UPDATE memories SET is_active=0, updated_at=? WHERE id=?
                        """,
                        (now, int(cand["id"])),
                    )
                    deactivated += 1

            cur = conn.execute(
                """
                INSERT INTO memories(
                    canonical_user_id, source_adapter, source_user_id, source_channel, memory_type,
                    memory, memory_hash, score, importance, confidence, reinforce_count, is_active,
                    last_seen_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    adapter,
                    adapter_user,
                    source_channel,
                    memory_type_safe,
                    memory,
                    mhash,
                    self._clamp01(score),
                    self._clamp01(importance),
                    self._clamp01(confidence),
                    1,
                    1,
                    now,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid or 0)

            # 记录蒸馏创建事件，传入 conn 避免嵌套打开第二个写事务
            if deactivated > 0:
                self._log_memory_event(
                    canonical_user_id=canonical_id,
                    event_type="create_with_conflict",
                    payload={
                        "new_memory_id": new_id,
                        "memory_type": memory_type_safe,
                        "deactivated_count": deactivated,
                    },
                    conn=conn,
                )
            else:
                self._log_memory_event(
                    canonical_user_id=canonical_id,
                    event_type="create",
                    payload={
                        "memory_id": new_id,
                        "memory_type": memory_type_safe,
                    },
                    conn=conn,
                )

            return new_id

    def _delete_memory(self, memory_id: int) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            deleted = cur.rowcount > 0
            if deleted and row:
                self._log_memory_event(
                    canonical_user_id=str(row["canonical_user_id"]),
                    event_type="delete",
                    payload={"memory_id": memory_id},
                    conn=conn,
                )
            return deleted

    def _list_memories(
        self, canonical_id: str, limit: int = 8
    ) -> List[Dict[str, object]]:
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, updated_at
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
            }
            for r in rows
        ]

    def _retrieve_memories(
        self, canonical_id: str, query: str, limit: int
    ) -> List[Dict[str, object]]:
        """从 memories 表中检索最相关的记忆，按综合评分排序。只返回 is_active=1 的有效记忆。"""
        query_words = set(self._tokenize(query))
        now_ts = int(time.time())

        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence, reinforce_count, last_seen_at
                FROM memories
                WHERE canonical_user_id=? AND is_active=1
                ORDER BY updated_at DESC
                LIMIT 80
                """,
                (canonical_id,),
            ).fetchall()

        scored = []
        for row in rows:
            memory_text = str(row["memory"])
            memory_words = set(self._tokenize(memory_text))
            overlap = len(query_words.intersection(memory_words))
            lexical = overlap / max(1, len(query_words)) if query_words else 0.0

            recency_bonus = 0.0
            last_seen = str(row["last_seen_at"])
            try:
                last_ts = int(
                    time.mktime(time.strptime(last_seen, "%Y-%m-%d %H:%M:%S"))
                )
                age_hours = max(1.0, (now_ts - last_ts) / 3600)
                recency_bonus = min(0.15, 0.15 / age_hours)
            except Exception:
                pass

            final_score = (
                0.35 * float(row["score"])
                + 0.25 * float(row["importance"])
                + 0.20 * float(row["confidence"])
                + 0.15 * lexical
                + 0.05 * min(1.0, float(row["reinforce_count"]) / 10.0)
                + recency_bonus
            )

            scored.append(
                {
                    "id": int(row["id"]),
                    "memory_type": str(row["memory_type"]),
                    "memory": memory_text,
                    "final_score": float(final_score),
                }
            )

        scored.sort(key=lambda x: float(x["final_score"]), reverse=True)
        top_result = scored[:limit]

        # 对命中的 top 结果进行强化：reinforce_count += 1，批量更新减少 DB 开销
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

    # =========================================================================
    # 对话缓存
    # =========================================================================

    def _insert_conversation(
        self,
        canonical_id: str,
        role: str,
        content: str,
        source_adapter: str,
        source_user_id: str,
        unified_msg_origin: str,
    ):
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO conversation_cache(
                    canonical_user_id, role, content, source_adapter, source_user_id,
                    unified_msg_origin, distilled, distilled_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, 0, '', ?)
                """,
                (
                    canonical_id,
                    role,
                    content[:1000],
                    source_adapter,
                    source_user_id,
                    unified_msg_origin,
                    self._now(),
                ),
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
        min_required = self.distill_min_batch_count if min_batch_count is None else max(1, int(min_batch_count))
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
        """获取待蒸馏的对话行，返回 dict 列表（避免 sqlite3.Row 在 async 上下文中的问题）。"""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, canonical_user_id, role, content, source_adapter, source_user_id, unified_msg_origin
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
    # 上下文压缩（规则摘要，不触发 LLM）
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
        rows = self._fetch_recent_conversation(canonical_id, limit=200)
        if len(rows) <= self.cache_max_rows:
            return

        joined = " ".join([c for _, c in rows[: -self.cache_max_rows]])
        summary = self._distill_text(joined)
        now = self._now()

        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO conversation_cache(
                    canonical_user_id, role, content, source_adapter, source_user_id,
                    unified_msg_origin, distilled, distilled_at, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    canonical_id,
                    "summary",
                    f"[auto-summary] {summary}",
                    "system",
                    canonical_id,
                    "",
                    now,
                    now,
                ),
            )

        self._trim_conversation(canonical_id, keep_last=self.cache_max_rows)

    @filter.command("tm_export")
    async def tm_export(self, event: AstrMessageEvent):
        """导出当前用户的所有记忆（JSON）：/tm_export"""
        canonical_id, _, _ = self._resolve_current_identity(event)
        data = self._export_user_data(canonical_id)
        yield event.plain_result(json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    @filter.command("tm_purge")
    async def tm_purge(self, event: AstrMessageEvent):
        """删除当前用户的所有记忆和缓存：/tm_purge"""
        canonical_id, _, _ = self._resolve_current_identity(event)
        deleted = self._purge_user_data(canonical_id)
        yield event.plain_result(
            f"已清除 {canonical_id} 的所有数据：{deleted['memories']} 条记忆，{deleted['cache']} 条缓存。"
        )

    # =========================================================================
    # 蒸馏历史与健康监测
    # =========================================================================

    def _record_distill_history(
        self,
        started_at: str,
        trigger: str,
        users_processed: int,
        memories_created: int,
        users_failed: int,
        errors: list,
        duration: float,
    ):
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO distill_history(
                    started_at, finished_at, trigger_type, users_processed,
                    memories_created, users_failed, errors, duration_sec
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    self._now(),
                    trigger,
                    users_processed,
                    memories_created,
                    users_failed,
                    json.dumps(errors, ensure_ascii=False),
                    duration,
                ),
            )

    def _get_distill_history(self, limit: int = 20) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM distill_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # =========================================================================
    # 蒸馏输出校验器
    # =========================================================================

    def _validate_distill_output(self, items: List[Dict[str, object]]) -> List[Dict[str, object]]:
        """校验 LLM 蒸馏输出，过滤无效条目。"""
        valid = []
        for item in items:
            mem = str(item.get("memory", "")).strip()
            if not mem or len(mem) < 4:
                continue
            if len(mem) > 500:
                mem = mem[:500]
                item["memory"] = mem
            mtype = str(item.get("memory_type", ""))
            if mtype not in {"preference", "fact", "task", "restriction", "style"}:
                item["memory_type"] = self._infer_memory_type(mem)
            for field in ("score", "importance", "confidence"):
                try:
                    v = float(item.get(field, 0.5))
                    if not (0.0 <= v <= 1.0):
                        item[field] = max(0.0, min(1.0, v))
                except (TypeError, ValueError):
                    item[field] = 0.5
            valid.append(item)
        return valid

    # =========================================================================
    # 记忆生命周期衰减
    # =========================================================================

    def _decay_stale_memories(self):
        """将长期未命中的记忆标记为 stale（is_active=2），超久的归档（is_active=3）。"""
        now_ts = int(time.time())
        stale_threshold = 30 * 86400   # 30 天未命中 → stale
        archive_threshold = 90 * 86400  # 90 天未命中 → archived

        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, last_seen_at FROM memories WHERE is_active = 1"
            ).fetchall()
            for row in rows:
                try:
                    last_ts = int(time.mktime(time.strptime(str(row["last_seen_at"]), "%Y-%m-%d %H:%M:%S")))
                except Exception:
                    continue
                age = now_ts - last_ts
                if age > archive_threshold:
                    conn.execute("UPDATE memories SET is_active = 3 WHERE id = ?", (int(row["id"]),))
                elif age > stale_threshold:
                    conn.execute("UPDATE memories SET is_active = 2 WHERE id = ?", (int(row["id"]),))

    # =========================================================================
    # 数据导出与清除
    # =========================================================================

    def _export_user_data(self, canonical_id: str) -> Dict:
        memories = self._list_memories(canonical_id, limit=500)
        with self._db() as conn:
            bindings = [
                dict(r) for r in conn.execute(
                    "SELECT adapter, adapter_user_id FROM identity_bindings WHERE canonical_user_id = ?",
                    (canonical_id,),
                ).fetchall()
            ]
        return {
            "canonical_user_id": canonical_id,
            "memories": memories,
            "bindings": bindings,
            "exported_at": self._now(),
        }

    def _purge_user_data(self, canonical_id: str) -> Dict[str, int]:
        with self._db() as conn:
            m = conn.execute("DELETE FROM memories WHERE canonical_user_id = ?", (canonical_id,)).rowcount
            c = conn.execute("DELETE FROM conversation_cache WHERE canonical_user_id = ?", (canonical_id,)).rowcount
            conn.execute("DELETE FROM memory_events WHERE canonical_user_id = ?", (canonical_id,))
        self._log_memory_event(
            canonical_user_id=canonical_id,
            event_type="purge",
            payload={"memories_deleted": m, "cache_deleted": c},
        )
        return {"memories": m, "cache": c}

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

    def _distill_text(self, text: str) -> str:
        """规则蒸馏：提取关键词 + 截断，作为 LLM 蒸馏的 fallback。"""
        normalized = self._normalize_text(text)
        if not normalized:
            return "空白输入"

        words = [w for w in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if len(w) >= 2]
        top = [w for w, _ in Counter(words).most_common(5)]
        prefix = f"关键词: {'/'.join(top)}; " if top else ""
        short = normalized[: self.memory_max_chars]
        return f"{prefix}记忆: {short}"

    def _infer_memory_type(self, text: str) -> str:
        lowered = text.lower()
        if any(k in lowered for k in ["喜欢", "爱吃", "偏好", "习惯", "讨厌"]):
            return "preference"
        if any(k in lowered for k in ["计划", "待办", "要做", "提醒", "deadline"]):
            return "task"
        if any(k in lowered for k in ["不要", "禁止", "禁忌", "不能"]):
            return "restriction"
        if any(k in lowered for k in ["风格", "语气", "简洁", "详细"]):
            return "style"
        return "fact"

    def _tokenize(self, text: str) -> List[str]:
        normalized = self._normalize_text(text)
        return [
            w.lower()
            for w in re.split(r"[^\w\u4e00-\u9fff]+", normalized)
            if len(w) >= 2
        ]

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _safe_memory_type(self, value: object) -> str:
        s = str(value or "fact").strip().lower()
        if s in {"preference", "fact", "task", "restriction", "style"}:
            return s
        return self._infer_memory_type(s)

    def _clamp01(self, value: object) -> float:  # type: ignore[arg-type]
        try:
            num = float(value)  # type: ignore[arg-type]
        except Exception:
            num = 0.0
        return max(0.0, min(1.0, num))

    def _get_global_stats(self) -> Dict[str, int]:
        """获取全局统计信息。"""
        with self._db() as conn:
            total_users = conn.execute(
                "SELECT COUNT(DISTINCT canonical_user_id) FROM memories"
            ).fetchone()[0]
            active_memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE is_active = 1"
            ).fetchone()[0]
            deactivated_memories = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE is_active = 0"
            ).fetchone()[0]
            pending_cached = conn.execute(
                "SELECT COUNT(*) FROM conversation_cache WHERE distilled = 0"
            ).fetchone()[0]
            total_events = conn.execute(
                "SELECT COUNT(*) FROM memory_events"
            ).fetchone()[0]

        return {
            "total_users": int(total_users),
            "total_active_memories": int(active_memories),
            "total_deactivated_memories": int(deactivated_memories),
            "pending_cached_rows": int(pending_cached),
            "total_events": int(total_events),
        }
