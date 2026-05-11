"""AdminService — WebUI 管理用例的应用服务边界。

所有 WebUI 管理操作通过此类暴露，web_server.py 只做 HTTP 适配（request
parsing + response wrapping），不再直接执行 SQL 或调用 plugin 私有方法。

设计约束（ADR-005）：
- 输入输出使用普通 dict / list，不依赖 aiohttp。
- 不持有 HTTP / 浏览器相关状态。
- 通过构造函数注入依赖（db_manager, identity_mgr, memory_logger, cfg）。

模块拆分（ADR-009 / TMEAAA-345）：
- 纯工具函数和基类查询保留在此 facade 中。
- 记忆相关方法 → admin_memory_mixin.py
- 用户画像方法 → admin_profile_mixin.py
- 身份管理方法 → admin_identity_mixin.py
- 蒸馏与运维方法 → admin_distill_mixin.py
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from .admin_memory_mixin import AdminMemoryMixin
from .admin_profile_mixin import AdminProfileMixin
from .admin_identity_mixin import AdminIdentityMixin
from .admin_distill_mixin import AdminDistillMixin
from .utils_shared import (
    _VALID_FACET_TYPES,
    _VALID_ITEM_STATUSES,
    _VALID_MEMORY_TYPES,
    _clamp01,
    _normalize_text,
    _now,
    _safe_memory_type,
)

if TYPE_CHECKING:
    from core.db import DatabaseManager
    from core.config import PluginConfig
    from core.identity import IdentityManager
    from core.utils import MemoryLogger

logger = logging.getLogger("astrbot")


class AdminService(AdminMemoryMixin, AdminProfileMixin, AdminIdentityMixin, AdminDistillMixin):
    """WebUI 管理用例的应用服务。

    Parameters
    ----------
    plugin : TMemoryPlugin
        插件实例——仅用于透传给仍然需要 ``plugin`` 作为首参的 core/ 模块
        函数（如 ``maintenance.get_global_stats(plugin)``）。
        AdminService 本身只通过显式注入的依赖工作。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._db_mgr = plugin._db_mgr
        self._identity_mgr: IdentityManager = plugin._identity_mgr
        self._memory_logger: MemoryLogger = plugin._memory_logger
        self._cfg: PluginConfig = plugin._cfg

    # ── helpers ───────────────────────────────────────────────────────

    def _db(self):
        return self._db_mgr.db()

    # =====================================================================
    # 基类只读查询（跨领域通用查询）
    # =====================================================================

    def get_users(self) -> List[Dict[str, Any]]:
        """返回所有用户：合并 memories 和 conversation_cache 两张表。"""
        with self._db() as conn:
            mem_rows = conn.execute(
                "SELECT canonical_user_id, COUNT(*) as cnt "
                "FROM memories WHERE is_active = 1 "
                "GROUP BY canonical_user_id"
            ).fetchall()
            cache_rows = conn.execute(
                "SELECT canonical_user_id, COUNT(*) as cnt "
                "FROM conversation_cache WHERE distilled = 0 "
                "GROUP BY canonical_user_id"
            ).fetchall()

        merged: Dict[str, Dict[str, Any]] = {}
        for r in mem_rows:
            uid = str(r["canonical_user_id"])
            merged[uid] = {"id": uid, "memory_count": int(r["cnt"]), "pending_count": 0}
        for r in cache_rows:
            uid = str(r["canonical_user_id"])
            if uid in merged:
                merged[uid]["pending_count"] = int(r["cnt"])
            else:
                merged[uid] = {"id": uid, "memory_count": 0, "pending_count": int(r["cnt"])}

        return sorted(
            merged.values(),
            key=lambda u: u["memory_count"] + u["pending_count"],
            reverse=True,
        )

    def get_global_stats(self) -> Dict[str, Any]:
        """获取全局统计信息。"""
        from .maintenance import get_global_stats
        stats = get_global_stats(self._plugin)
        stats["pending_users"] = self.count_pending_users()
        return stats

    def count_pending_users(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS n FROM "
                "(SELECT canonical_user_id FROM conversation_cache "
                "WHERE distilled=0 GROUP BY canonical_user_id)"
            ).fetchone()
        return int(row["n"] if row else 0)

    def get_events(self, user: str) -> List[Dict[str, Any]]:
        """返回指定用户的审计事件列表。"""
        if not user:
            return []
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, event_type, payload_json, created_at "
                "FROM memory_events WHERE canonical_user_id = ? "
                "ORDER BY id DESC LIMIT 100",
                (user,),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "event_type": str(r["event_type"]),
                "payload_json": str(r["payload_json"]),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    def get_identities(self) -> List[Dict[str, Any]]:
        """返回所有身份绑定关系。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, adapter, adapter_user_id, canonical_user_id, updated_at "
                "FROM identity_bindings ORDER BY canonical_user_id, adapter"
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "adapter": str(r["adapter"]),
                "adapter_user_id": str(r["adapter_user_id"]),
                "canonical_user_id": str(r["canonical_user_id"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]
