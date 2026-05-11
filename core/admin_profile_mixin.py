"""AdminProfileMixin — 用户画像查询与写操作。

所有方法通过 ``self._db_mgr`` / ``self._plugin`` 等基类属性访问共享状态。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from core.db import DatabaseManager
    from core.config import PluginConfig

logger = logging.getLogger("astrbot")


class AdminProfileMixin:
    """用户画像管理方法组。

    包含画像概览、条目查询、证据链查询以及画像条目的增删改合并。
    """

    # =====================================================================
    # 只读查询
    # =====================================================================

    def get_profile_summary(self, user: str) -> Dict[str, Any]:
        """返回用户画像概览：profile 摘要 + 按 facet/status 统计。"""
        if not user:
            return {"user_profile": None, "facet_counts": {}, "status_counts": {}, "total_items": 0}
        with self._db() as conn:
            up = conn.execute(
                "SELECT * FROM user_profiles WHERE canonical_user_id=?",
                (user,),
            ).fetchone()
            facet_counts = {}
            for r in conn.execute(
                "SELECT facet_type, COUNT(*) as cnt FROM profile_items "
                "WHERE canonical_user_id=? AND status='active' GROUP BY facet_type",
                (user,),
            ).fetchall():
                facet_counts[str(r["facet_type"])] = int(r["cnt"])
            status_counts = {}
            for r in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM profile_items "
                "WHERE canonical_user_id=? GROUP BY status",
                (user,),
            ).fetchall():
                status_counts[str(r["status"])] = int(r["cnt"])
            total = conn.execute(
                "SELECT COUNT(*) FROM profile_items WHERE canonical_user_id=?",
                (user,),
            ).fetchone()
        return {
            "user_profile": {
                "canonical_user_id": str(up["canonical_user_id"]),
                "display_name": str(up["display_name"] or ""),
                "profile_version": int(up["profile_version"]),
                "summary_text": str(up["summary_text"] or ""),
                "created_at": str(up["created_at"]),
                "updated_at": str(up["updated_at"]),
            } if up else None,
            "facet_counts": facet_counts,
            "status_counts": status_counts,
            "total_items": int(total[0]) if total else 0,
        }

    def get_profile_items(
        self, user: str, facet_type: str = "", status: str = "active"
    ) -> List[Dict[str, Any]]:
        """返回用户画像条目列表，可筛选 facet_type 和 status。"""
        from .utils_shared import _VALID_FACET_TYPES, _VALID_ITEM_STATUSES

        if not user:
            return []
        where = ["canonical_user_id = ?"]
        params: list = [user]
        if facet_type and facet_type in _VALID_FACET_TYPES:
            where.append("facet_type = ?")
            params.append(facet_type)
        if status and status in _VALID_ITEM_STATUSES:
            where.append("status = ?")
            params.append(status)
        with self._db() as conn:
            rows = conn.execute(
                f"""
                SELECT pi.*,
                       (SELECT COUNT(*) FROM profile_item_evidence e WHERE e.profile_item_id=pi.id) AS evidence_count
                FROM profile_items pi
                WHERE {' AND '.join(where)}
                ORDER BY importance DESC, confidence DESC, updated_at DESC LIMIT 200
                """,
                params,
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "facet_type": str(r["facet_type"]),
                "title": str(r["title"] or ""),
                "content": str(r["content"]),
                "status": str(r["status"]),
                "confidence": float(r["confidence"]),
                "importance": float(r["importance"]),
                "stability": float(r["stability"] or 0.5),
                "usage_count": int(r["usage_count"] or 0),
                "last_used_at": str(r["last_used_at"] or ""),
                "last_confirmed_at": str(r["last_confirmed_at"] or ""),
                "source_scope": str(r["source_scope"] or "user"),
                "persona_id": str(r["persona_id"] or ""),
                "evidence_count": int(r["evidence_count"] or 0),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def get_profile_item_evidence(self, item_id: int) -> List[Dict[str, Any]]:
        """返回指定画像条目的证据链。"""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT e.*, cc.role, cc.content AS excerpt
                FROM profile_item_evidence e
                LEFT JOIN conversation_cache cc ON cc.id = e.conversation_cache_id
                WHERE e.profile_item_id = ?
                ORDER BY e.id LIMIT 50
                """,
                (item_id,),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "conversation_cache_id": int(r["conversation_cache_id"] or 0),
                "source_role": str(r["source_role"]),
                "source_excerpt": str(r["source_excerpt"] or ""),
                "evidence_kind": str(r["evidence_kind"]),
                "confidence_delta": float(r["confidence_delta"] or 0),
                "excerpt": str(r["excerpt"] or ""),
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    # =====================================================================
    # 写操作
    # =====================================================================

    def update_profile_item(self, item_id: int, data: Dict[str, Any]) -> None:
        """更新画像条目字段（title, content, facet_type, confidence, importance, status）。"""
        from .utils_shared import _now, _clamp01, _VALID_FACET_TYPES, _VALID_ITEM_STATUSES

        now = _now()
        fields: list = []
        params: list = []

        for col, key, conv in [
            ("title", "title", str),
            ("content", "content", str),
            ("facet_type", "facet_type", lambda v: v if v in _VALID_FACET_TYPES else None),
            ("confidence", "confidence", lambda v: _clamp01(v)),
            ("importance", "importance", lambda v: _clamp01(v)),
            ("status", "status", lambda v: v if v in _VALID_ITEM_STATUSES else None),
        ]:
            if key in data:
                val = conv(data[key])
                if val is not None:
                    fields.append(f"{col} = ?")
                    params.append(val)

        if not fields:
            raise ValueError("no fields to update")

        if "content" in data:
            normalized = re.sub(r"\s+", " ", str(data["content"]).strip()).lower()
            fields.append("normalized_content = ?")
            params.append(normalized)

        fields.append("updated_at = ?")
        params.append(now)
        params.append(item_id)

        with self._db() as conn:
            conn.execute(
                f"UPDATE profile_items SET {', '.join(fields)} WHERE id = ?", params
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin,
            canonical_user_id=str(data.get("user", "")),
            event_type="profile_item_update_webui",
            payload={"profile_item_id": item_id, "updated_fields": list(data.keys())},
        )

    def archive_profile_item(self, item_id: int) -> bool:
        """归档一条画像条目。"""
        from .memory_ops import ProfileItemOps
        return ProfileItemOps(self._plugin).archive_item(item_id)

    def merge_profile_items(self, user: str, ids: List[int]) -> Dict[str, Any]:
        """合并多条画像条目（同 facet_type + 同 profile item 语义合并）。

        保留 ids[0]，将其余条目的证据迁移过去，然后标记被合并条目为 superseded。
        """
        from .utils_shared import _now

        safe_ids = [int(i) for i in ids if str(i).isdigit()]
        if len(safe_ids) < 2:
            raise ValueError("need at least 2 profile item ids to merge")

        placeholders = ",".join(["?"] * len(safe_ids))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT id, content, facet_type "
                f"FROM profile_items WHERE id IN ({placeholders}) AND canonical_user_id=?",
                [*safe_ids, user],
            ).fetchall()

        if len(rows) < 2:
            raise ValueError("not enough valid profile items")

        rows_by_id = {int(r["id"]): r for r in rows}

        facet_types = {r["facet_type"] for r in rows}
        if len(facet_types) > 1:
            raise ValueError(
                f"cannot merge items with different facet types: {', '.join(sorted(facet_types))}"
            )

        keep_id = safe_ids[0]
        if keep_id not in rows_by_id:
            raise ValueError(f"keeper item {keep_id} not found")
        keep = rows_by_id[keep_id]
        merged_parts = [str(keep["content"])]

        now = _now()
        with self._db() as conn:
            for row in rows:
                merged_id = int(row["id"])
                if merged_id == keep_id:
                    continue
                merged_parts.append(str(row["content"]))
                conn.execute(
                    "UPDATE profile_item_evidence SET profile_item_id=? WHERE profile_item_id=?",
                    (keep_id, merged_id),
                )
                conn.execute(
                    "UPDATE profile_items SET status='superseded', updated_at=? WHERE id=? AND status='active'",
                    (now, merged_id),
                )
                conn.execute(
                    "UPDATE profile_relations SET status='archived', updated_at=? WHERE (from_item_id=? OR to_item_id=?)",
                    (now, merged_id, merged_id),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO profile_relations(canonical_user_id, from_item_id, to_item_id, relation_type, status, weight, created_at, updated_at)
                    VALUES(?, ?, ?, 'supersedes', 'active', 1.0, ?, ?)
                    """,
                    (user, keep_id, merged_id, now, now),
                )

            merged_text = "; ".join(merged_parts)
            normalized = re.sub(r"\s+", " ", merged_text).lower()
            conn.execute(
                "UPDATE profile_items SET content=?, normalized_content=?, updated_at=? WHERE id=?",
                (merged_text[:500], normalized, now, keep_id),
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin, user,
            "profile_items_merged_webui",
            {"keep_id": keep_id, "archived_ids": [int(r["id"]) for r in rows if int(r["id"]) != keep_id]},
        )

        return {"keep_id": keep_id, "archived_count": len(rows) - 1}
