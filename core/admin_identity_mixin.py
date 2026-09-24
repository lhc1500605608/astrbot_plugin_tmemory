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

    def _export_identity_map(self) -> None:
        """绑定类变更后全量刷新共享 identity_map.json（fail-closed，不并入业务事务）。"""
        from .identity_export import export_identity_map

        export_identity_map(self._db_mgr)

    def merge_users(self, from_id: str, to_id: str) -> int:
        """合并两个用户：将 from_user 的所有记忆和绑定迁移到 to_user。"""
        # 导出由 ``IdentityManager.merge_identity`` 在事务提交后统一触发。
        return self._identity_mgr.merge_identity(from_id, to_id)

    # ── 人物身份绑定（TMEAAA-540）──────────────────────────────────────────

    def bind_identity(
        self, adapter: str, adapter_user_id: str, canonical_user_id: str = ""
    ) -> dict[str, Any]:
        """新增/更新一条人物身份绑定，并写审计事件。

        - ``canonical_user_id`` 为空时按 ``adapter:adapter_user_id`` 建立新 Person。
        - 同一 ``(adapter, adapter_user_id)`` 重复调用为幂等 upsert。
        """
        from .memory_ops import log_memory_event
        from .utils_shared import _now

        adapter = str(adapter or "").strip()
        adapter_user_id = str(adapter_user_id or "").strip()
        if not adapter or not adapter_user_id:
            raise ValueError("adapter and adapter_user_id are required")
        canonical = str(canonical_user_id or "").strip() or f"{adapter}:{adapter_user_id}"

        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO identity_bindings(adapter, adapter_user_id, canonical_user_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(adapter, adapter_user_id)
                DO UPDATE SET canonical_user_id=excluded.canonical_user_id, updated_at=excluded.updated_at
                """,
                (adapter, adapter_user_id, canonical, _now()),
            )
            row = conn.execute(
                "SELECT id FROM identity_bindings WHERE adapter=? AND adapter_user_id=?",
                (adapter, adapter_user_id),
            ).fetchone()

        binding_id = int(row["id"]) if row else 0
        log_memory_event(
            self._plugin,
            canonical_user_id=canonical,
            event_type="bind",
            payload={
                "binding_id": binding_id,
                "adapter": adapter,
                "adapter_user_id": adapter_user_id,
                "canonical_user_id": canonical,
                "source": "admin",
            },
        )
        self._export_identity_map()
        return {
            "binding_id": binding_id,
            "adapter": adapter,
            "adapter_user_id": adapter_user_id,
            "canonical_user_id": canonical,
        }

    def unbind_identity(self, binding_id: int) -> dict[str, Any]:
        """解绑单条绑定：回退到该 ``adapter:user`` 自成 Person 的语义，写审计事件。

        幂等：重复解绑同一条绑定返回相同的自指 canonical 结果。
        """
        from .memory_ops import log_memory_event
        from .utils_shared import _now

        with self._db() as conn:
            row = conn.execute(
                "SELECT adapter, adapter_user_id, canonical_user_id "
                "FROM identity_bindings WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if not row:
                raise LookupError("binding not found")
            adapter = str(row["adapter"])
            adapter_user_id = str(row["adapter_user_id"])
            old_canonical = str(row["canonical_user_id"])
            self_canonical = f"{adapter}:{adapter_user_id}"
            conn.execute(
                "UPDATE identity_bindings SET canonical_user_id = ?, updated_at = ? WHERE id = ?",
                (self_canonical, _now(), binding_id),
            )

        log_memory_event(
            self._plugin,
            canonical_user_id=self_canonical,
            event_type="unbind",
            payload={
                "binding_id": binding_id,
                "adapter": adapter,
                "adapter_user_id": adapter_user_id,
                "old_canonical": old_canonical,
                "new_canonical": self_canonical,
            },
        )
        self._export_identity_map()
        return {
            "binding_id": binding_id,
            "adapter": adapter,
            "adapter_user_id": adapter_user_id,
            "old_canonical": old_canonical,
            "canonical_user_id": self_canonical,
        }

    def get_person_identities(self, include_suggestions: bool = False) -> dict[str, Any]:
        """返回 Person → 其多适配器身份绑定（含 display_name）。

        ``include_suggestions=True`` 时附带 ``suggestions[]``：某个
        ``(adapter, user)`` 所属 Person 的 display_name 与另一个既有 Person
        完全一致时提示「是否同一人」。**只提示，绝不自动绑定。**
        """
        with self._db() as conn:
            binding_rows = conn.execute(
                "SELECT id, adapter, adapter_user_id, canonical_user_id, updated_at "
                "FROM identity_bindings ORDER BY canonical_user_id, adapter"
            ).fetchall()
            profile_rows = conn.execute(
                "SELECT canonical_user_id, display_name FROM user_profiles"
            ).fetchall()

        display_names: dict[str, str] = {
            str(r["canonical_user_id"]): str(r["display_name"] or "")
            for r in profile_rows
        }

        persons: dict[str, dict[str, Any]] = {}
        for r in binding_rows:
            pid = str(r["canonical_user_id"])
            person = persons.setdefault(
                pid,
                {
                    "person_id": pid,
                    "display_name": display_names.get(pid, ""),
                    "bindings": [],
                },
            )
            person["bindings"].append(
                {
                    "binding_id": int(r["id"]),
                    "adapter": str(r["adapter"]),
                    "adapter_user_id": str(r["adapter_user_id"]),
                    "updated_at": str(r["updated_at"]),
                }
            )

        for pid, name in display_names.items():
            if pid not in persons:
                persons[pid] = {
                    "person_id": pid,
                    "display_name": name,
                    "bindings": [],
                }

        ordered = sorted(
            persons.values(), key=lambda p: (-len(p["bindings"]), p["person_id"])
        )
        result: dict[str, Any] = {"persons": ordered}
        if include_suggestions:
            result["suggestions"] = self._identity_suggestions(ordered)
        return result

    @staticmethod
    def _identity_suggestions(persons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """display_name 完全一致的 Person 提示（绝不自动绑定）。"""
        by_name: dict[str, list[str]] = {}
        for person in persons:
            name = str(person.get("display_name") or "")
            if name:
                by_name.setdefault(name, []).append(str(person["person_id"]))

        suggestions: list[dict[str, Any]] = []
        seen = set()
        for person in persons:
            name = str(person.get("display_name") or "")
            if not name:
                continue
            others = [
                pid for pid in by_name.get(name, []) if pid != person["person_id"]
            ]
            if not others:
                continue
            for binding in person.get("bindings", []):
                for other in others:
                    key = (binding["adapter"], binding["adapter_user_id"], other)
                    if key in seen:
                        continue
                    seen.add(key)
                    suggestions.append(
                        {
                            "display_name": name,
                            "adapter": binding["adapter"],
                            "adapter_user_id": binding["adapter_user_id"],
                            "binding_id": binding["binding_id"],
                            "person_id": person["person_id"],
                            "candidate_person_id": other,
                        }
                    )
        return suggestions

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
        self._export_identity_map()
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
