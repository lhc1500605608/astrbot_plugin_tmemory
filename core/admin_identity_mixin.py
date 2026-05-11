"""AdminIdentityMixin — 身份绑定与用户管理操作。

所有方法通过 ``self._db_mgr`` / ``self._identity_mgr`` / ``self._plugin`` 访问共享状态。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from core.db import DatabaseManager
    from core.identity import IdentityManager

logger = logging.getLogger("astrbot")


class AdminIdentityMixin:
    """身份与用户管理方法组。

    包含用户合并、绑定迁移、数据导出与清除。
    """

    def merge_users(self, from_id: str, to_id: str) -> int:
        """合并两个用户：将 from_user 的所有记忆和绑定迁移到 to_user。"""
        return self._identity_mgr.merge_identity(from_id, to_id)

    def rebind_user(self, binding_id: int, new_canonical: str) -> Dict[str, str]:
        """将一个适配器账号改绑到新的统一用户 ID。

        Returns
        -------
        dict with ``old_canonical``, ``adapter``, ``adapter_user_id``
        """
        from .utils_shared import _now

        now = _now()
        with self._db() as conn:
            row = conn.execute(
                "SELECT adapter, adapter_user_id, canonical_user_id "
                "FROM identity_bindings WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if not row:
                raise LookupError("binding not found")
            old_canonical = str(row["canonical_user_id"])
            conn.execute(
                "UPDATE identity_bindings SET canonical_user_id = ?, updated_at = ? WHERE id = ?",
                (new_canonical, now, binding_id),
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin,
            canonical_user_id=new_canonical,
            event_type="rebind",
            payload={
                "binding_id": binding_id,
                "adapter": str(row["adapter"]),
                "adapter_user_id": str(row["adapter_user_id"]),
                "old_canonical": old_canonical,
                "new_canonical": new_canonical,
            },
        )
        return {
            "old_canonical": old_canonical,
            "adapter": str(row["adapter"]),
            "adapter_user_id": str(row["adapter_user_id"]),
        }

    def export_user(self, user: str) -> Dict:
        """导出用户数据。"""
        from .maintenance import export_user_data
        return export_user_data(self._plugin, user)

    def purge_user(self, user: str) -> Dict[str, int]:
        """清除用户全部数据。"""
        from .maintenance import purge_user_data
        return purge_user_data(self._plugin, user)
