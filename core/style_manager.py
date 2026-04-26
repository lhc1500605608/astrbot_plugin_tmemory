"""风格蒸馏 v3: 可复用人格档案 + 对话级绑定管理。

设计约束 (CTO review, ADR-006):
- style_bindings 用 UNIQUE(adapter_name, conversation_id)，每会话最多一个绑定
- 不建显式 default profile 记录；未绑定或 profile_id=NULL 回退 AstrBot 默认人格
- style_profiles 为可复用档案，不归属单个 conversation
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from core.db import DatabaseManager

_VALID_FIELDS = frozenset({"profile_name", "prompt_supplement", "description", "source_user", "source_adapter", "style_summary"})


def _build_default_prompt_supplement(style_summary: str) -> str:
    """从 style 记忆摘要生成默认的 prompt_supplement。"""
    if not style_summary:
        return ""
    return (
        "以下为用户沟通风格偏好，请据此调整回复方式:\n"
        + style_summary[:400]
    )


class StyleManager:
    """人格档案与对话风格绑定的数据访问层。"""

    def __init__(self, db_mgr: DatabaseManager) -> None:
        self._db_mgr = db_mgr

    def _db(self):
        return self._db_mgr.db()

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    # ── Profiles ───────────────────────────────────────────────────────

    def create_profile(
        self,
        profile_name: str,
        prompt_supplement: str = "",
        description: str = "",
        source_user: str = "",
        source_adapter: str = "",
        style_summary: str = "",
    ) -> int:
        now = self._now()
        with self._db() as conn:
            cur = conn.execute(
                """INSERT INTO style_profiles(
                       profile_name, prompt_supplement, description,
                       source_user, source_adapter, style_summary,
                       created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile_name, prompt_supplement, description,
                 source_user, source_adapter, style_summary,
                 now, now),
            )
            return int(cur.lastrowid or 0)

    def update_profile(self, profile_id: int, **kwargs) -> bool:
        updates = {k: v for k, v in kwargs.items() if k in _VALID_FIELDS}
        if not updates:
            return False
        updates["updated_at"] = self._now()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [profile_id]
        with self._db() as conn:
            cur = conn.execute(
                f"UPDATE style_profiles SET {set_clause} WHERE id=?",
                params,
            )
            return cur.rowcount > 0

    def delete_profile(self, profile_id: int) -> bool:
        with self._db() as conn:
            conn.execute(
                "UPDATE style_bindings SET profile_id=NULL, updated_at=? WHERE profile_id=?",
                (self._now(), profile_id),
            )
            cur = conn.execute("DELETE FROM style_profiles WHERE id=?", (profile_id,))
            return cur.rowcount > 0

    def get_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, profile_name, prompt_supplement, description, "
                "source_user, source_adapter, style_summary, created_at, updated_at "
                "FROM style_profiles WHERE id=?",
                (profile_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_profile_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, profile_name, prompt_supplement, description, "
                "source_user, source_adapter, style_summary, created_at, updated_at "
                "FROM style_profiles WHERE profile_name=?",
                (name,),
            ).fetchone()
        return dict(row) if row else None

    def list_profiles(self) -> List[Dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT id, profile_name, prompt_supplement, description, "
                "source_user, source_adapter, style_summary, created_at, updated_at "
                "FROM style_profiles ORDER BY profile_name"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Bindings ───────────────────────────────────────────────────────

    def get_binding(
        self, adapter_name: str, conversation_id: str
    ) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                """SELECT sb.id, sb.adapter_name, sb.conversation_id, sb.profile_id,
                          sp.profile_name, sp.prompt_supplement
                   FROM style_bindings sb
                   LEFT JOIN style_profiles sp ON sp.id = sb.profile_id
                   WHERE sb.adapter_name=? AND sb.conversation_id=?""",
                (adapter_name, conversation_id),
            ).fetchone()
        return dict(row) if row else None

    def set_binding(
        self, adapter_name: str, conversation_id: str, profile_id: int
    ) -> bool:
        now = self._now()
        with self._db() as conn:
            cur = conn.execute(
                """INSERT INTO style_bindings(adapter_name, conversation_id, profile_id, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(adapter_name, conversation_id)
                   DO UPDATE SET profile_id=excluded.profile_id, updated_at=excluded.updated_at""",
                (adapter_name, conversation_id, profile_id, now, now),
            )
            return cur.rowcount > 0

    def remove_binding(self, adapter_name: str, conversation_id: str) -> bool:
        with self._db() as conn:
            cur = conn.execute(
                "DELETE FROM style_bindings WHERE adapter_name=? AND conversation_id=?",
                (adapter_name, conversation_id),
            )
            return cur.rowcount > 0

    # ── Auto-create profile from distilled style memories ──────────────

    _AUTO_CREATE_THRESHOLD = 3

    def auto_create_profile_if_ready(
        self, canonical_user_id: str, source_adapter: str
    ) -> Optional[int]:
        """蒸馏完成后自动创建人格档案。

        当用户累积 >= _AUTO_CREATE_THRESHOLD 条活跃 style 记忆且尚未
        被任何 profile 覆盖时，自动生成一条以 canonical_user_id 命名的档案。
        """
        with self._db() as conn:
            # 统计活跃 style 记忆数
            row = conn.execute(
                "SELECT COUNT(1) AS n, GROUP_CONCAT(memory, '; ') AS summary "
                "FROM memories "
                "WHERE canonical_user_id=? AND memory_type='style' AND is_active=1",
                (canonical_user_id,),
            ).fetchone()
            if not row or int(row["n"]) < self._AUTO_CREATE_THRESHOLD:
                return None

            # 已有以此用户为 source 的档案则不再重复创建
            existing = conn.execute(
                "SELECT id FROM style_profiles WHERE source_user=?",
                (canonical_user_id,),
            ).fetchone()
            if existing:
                return None

        style_summary = str(row["summary"] or "")[:500]
        profile_name = f"{canonical_user_id}-auto-style"
        prompt_supplement = _build_default_prompt_supplement(style_summary)

        return self.create_profile(
            profile_name=profile_name,
            prompt_supplement=prompt_supplement,
            description=f"自动生成 ({canonical_user_id})",
            source_user=canonical_user_id,
            source_adapter=source_adapter,
            style_summary=style_summary,
        )

    def list_bindings(self) -> List[Dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                """SELECT sb.id, sb.adapter_name, sb.conversation_id, sb.profile_id,
                          sp.profile_name, sb.updated_at
                   FROM style_bindings sb
                   LEFT JOIN style_profiles sp ON sp.id = sb.profile_id
                   ORDER BY sb.adapter_name, sb.conversation_id"""
            ).fetchall()
        return [dict(r) for r in rows]
