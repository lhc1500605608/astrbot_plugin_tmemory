import time
from typing import Tuple, Dict, Any
from astrbot.api.event import AstrMessageEvent

class IdentityManager:
    def __init__(self, db_manager, cfg, memory_logger):
        self._db_mgr = db_manager
        self._cfg = cfg
        self._memory_logger = memory_logger

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    @staticmethod
    def _platform_str(val) -> str:
        try:
            from astrbot.core.platform.platform_metadata import PlatformMetadata
            if isinstance(val, PlatformMetadata):
                return val.id or val.name
        except ImportError:
            pass
        return str(val)

    def get_adapter_name(self, event: AstrMessageEvent) -> str:
        for attr in ["adapter_name", "platform", "platform_id"]:
            val = getattr(event, attr, None)
            if val:
                return self._platform_str(val)
        return "unknown"

    def get_adapter_user_id(self, event: AstrMessageEvent) -> str:
        val = event.get_sender_id()
        return str(val) if val else "unknown"

    def resolve_current_identity(self, event: AstrMessageEvent) -> Tuple[str, str, str]:
        adapter = self.get_adapter_name(event)
        adapter_user = self.get_adapter_user_id(event)

        with self._db_mgr.db() as conn:
            row = conn.execute(
                "SELECT canonical_user_id FROM identity_bindings WHERE adapter=? AND adapter_user_id=?",
                (adapter, adapter_user),
            ).fetchone()

        if row:
            return row["canonical_user_id"], adapter, adapter_user

        canonical = f"{adapter}:{adapter_user}"
        self.bind_identity(adapter, adapter_user, canonical)
        return canonical, adapter, adapter_user

    def bind_identity(self, adapter: str, adapter_user: str, canonical_id: str):
        now = self._now()
        with self._db_mgr.db() as conn:
            conn.execute(
                """
                INSERT INTO identity_bindings(adapter, adapter_user_id, canonical_user_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(adapter, adapter_user_id)
                DO UPDATE SET canonical_user_id=excluded.canonical_user_id, updated_at=excluded.updated_at
                """,
                (adapter, adapter_user, canonical_id, now),
            )
        self._memory_logger.log_memory_event(
            canonical_user_id=canonical_id,
            event_type="bind",
            payload={"adapter": adapter, "adapter_user_id": adapter_user},
        )

    def merge_identity(self, from_id: str, to_id: str) -> int:
        now = self._now()
        moved = 0
        with self._db_mgr.db() as conn:
            rows = conn.execute(
                """
                SELECT source_adapter, source_user_id, source_channel, memory_type, memory, memory_hash,
                       score, importance, confidence, reinforce_count, last_seen_at, is_active,
                       COALESCE(is_pinned, 0) AS is_pinned
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
                            reinforce_count, last_seen_at, created_at, updated_at, is_active, is_pinned
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            row["is_pinned"],
                        ),
                    )
                    moved += 1
                except Exception:
                    # Constraint error - merge by updating scores
                    conn.execute(
                        """
                        UPDATE memories 
                        SET score=MAX(score, ?), importance=MAX(importance, ?), 
                            confidence=MAX(confidence, ?), reinforce_count=reinforce_count+? 
                        WHERE canonical_user_id=? AND memory_hash=?
                        """,
                        (row["score"], row["importance"], row["confidence"], row["reinforce_count"] or 0, to_id, row["memory_hash"])
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
            
        self._memory_logger.log_memory_event(
            canonical_user_id=to_id,
            event_type="merge",
            payload={"from_id": from_id, "to_id": to_id, "moved_count": moved},
        )
        return moved
