"""AdminService — WebUI 管理用例的应用服务边界。

所有 WebUI 管理操作通过此类暴露，web_server.py 只做 HTTP 适配（request
parsing + response wrapping），不再直接执行 SQL 或调用 plugin 私有方法。

设计约束（ADR-005）：
- 输入输出使用普通 dict / list，不依赖 aiohttp。
- 不持有 HTTP / 浏览器相关状态。
- 通过构造函数注入依赖（db_manager, identity_mgr, memory_logger, cfg）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from core.db import DatabaseManager
    from core.config import PluginConfig
    from core.identity import IdentityManager
    from core.utils import MemoryLogger

logger = logging.getLogger("astrbot")

# ── 纯工具函数（无状态） ─────────────────────────────────────────────

_VALID_MEMORY_TYPES = frozenset({"preference", "fact", "task", "restriction", "style"})


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _safe_memory_type(value: object) -> str:
    s = str(value or "fact").strip().lower()
    return s if s in _VALID_MEMORY_TYPES else "fact"


def _clamp01(value: object) -> float:
    try:
        num = float(value)  # type: ignore[arg-type]
    except Exception:
        num = 0.0
    return max(0.0, min(1.0, num))


class AdminService:
    """WebUI 管理用例的应用服务。

    Parameters
    ----------
    plugin : TMemoryPlugin
        插件实例——仅用于透传给仍然需要 ``plugin`` 作为首参的 core/ 模块
        函数（如 ``maintenance.get_global_stats(plugin)``）。
        AdminService 本身只通过显式注入的依赖工作。
    """

    def __init__(self, plugin: Any) -> None:
        # 显式依赖——AdminService 只通过这些引用工作
        self._plugin = plugin
        self._db_mgr = plugin._db_mgr
        self._identity_mgr: IdentityManager = plugin._identity_mgr
        self._memory_logger: MemoryLogger = plugin._memory_logger
        self._cfg: PluginConfig = plugin._cfg

    # ── helpers ───────────────────────────────────────────────────────

    def _db(self):
        return self._db_mgr.db()

    # =====================================================================
    # Batch 1.1 — 只读查询
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

    def get_memories(self, user: str) -> List[Dict[str, Any]]:
        """返回指定用户的活跃记忆列表。"""
        if not user:
            return []
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence,
                       reinforce_count, is_active,
                       COALESCE(is_pinned, 0) AS is_pinned,
                       last_seen_at, created_at, updated_at
                FROM memories WHERE canonical_user_id = ? AND is_active = 1
                ORDER BY importance DESC, score DESC, updated_at DESC LIMIT 200
                """,
                (user,),
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
                "is_active": int(r["is_active"]),
                "is_pinned": int(r["is_pinned"]),
                "last_seen_at": str(r["last_seen_at"]),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

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

    def get_pending(self) -> List[Dict[str, Any]]:
        """返回待蒸馏队列详情。"""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT canonical_user_id, COUNT(*) as cnt, "
                "MIN(created_at) as oldest, MAX(created_at) as newest "
                "FROM conversation_cache WHERE distilled = 0 "
                "GROUP BY canonical_user_id ORDER BY cnt DESC LIMIT 100"
            ).fetchall()
        return [
            {
                "user": str(r["canonical_user_id"]),
                "count": int(r["cnt"]),
                "oldest": str(r["oldest"]),
                "newest": str(r["newest"]),
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

    def get_style_memories(self, user: str) -> List[Dict[str, Any]]:
        """返回指定用户的 style 类型记忆列表。"""
        if not user:
            return []
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT id, memory_type, memory, score, importance, confidence,
                       reinforce_count, is_active,
                       COALESCE(is_pinned, 0) AS is_pinned,
                       last_seen_at, created_at, updated_at
                FROM memories WHERE canonical_user_id = ? AND is_active = 1
                  AND memory_type = 'style'
                ORDER BY importance DESC, score DESC, updated_at DESC LIMIT 100
                """,
                (user,),
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
                "is_active": int(r["is_active"]),
                "is_pinned": int(r["is_pinned"]),
                "last_seen_at": str(r["last_seen_at"]),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]

    def get_style_stats(self) -> Dict[str, Any]:
        """返回聊天风格记忆的全局统计。"""
        with self._db() as conn:
            total = conn.execute(
                "SELECT COUNT(1) AS n FROM memories WHERE memory_type='style' AND is_active=1"
            ).fetchone()
            users = conn.execute(
                "SELECT COUNT(DISTINCT canonical_user_id) AS n FROM memories"
                " WHERE memory_type='style' AND is_active=1"
            ).fetchone()
            avg_conf = conn.execute(
                "SELECT AVG(confidence) AS v FROM memories"
                " WHERE memory_type='style' AND is_active=1"
            ).fetchone()
        return {
            "total_style_memories": int(total["n"] if total else 0),
            "style_users": int(users["n"] if users else 0),
            "avg_confidence": round(float(avg_conf["v"] if avg_conf and avg_conf["v"] else 0), 2),
        }

    def get_distill_history(self, limit: int = 30) -> List[Dict]:
        """返回蒸馏历史记录。"""
        from .distill_validator import get_distill_history
        return get_distill_history(self._plugin, limit=limit)

    # =====================================================================
    # Style Profiles & Bindings (v3)
    # =====================================================================

    def list_style_profiles(self) -> List[Dict[str, Any]]:
        return self._plugin._style_mgr.list_profiles()

    def get_style_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        return self._plugin._style_mgr.get_profile(profile_id)

    def create_style_profile(
        self, name: str, prompt_supplement: str, description: str = "",
        source_user: str = "", source_adapter: str = "", style_summary: str = "",
    ) -> Dict[str, Any]:
        existing = self._plugin._style_mgr.get_profile_by_name(name)
        if existing:
            return {"error": f"profile '{name}' already exists", "id": existing["id"]}
        pid = self._plugin._style_mgr.create_profile(
            name, prompt_supplement, description,
            source_user, source_adapter, style_summary,
        )
        return {"id": pid, "name": name}

    def update_style_profile(self, profile_id: int, **kwargs) -> bool:
        return self._plugin._style_mgr.update_profile(profile_id, **kwargs)

    def delete_style_profile(self, profile_id: int) -> bool:
        return self._plugin._style_mgr.delete_profile(profile_id)

    def list_style_bindings(self) -> List[Dict[str, Any]]:
        return self._plugin._style_mgr.list_bindings()

    def get_style_binding(
        self, adapter_name: str, conversation_id: str
    ) -> Optional[Dict[str, Any]]:
        return self._plugin._style_mgr.get_binding(adapter_name, conversation_id)

    def set_style_binding(
        self, adapter_name: str, conversation_id: str, profile_id: int
    ) -> bool:
        return self._plugin._style_mgr.set_binding(adapter_name, conversation_id, profile_id)

    def remove_style_binding(self, adapter_name: str, conversation_id: str) -> bool:
        return self._plugin._style_mgr.remove_binding(adapter_name, conversation_id)

    # =====================================================================
    # Batch 1.2 — 低风险写操作
    # =====================================================================

    def add_memory(
        self,
        user: str,
        memory: str,
        score: float = 0.7,
        memory_type: str = "fact",
        importance: float = 0.6,
        confidence: float = 0.7,
    ) -> int:
        """通过 WebUI 添加一条记忆。"""
        from .memory_ops import MemoryOps
        return MemoryOps(self._plugin).insert_memory(
            canonical_id=user,
            adapter="webui",
            adapter_user=user,
            memory=memory,
            score=score,
            memory_type=memory_type,
            importance=importance,
            confidence=confidence,
            source_channel="webui",
        )

    def update_memory(self, mem_id: int, data: Dict[str, Any]) -> None:
        """更新记忆字段（memory, memory_type, score, importance, confidence, is_pinned）。"""
        now = _now()
        fields: list = []
        params: list = []

        for col, key, conv in [
            ("memory", "memory", str),
            ("memory_type", "memory_type", lambda v: _safe_memory_type(v)),
            ("score", "score", lambda v: _clamp01(v)),
            ("importance", "importance", lambda v: _clamp01(v)),
            ("confidence", "confidence", lambda v: _clamp01(v)),
            ("is_pinned", "is_pinned", lambda v: 1 if v else 0),
        ]:
            if key in data:
                fields.append(f"{col} = ?")
                params.append(conv(data[key]))

        if not fields:
            raise ValueError("no fields to update")

        if "memory" in data:
            new_hash = hashlib.sha256(
                _normalize_text(str(data["memory"])).encode()
            ).hexdigest()
            fields.append("memory_hash = ?")
            params.append(new_hash)

        fields.append("updated_at = ?")
        params.append(now)
        params.append(mem_id)

        with self._db() as conn:
            conn.execute(
                f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", params
            )

        from .memory_ops import log_memory_event
        log_memory_event(
            self._plugin,
            canonical_user_id=str(data.get("user", "")),
            event_type="webui_update",
            payload={"memory_id": mem_id, "updated_fields": list(data.keys())},
        )

    def delete_memory(self, memory_id: int) -> bool:
        """删除一条记忆（含向量索引清理和审计）。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            deleted = cur.rowcount > 0
            if deleted and row:
                self._memory_logger.log_memory_event(
                    canonical_user_id=str(row["canonical_user_id"]),
                    event_type="delete",
                    payload={"memory_id": memory_id},
                    conn=conn,
                )
                from .vector import delete_vector
                delete_vector(self._plugin, memory_id, conn=conn)
            return deleted

    def set_pinned(self, memory_id: int, pinned: bool) -> bool:
        """设置/取消记忆常驻。"""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE memories SET is_pinned = ? WHERE id = ?",
                (1 if pinned else 0, memory_id),
            )
            return cur.rowcount > 0

    # =====================================================================
    # Batch 1.3 — 高风险写操作
    # =====================================================================

    async def trigger_distill(self) -> Dict[str, Any]:
        """手动触发蒸馏。"""
        from .memory_ops import MemoryOps
        processed_users, total_memories = await MemoryOps(self._plugin).run_distill_cycle(
            force=True, trigger="manual_web"
        )
        return {
            "processed_users": processed_users,
            "total_memories": total_memories,
        }

    def set_distill_pause(self, pause: bool) -> None:
        """暂停或恢复自动蒸馏。

        写入 ``_cfg.distill_pause``（distill worker 实际检查的位置）。
        """
        self._cfg.distill_pause = pause

    def merge_users(self, from_id: str, to_id: str) -> int:
        """合并两个用户：将 from_user 的所有记忆和绑定迁移到 to_user。"""
        return self._identity_mgr.merge_identity(from_id, to_id)

    def rebind_user(self, binding_id: int, new_canonical: str) -> Dict[str, str]:
        """将一个适配器账号改绑到新的统一用户 ID。

        Returns
        -------
        dict with ``old_canonical``, ``adapter``, ``adapter_user_id``
        """
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

    async def refine_memories(
        self,
        user: str,
        mode: str,
        limit: int,
        dry_run: bool,
        include_pinned: bool,
        extra_instruction: str,
        unified_msg_origin: str = "",
    ) -> Dict[str, Any]:
        """LLM 记忆精馏/提纯。"""
        mode = mode or self._cfg.manual_purify_default_mode or "both"
        limit = max(1, min(200, limit or self._cfg.manual_purify_default_limit or 20))

        class _Evt:
            pass
        _Evt.unified_msg_origin = unified_msg_origin  # type: ignore[attr-defined]

        from .memory_ops import MemoryOps
        return await MemoryOps(self._plugin).manual_purify_memories(
            event=_Evt(),  # type: ignore[arg-type]
            canonical_id=user,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra_instruction,
        )

    async def merge_memories(
        self, user: str, ids: List[int], merged_text: str = ""
    ) -> Dict[str, Any]:
        """合并多条记忆为一条。"""
        safe_ids = [int(i) for i in ids if str(i).isdigit()]
        rows = self._fetch_memories_by_ids(user, safe_ids)
        if len(rows) < 2:
            raise ValueError("not enough valid memory ids")

        if not merged_text:
            merged_text = self._auto_merge_memory_text(
                [str(r["memory"]) for r in rows]
            )

        keep_id = int(rows[0]["id"])
        self._update_memory_text(keep_id, merged_text)

        if getattr(self._plugin, "_vec_available", False):
            from .vector import upsert_vector
            await upsert_vector(self._plugin, keep_id, merged_text)

        deleted = 0
        for r in rows[1:]:
            if self.delete_memory(int(r["id"])):
                deleted += 1

        return {"keep_id": keep_id, "deleted": deleted}

    async def split_memory(
        self,
        user: str,
        memory_id: int,
        segments: Optional[List[str]] = None,
        unified_msg_origin: str = "",
    ) -> Dict[str, Any]:
        """拆分一条记忆为多条。"""
        row = self._fetch_memory_by_id(user, memory_id)
        if not row:
            raise LookupError("memory not found")

        if isinstance(segments, list):
            segs = [_normalize_text(str(s)) for s in segments if _normalize_text(str(s))]
        else:
            class _Evt:
                pass
            _Evt.unified_msg_origin = unified_msg_origin  # type: ignore[attr-defined]
            from .maintenance import llm_split_memory
            segs = await llm_split_memory(self._plugin, _Evt(), str(row["memory"]))  # type: ignore[arg-type]

        if len(segs) < 2:
            raise ValueError("segments < 2")

        self._update_memory_text(memory_id, segs[0])
        vec_available = getattr(self._plugin, "_vec_available", False)
        if vec_available:
            from .vector import upsert_vector
            await upsert_vector(self._plugin, memory_id, segs[0])

        from .memory_ops import MemoryOps
        ops = MemoryOps(self._plugin)
        added: List[int] = []
        for seg in segs[1:]:
            new_id = ops.insert_memory(
                canonical_id=user,
                adapter=str(row["source_adapter"]),
                adapter_user=str(row["source_user_id"]),
                memory=seg,
                score=float(row["score"]),
                memory_type=str(row["memory_type"]),
                importance=float(row["importance"]),
                confidence=float(row["confidence"]),
                source_channel="manual_split",
            )
            if vec_available and new_id:
                from .vector import upsert_vector
                await upsert_vector(self._plugin, new_id, seg)
            added.append(new_id)

        return {"base_id": memory_id, "added_ids": added}

    # ── 内部数据访问 ─────────────────────────────────────────────────

    def _fetch_memories_by_ids(
        self, canonical_id: str, ids: List[int]
    ) -> List[Dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join(["?"] * len(ids))
        with self._db() as conn:
            rows = conn.execute(
                f"SELECT id, memory, memory_type, score, importance, confidence, "
                f"source_adapter, source_user_id "
                f"FROM memories WHERE canonical_user_id=? AND id IN ({placeholders}) ORDER BY id",
                [canonical_id, *ids],
            ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_memory_by_id(
        self, canonical_id: str, memory_id: int
    ) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT id, canonical_user_id, source_adapter, source_user_id, "
                "memory, memory_type, score, importance, confidence "
                "FROM memories WHERE id=? AND canonical_user_id=?",
                (memory_id, canonical_id),
            ).fetchone()
        return dict(row) if row else None

    def _update_memory_text(self, memory_id: int, memory: str) -> None:
        now = _now()
        mhash = hashlib.sha256(
            _normalize_text(memory).encode("utf-8")
        ).hexdigest()
        try:
            import jieba
            tokenized_memory = " ".join(jieba.cut_for_search(memory))
        except ImportError:
            tokenized_memory = memory
        with self._db() as conn:
            conn.execute(
                "UPDATE memories SET memory=?, tokenized_memory=?, "
                "memory_hash=?, updated_at=? WHERE id=?",
                (memory, tokenized_memory, mhash, now, memory_id),
            )

    def _auto_merge_memory_text(self, memories: List[str]) -> str:
        """无 LLM 时的简单合并策略：去重后拼接。"""
        uniq: List[str] = []
        seen: set = set()
        for m in memories:
            n = _normalize_text(m)
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)
        if not uniq:
            return ""
        if len(uniq) == 1:
            return uniq[0]
        merged = ";".join(uniq)
        if not merged.startswith("用户"):
            merged = f"用户{merged}"
        return merged[:300]
