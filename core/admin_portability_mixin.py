"""AdminPortabilityMixin — 数据集导出 / 导入用例（Plan TMEAAA-379 B6）。

薄封装：把 ``core/portability.py`` 的纯函数接到 AdminService 依赖上。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import portability as _portability


class AdminPortabilityMixin:
    """数据导出 / 导入方法组。"""

    def export_dataset(self, users: Optional[List[str]] = None) -> Dict[str, Any]:
        """导出全部（或指定）用户的记忆与身份绑定为版本化信封。"""
        return _portability.build_export(self._plugin, users)

    def import_dataset(
        self,
        payload: Any,
        *,
        dry_run: bool = True,
        on_conflict: str = "skip",
        backup: bool = True,
    ) -> Dict[str, Any]:
        """导入记忆数据；默认 dry-run，落库前备份 + 事务回滚。"""
        return _portability.import_dataset(
            self._plugin,
            payload,
            dry_run=dry_run,
            on_conflict=on_conflict,
            backup=backup,
        )
