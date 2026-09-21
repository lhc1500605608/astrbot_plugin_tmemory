from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import jieba

from ..adapters import config as _config_adapter
from ..adapters import conversation as _conversation_adapter
from ..adapters import event as _event_adapter
from ..adapters import llm as _llm_adapter
from ..adapters import version as _version_adapter
from . import maintenance as _maintenance
from . import memory_ops as _memory_ops
from . import vector as _vector
from .data_access import DataAccessMixin

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import ProviderRequest
    from .db import _LockedConnection

logger = logging.getLogger("astrbot")


class PluginHelpersMixin(DataAccessMixin):
    _TRANSCRIPT_PREFIX_RE = re.compile(
        r"^(user|assistant|summary)\s*:\s*", re.IGNORECASE | re.MULTILINE
    )

    async def _build_knowledge_injection(
        self,
        canonical_user_id: str,
        query: str,
        limit: int,
        scope: str = "user",
        persona_id: str = "",
        exclude_private: bool = False,
    ) -> str:
        items = self._retrieval_mgr.retrieve_profile_items(
            canonical_user_id, query, limit,
            scope=scope, persona_id=persona_id, exclude_private=exclude_private,
        )
        if not items:
            return ""

        from .injection import InjectionBuilder
        block = InjectionBuilder._assemble_profile_blocks(items)
        if self._cfg.inject_max_chars > 0 and len(block) > self._cfg.inject_max_chars:
            cutoff = max(self._cfg.inject_max_chars - 3, 1)
            block = block[:cutoff] + "\u2026"
        return block

    def _inject_block_by_position(self, req: ProviderRequest, block: str) -> None:
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
        elif self._cfg.inject_position == "extra_user_temp":
            if not _llm_adapter.inject_extra_user_temp(req, block):
                count = getattr(self, "_extra_user_temp_fallback_count", 0) + 1
                self._extra_user_temp_fallback_count = count
                message = (
                    "[tmemory] %s 已回退到 system_prompt 注入（第 %s 次；"
                    "UI 提示见 /api/capabilities）"
                )
                if count == 1:
                    logger.warning(
                        message, _version_adapter.extra_user_temp_hint(), count
                    )
                else:
                    logger.debug(
                        message, _version_adapter.extra_user_temp_hint(), count
                    )
                existing = getattr(req, "system_prompt", "") or ""
                req.system_prompt = existing + ("\n\n" if existing else "") + block
        else:  # system_prompt
            existing = getattr(req, "system_prompt", "") or ""
            req.system_prompt = existing + ("\n\n" if existing else "") + block

    async def build_memory_context(
        self, canonical_user_id: str, query: str, limit: int = 6
    ) -> str:
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
            memory_lines = ["- (none) \u6682\u65e0\u5339\u914d\u957f\u671f\u8bb0\u5fc6"]

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

    def _resolve_db_path(self) -> str:
        cwd = os.getcwd()
        candidates = []

        data_path = _config_adapter.get_astrbot_data_path()
        if data_path:
            candidates.append(
                os.path.join(data_path, "plugin_data", self.plugin_name)
            )

        candidates.extend(
            [
                os.path.join(cwd, "data", "plugin_data", self.plugin_name),
                os.path.join(cwd, "plugin_data", self.plugin_name),
            ]
        )

        for path in candidates:
            try:
                os.makedirs(path, exist_ok=True)
                return os.path.join(path, "tmemory.db")
            except OSError:
                continue

        raise RuntimeError(f"[tmemory] \u65e0\u6cd5\u521b\u5efa\u6301\u4e45\u5316\u6570\u636e\u76ee\u5f55\u3002\u5df2\u5c1d\u8bd5: {candidates}")

    def _load_sqlite_vec(self):
        """在启用向量检索时加载 sqlite-vec 扩展（缺失时禁用并把具体原因记入
        ``self._sqlite_env``，保留干净降级）。

        v0.10.0 回归修复：该加载逻辑在 TMEAAA-96 重构中丢失，导致
        ``_vec_available`` 恒为 False、向量索引路径完全失效。
        TMEAAA-475：探测/回退统一走 ``core.sqlite_env``，对外透出稳定原因码。
        """
        from .sqlite_env import probe_sqlite_environment

        self._sqlite_vec = None
        self._vec_available = False
        try:
            self._db_mgr.set_vec_extension(None)
        except Exception:
            pass

        report = probe_sqlite_environment()
        if not self._cfg.enable_vector_search:
            self._sqlite_env = report
            return

        if not report.sqlite_vec_installed:
            self._sqlite_env = report
            logger.warning(
                "[tmemory] sqlite-vec not installed; vector search disabled "
                "(reasons=%s). Run: pip install sqlite-vec",
                list(report.reasons),
            )
            return
        try:
            import sqlite_vec  # type: ignore[import-not-found]
        except ImportError:
            self._sqlite_env = report.with_reason("sqlite_vec_not_installed")
            logger.warning(
                "[tmemory] sqlite-vec import failed; vector search disabled. "
                "Run: pip install sqlite-vec"
            )
            return

        self._sqlite_vec = sqlite_vec
        # 模块可导入 ≠ 连接上 vec0 可用：注册后必须逐连接 load 并探测。
        self._db_mgr.set_vec_extension(sqlite_vec)
        with self._db() as conn:
            vec_ok = self._db_mgr.vec0_available(conn)
        report = report.with_vec0_available(vec_ok)
        # 合并 db.py 记录的具体原因（如 load_extension_missing）。
        for code in sorted(getattr(self._db_mgr, "vec_load_reasons", set()) or set()):
            report = report.with_reason(code)
        self._sqlite_env = report

        if vec_ok:
            self._vec_available = True
            logger.info("[tmemory] sqlite-vec loaded; vector search available")
        else:
            self._vec_available = False
            logger.warning(
                "[tmemory] sqlite-vec imported but vec0 module is not available on "
                "connection; vector search disabled (clean degradation, reasons=%s).",
                list(report.reasons),
            )

    def _init_db(self):
        self._db_mgr.init_db(self._vec_available, getattr(self._cfg, "embed_dim", 768))
        if self._vec_available and not getattr(self._db_mgr, "vec_enabled", False):
            self._vec_available = False

    def _migrate_schema(self, conn: Optional[sqlite3.Connection] = None):
        if conn is None:
            with self._db() as _conn:
                self._db_mgr.migrate_schema(_conn)
        else:
            self._db_mgr.migrate_schema(conn)

    def _db(self) -> _LockedConnection:
        return self._db_mgr.db()

    def _close_db(self) -> None:
        self._db_mgr.close()

    async def _get_http_session(self):
        return await _vector.get_http_session(self)

    async def _embed_text(self, text: str) -> Optional[List[float]]:
        return await _vector.embed_text(self, text)

    async def _upsert_vector(self, memory_id: int, text: str) -> bool:
        return await _vector.upsert_vector(self, memory_id, text)

    async def _upsert_profile_vector(self, profile_item_id: int, text: str) -> bool:
        return await _vector.upsert_profile_vector(self, profile_item_id, text)

    def _delete_vector(self, memory_id: int, conn=None) -> None:
        from . import vector as _vec

        _vec.delete_vector(self, memory_id, conn)

    def _delete_vectors_for_user(self, canonical_id: str, conn=None) -> None:
        from . import vector as _vec

        _vec.delete_vectors_for_user(self, canonical_id, conn)

    async def _rebuild_vector_index(self) -> Tuple[int, int]:
        return await _vector.rebuild_vector_index(self)

    def _embedding_status(self) -> Dict[str, object]:
        """embedding/rerank 来源快照；未启用向量检索时返回空 dict。"""
        if getattr(self, "_vector_manager", None) is None:
            return {}
        return _vector.embedding_status(self)

    def _log_memory_event(
        self,
        canonical_user_id: str,
        event_type: str,
        payload: Dict[str, object],
        conn: Optional[sqlite3.Connection] = None,
    ):
        _memory_ops.log_memory_event(self, canonical_user_id, event_type, payload, conn)

    def _safe_get_unified_msg_origin(self, event: AstrMessageEvent) -> str:
        return _event_adapter.get_unified_msg_origin(event)

    @staticmethod
    def _platform_str(val):
        return _event_adapter.get_platform_str(val)

    def _get_adapter_name(self, event: AstrMessageEvent) -> str:
        return _event_adapter.get_adapter_name(event)

    def _get_adapter_user_id(self, event: AstrMessageEvent) -> str:
        return _event_adapter.get_adapter_user_id(event)

    def _get_memory_scope(self, event: AstrMessageEvent) -> str:
        return _event_adapter.get_memory_scope(event, self._cfg.memory_scope)

    async def _get_current_persona_async(self, event: AstrMessageEvent) -> str:
        """优先经 conversation_manager（公共 API）解析 persona。

        conversation_manager 路径不可用/为空时，才退回事件私有字段兜底
        （``_get_current_persona``，实际读取收敛在 adapters/event.py），并计数打点。
        """
        umo = self._safe_get_unified_msg_origin(event)
        persona = await _conversation_adapter.get_current_persona_id(
            self.context, umo
        )
        if persona:
            return persona
        return self._get_current_persona(event)

    def _get_current_persona(self, event: AstrMessageEvent) -> str:
        """最后兜底：经 adapters/event 读取事件私有 conversation 字段并打点。"""
        persona = _event_adapter.get_current_persona(event)
        count = getattr(self, "_persona_private_fallback_count", 0) + 1
        self._persona_private_fallback_count = count
        logger.debug(
            "[tmemory] persona 经事件私有字段兜底解析（count=%s, hit=%s）",
            count,
            bool(persona),
        )
        return persona

    def _is_group_event(self, event: AstrMessageEvent) -> bool:
        return _event_adapter.is_group_event(event)

    def _build_sanitize_patterns(self) -> list:
        return [
            (re.compile(r"1[3-9]\d{9}"), "[\u624b\u673a\u53f7]"),
            (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[\u90ae\u7bb1]"),
            (re.compile(r"\d{17}[\dXx]"), "[\u8eab\u4efd\u8bc1]"),
            (re.compile(r"\d{15,19}"), "[\u957f\u6570\u5b57]"),
        ]

    def _sanitize_text(self, text: str) -> str:
        for pattern, replacement in self._sanitize_patterns:
            text = pattern.sub(replacement, text)
        return text

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

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
