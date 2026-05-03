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
        """Merge all data from from_id into to_id within a single transaction.

        Follows ADR user-profile-model section 8 migration order.
        Returns the number of profile items merged.
        """
        now = self._now()
        profile_moved = 0
        legacy_moved = 0
        with self._db_mgr.db() as conn:
            # 1. Ensure user_profiles(to_id) exists
            conn.execute(
                "INSERT OR IGNORE INTO user_profiles(canonical_user_id, display_name, profile_version, summary_text, created_at, updated_at) VALUES(?, '', 1, '', ?, ?)",
                (to_id, now, now),
            )

            # 2. Update identity_bindings
            conn.execute(
                "UPDATE identity_bindings SET canonical_user_id=?, updated_at=? WHERE canonical_user_id=?",
                (to_id, now, from_id),
            )

            # 3. Update conversation_cache
            conn.execute(
                "UPDATE conversation_cache SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )

            # 4. Migrate profile_items with conflict resolution
            from_items = conn.execute(
                """
                SELECT * FROM profile_items WHERE canonical_user_id=?
                """,
                (from_id,),
            ).fetchall()

            for item in from_items:
                existing = conn.execute(
                    """
                    SELECT id, status, confidence, importance, stability, usage_count
                    FROM profile_items
                    WHERE canonical_user_id=? AND facet_type=? AND normalized_content=? AND persona_id=? AND source_scope=?
                    """,
                    (to_id, str(item["facet_type"]), str(item["normalized_content"]), str(item["persona_id"]), str(item["source_scope"])),
                ).fetchone()

                if existing:
                    survivor_id = int(existing["id"])
                    s_status = str(existing["status"])
                    i_status = str(item["status"])

                    # Merge evidence from incoming item to survivor
                    conn.execute(
                        "UPDATE profile_item_evidence SET profile_item_id=?, canonical_user_id=? WHERE profile_item_id=?",
                        (survivor_id, to_id, int(item["id"])),
                    )

                    # Merge scores (ADR 8.2 rule 3)
                    conn.execute(
                        """
                        UPDATE profile_items
                        SET confidence=MAX(confidence, ?),
                            importance=MAX(importance, ?),
                            stability=MAX(stability, ?),
                            usage_count=usage_count + ?,
                            last_used_at=CASE WHEN ? > last_used_at OR last_used_at='' THEN ? ELSE last_used_at END,
                            last_confirmed_at=CASE WHEN ? > last_confirmed_at OR last_confirmed_at='' THEN ? ELSE last_confirmed_at END,
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            float(item["confidence"]), float(item["importance"]), float(item["stability"]),
                            int(item["usage_count"]),
                            str(item["last_used_at"]), str(item["last_used_at"]),
                            str(item["last_confirmed_at"]), str(item["last_confirmed_at"]),
                            now, survivor_id,
                        ),
                    )

                    # Status conflict resolution (ADR 8.3)
                    resolved_status = self._resolve_merge_status(s_status, i_status)
                    if resolved_status != s_status:
                        conn.execute(
                            "UPDATE profile_items SET status=?, updated_at=? WHERE id=?",
                            (resolved_status, now, survivor_id),
                        )

                    # Delete incoming duplicate item
                    conn.execute("DELETE FROM profile_items WHERE id=?", (int(item["id"]),))
                else:
                    # No conflict: just reassign canonical_user_id
                    conn.execute(
                        "UPDATE profile_items SET canonical_user_id=? WHERE id=?",
                        (to_id, int(item["id"])),
                    )
                    profile_moved += 1

            # 5. Update evidence canonical_user_id for non-migrated evidence
            conn.execute(
                "UPDATE profile_item_evidence SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )

            # 6. Update profile_relations canonical_user_id and item refs
            conn.execute(
                "UPDATE profile_relations SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )

            # 7. Update memory_events
            conn.execute(
                "UPDATE memory_events SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )

            # 8. Update legacy tables for consistency (row-by-row for UNIQUE constraint)
            old_memories = conn.execute(
                "SELECT * FROM memories WHERE canonical_user_id=?",
                (from_id,),
            ).fetchall()
            for row in old_memories:
                try:
                    conn.execute(
                        """
                        INSERT INTO memories(
                            canonical_user_id, source_adapter, source_user_id, source_channel,
                            memory_type, summary_channel, memory, memory_hash, score, importance, confidence,
                            reinforce_count, attention_score, last_seen_at, created_at, updated_at, is_active, is_pinned, persona_id, scope
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            to_id,
                            row["source_adapter"], row["source_user_id"], row["source_channel"],
                            row["memory_type"], row["summary_channel"], row["memory"], row["memory_hash"],
                            row["score"], row["importance"], row["confidence"],
                            row["reinforce_count"], float(row["attention_score"] or 0.5),
                            row["last_seen_at"], now, now, row["is_active"], row["is_pinned"],
                            row["persona_id"], row["scope"],
                        ),
                    )
                    legacy_moved += 1
                except Exception:
                    conn.execute(
                        """
                        UPDATE memories
                        SET score=MAX(score, ?), importance=MAX(importance, ?),
                            confidence=MAX(confidence, ?), reinforce_count=reinforce_count+?,
                            attention_score=MAX(attention_score, ?)
                        WHERE canonical_user_id=? AND memory_hash=?
                        """,
                        (float(row["score"]), float(row["importance"]), float(row["confidence"]),
                         int(row["reinforce_count"] or 0), float(row["attention_score"] or 0.5),
                         to_id, row["memory_hash"]),
                    )
            conn.execute("DELETE FROM memories WHERE canonical_user_id=?", (from_id,))
            conn.execute(
                "UPDATE memory_episodes SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )
            conn.execute(
                "UPDATE episode_sources SET canonical_user_id=? WHERE canonical_user_id=?",
                (to_id, from_id),
            )

            # 9. Delete orphaned user_profiles(from_id)
            conn.execute("DELETE FROM user_profiles WHERE canonical_user_id=?", (from_id,))

        total_moved = profile_moved + legacy_moved
        self._memory_logger.log_memory_event(
            canonical_user_id=to_id,
            event_type="profile_identity_merged",
            payload={"from_id": from_id, "to_id": to_id, "profile_items": profile_moved, "legacy_memories": legacy_moved},
        )
        return total_moved

    @staticmethod
    def _resolve_merge_status(survivor: str, incoming: str) -> str:
        """Resolve status when merging duplicate profile items. ADR section 8.3."""
        if survivor == "active":
            return "active"  # survivor keeps active, incoming evidence merged
        if survivor == "contradicted":
            if incoming == "active":
                return "contradicted"  # conservative: keep contradicted
            return "contradicted"
        # survivor is superseded or archived
        if incoming == "active":
            return "active"  # restore: incoming brings fresh active evidence
        if incoming == "contradicted":
            return "contradicted"
        if incoming == "superseded":
            return "superseded"
        return "archived"
