"""Profile item CRUD and state-machine operations.

Extracted from ``core/memory_ops.py`` per ADR-009 (module boundary split).
Physical move only — no behavior change.
"""

from typing import List

from .memory_events import log_memory_event

# ═══════════════════════════════════════════════════════════════════════════════
# Profile Item Operations (ADR user-profile-model)
# ═══════════════════════════════════════════════════════════════════════════════

class ProfileItemOps:
    """CRUD and state-machine operations for profile_items, evidence, and relations."""

    def __init__(self, plugin):
        self.plugin = plugin

    def _normalize_content(self, text: str) -> str:
        """Normalize content for unique-key matching: trim, collapse whitespace, lowercase ASCII."""
        import re
        t = text.strip()
        t = re.sub(r"\s+", " ", t)
        return t.lower()

    def _now(self) -> str:
        return self.plugin._now()

    # ── Upsert (reinforce on duplicate) ────────────────────────────────────

    def upsert_profile_item(
        self,
        canonical_id: str,
        facet_type: str,
        title: str,
        content: str,
        confidence: float,
        importance: float,
        source_scope: str = "user",
        persona_id: str = "",
    ) -> int:
        """Insert or reinforce a profile item. Returns the item id."""
        normalized = self._normalize_content(content)
        now = self._now()

        with self.plugin._db() as conn:
            row = conn.execute(
                """
                SELECT id, status, confidence, importance, stability, usage_count
                FROM profile_items
                WHERE canonical_user_id=? AND facet_type=? AND normalized_content=? AND persona_id=? AND source_scope=?
                """,
                (canonical_id, facet_type, normalized, persona_id, source_scope),
            ).fetchone()

            if row:
                existing_id = int(row["id"])
                existing_status = str(row["status"])

                if existing_status == "active":
                    conn.execute(
                        """
                        UPDATE profile_items
                        SET confidence=MAX(confidence, ?),
                            importance=MAX(importance, ?),
                            stability=MIN(1.0, stability + 0.05),
                            last_confirmed_at=?,
                            updated_at=?
                        WHERE id=?
                        """,
                        (confidence, importance, now, now, existing_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE profile_items
                        SET updated_at=?
                        WHERE id=?
                        """,
                        (now, existing_id),
                    )

                self._ensure_user_profile(conn, canonical_id, now)
                log_memory_event(
                    self.plugin, canonical_id,
                    "profile_item_reinforced",
                    {"profile_item_id": existing_id, "facet_type": facet_type},
                    conn=conn,
                )
                return existing_id

            # Insert new active item
            cur = conn.execute(
                """
                INSERT INTO profile_items(
                    canonical_user_id, facet_type, title, content, normalized_content,
                    status, confidence, importance, stability,
                    source_scope, persona_id, embedding_status,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    canonical_id, facet_type, title, content, normalized,
                    confidence, importance, self.plugin._cfg.profile_stability_default,
                    source_scope, persona_id,
                    now, now,
                ),
            )
            new_id = int(cur.lastrowid or 0)

            self._ensure_user_profile(conn, canonical_id, now)
            log_memory_event(
                self.plugin, canonical_id,
                "profile_item_created",
                {"profile_item_id": new_id, "facet_type": facet_type},
                conn=conn,
            )
            return new_id

    # ── Evidence ───────────────────────────────────────────────────────────

    def add_evidence(
        self,
        profile_item_id: int,
        canonical_user_id: str,
        source_ids: List[int],
        source_role: str = "user",
        evidence_kind: str = "conversation",
        confidence_delta: float = 0.0,
    ) -> None:
        """Add evidence rows linking profile item to conversation_cache sources."""
        now = self._now()
        with self.plugin._db() as conn:
            for cid in source_ids:
                conn.execute(
                    """
                    INSERT INTO profile_item_evidence(
                        profile_item_id, conversation_cache_id, canonical_user_id,
                        source_role, evidence_kind, confidence_delta, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (profile_item_id, int(cid), canonical_user_id, source_role, evidence_kind, confidence_delta, now),
                )

    # ── State transitions ──────────────────────────────────────────────────

    def supersede_item(self, item_id: int, superseded_by: int) -> bool:
        """Mark item as superseded by another item. Creates supersedes relation."""
        now = self._now()
        with self.plugin._db() as conn:
            conn.execute(
                "UPDATE profile_items SET status='superseded', updated_at=? WHERE id=? AND status='active'",
                (now, item_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO profile_relations(
                    canonical_user_id, from_item_id, to_item_id, relation_type, status, weight, created_at, updated_at
                ) VALUES(
                    (SELECT canonical_user_id FROM profile_items WHERE id=?),
                    ?, ?, 'supersedes', 'active', 1.0, ?, ?
                )
                """,
                (superseded_by, superseded_by, item_id, now, now),
            )
        log_memory_event(
            self.plugin,
            conn.execute("SELECT canonical_user_id FROM profile_items WHERE id=?", (item_id,)).fetchone()["canonical_user_id"],
            "profile_item_superseded",
            {"profile_item_id": item_id, "superseded_by": superseded_by},
        )
        return True

    def mark_contradicted(self, item_a: int, item_b: int, canonical_user_id: str) -> bool:
        """Mark two items as contradictory. Marks the newer one as contradicted, creates contradicts relation."""
        now = self._now()
        with self.plugin._db() as conn:
            conn.execute(
                "UPDATE profile_items SET status='contradicted', updated_at=? WHERE id=? AND status='active'",
                (now, item_b),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO profile_relations(
                    canonical_user_id, from_item_id, to_item_id, relation_type, status, weight, created_at, updated_at
                ) VALUES(?, ?, ?, 'contradicts', 'active', 0.5, ?, ?)
                """,
                (canonical_user_id, item_a, item_b, now, now),
            )
        log_memory_event(
            self.plugin, canonical_user_id,
            "profile_item_contradicted",
            {"item_a": item_a, "item_b": item_b},
        )
        return True

    def archive_item(self, item_id: int) -> bool:
        """Soft-delete: archive a profile item."""
        now = self._now()
        with self.plugin._db() as conn:
            conn.execute(
                "UPDATE profile_items SET status='archived', updated_at=? WHERE id=?",
                (now, item_id),
            )
            conn.execute(
                "UPDATE profile_relations SET status='archived', updated_at=? WHERE from_item_id=? OR to_item_id=?",
                (now, item_id, item_id),
            )
        row = self.plugin._db().conn.execute(
            "SELECT canonical_user_id FROM profile_items WHERE id=?", (item_id,)
        ).fetchone()
        if row:
            log_memory_event(
                self.plugin, str(row["canonical_user_id"]),
                "profile_item_archived",
                {"profile_item_id": item_id},
            )
        return True

    # ── Ensure aggregate root ──────────────────────────────────────────────

    def _ensure_user_profile(self, conn, canonical_id: str, now: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO user_profiles(canonical_user_id, display_name, profile_version, summary_text, created_at, updated_at)
            VALUES(?, '', 1, '', ?, ?)
            """,
            (canonical_id, now, now),
        )
        conn.execute(
            "UPDATE user_profiles SET updated_at=? WHERE canonical_user_id=?",
            (now, canonical_id),
        )
