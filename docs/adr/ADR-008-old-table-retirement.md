# ADR-008: Old Table Retirement Boundary

- Status: Accepted
- Date: 2026-05-08
- Deciders: CTO, Software Architect
- Related: `TMEAAA-312`, `TMEAAA-316`
- Supersedes: ADR-006 §Decision (product narrative); does NOT supersede ADR-006 schema migration history
- Depends on: ADR-007 (profile model baseline)

## Context

The repository currently maintains four tables that belong to previous architectural eras:

| Table | Introduced | Original Role | Current Status |
|-------|-----------|---------------|----------------|
| `conversations` | pre-v0.5.0 | Legacy flat conversation store | Dead — DDL only, zero active code paths |
| `memories` | pre-v0.5.0 | Semantic fact table (ADR-0001, ADR-006 center) | Active — still the primary fact store for legacy path |
| `memory_episodes` | v0.8.0 (ADR-006) | Episodic aggregation layer | Active — used by consolidation pipeline |
| `episode_sources` | v0.8.0 (ADR-006) | Episode↔conversation_cache link table | Active — used by consolidation pipeline |

The v0.8.3 profile model (ADR-007) introduced a new primary data path (`conversation_cache → profile_items`), but did not remove the old tables. The v0.9.0 roadmap (§3.2 R2) identifies this coexistence as an architectural risk: "路线图若继续同时推动三套概念，会放大维护成本."

This ADR defines the retirement boundary: what can be removed and when, grounded in actual code dependencies rather than abstract preference.

## Current Dependency Analysis

### `conversations` — Zero active code paths, but schema-init coupling

Complete dependency map across all `.py` files:

| File | Line(s) | Reference | Type |
|------|---------|-----------|------|
| `core/db.py` | 9–22 | `_DDL_CONVERSATIONS` — `CREATE TABLE IF NOT EXISTS conversations` | DDL definition |
| `core/db.py` | 482 | `conn.execute(_DDL_CONVERSATIONS)` in `init_db()` | Schema init |
| `core/db.py` | 585 | `CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (canonical_user_id, timestamp)` | Index — init_db |
| `core/db.py` | 586 | `CREATE INDEX IF NOT EXISTS idx_conversations_distilled ON conversations (distilled, timestamp)` | Index — init_db |
| `core/db.py` | 591 | `CREATE INDEX IF NOT EXISTS idx_conversations_scope_persona ON conversations (canonical_user_id, scope, persona_id)` | Index — init_db |
| `core/maintenance.py` | 365 | `DELETE FROM conversations WHERE canonical_user_id = ?` in `purge_user_data()` | Purge cleanup |
| `tests/test_admin_service.py` | 381 | `INSERT INTO conversations(canonical_user_id, role, content, timestamp) VALUES(?, ?, ?, ?)` | Test fixture |
| `tests/test_admin_service.py` | 396, 406 | `"conversations"` in purge table list and assertion | Test assertion |

No `INSERT` (production), no `SELECT`, no `UPDATE` in any application code. No WebUI reference.

**Critical schema-init concern**: If `_DDL_CONVERSATIONS` is removed but the three `CREATE INDEX IF NOT EXISTS idx_conversations_*` statements remain in `init_db()`, a fresh database initialization will fail with `sqlite3.OperationalError: no such table: conversations`. While `CREATE INDEX IF NOT EXISTS` checks for index name collision, it does NOT check for table existence — the statement fails if the target table is missing.

**Verdict: Dead code, but removal must be atomic — DDL definition, all three indexes, purge reference, and test fixtures must be removed together in a single change. Safe to do in v0.9.0.**

### `memories` — Heavily referenced, but superseded in product model

Active code paths (representative, not exhaustive):

| Module | Operations | Nature |
|--------|-----------|--------|
| `core/memory_ops.py` | Full CRUD: create, read, update, deactivate, reinforce | **Legacy path** — profile item ops exist in parallel (§515+) |
| `core/utils.py` | Delete, reinforce, edit, pin, retrieve for injection | **Mixed** — retrieval is active, but profile retrieval also exists |
| `core/admin_service.py` | WebUI CRUD: list, edit, delete, pin, merge | **Legacy UI** — profile workbench exists in parallel (`/api/profile/*`) |
| `core/consolidation.py:539` | Link memories to episode provenance | **Transitional** — bridges old semantic extraction to episode layer |
| `core/maintenance.py` | Decay, prune, purge, global stats | **Active** — stats still count from memories table |
| `core/vector.py:162,178` | Vector retrieval from `memories` | **Active** — vector search still queries memories |
| `hybrid_search.py:48,124,138` | FTS5 search defaults to `memories_fts` | **Active** — search infrastructure tied to memories table name |
| `search/retrieval.py` | FTS5 and keyword search against `memories` | **Active** — dual retrieval path (memories + profile_items) |
| `core/identity.py:187-224` | User merge: SELECT, INSERT, UPDATE, DELETE on memories | **Active** — identity merge still operates on memories |

Supporting infrastructure:
- FTS5 virtual table (`memories_fts`) with 3 triggers (AI, AD, AU)
- Schema migration logic (18 additive columns in `core/db.py:362-396`)
- jieba tokenization backfill (`core/db.py:547-565`)
- Vector table (`memory_vectors`) keyed to `memories.id`

**Verdict: Cannot retire in v0.9.0. Too many active code paths. Retirement requires a phased migration of each dependency to the profile model.**

### `memory_episodes` — Active consolidation pipeline

| Module | Operations | Nature |
|--------|-----------|--------|
| `core/consolidation.py` | EpisodeManager: create, read, update status | **Active** — core consolidation loop |
| `core/admin_service.py:187-194` | WebUI mindmap projection reads episodes | **Active UI** |
| `core/identity.py:226` | User merge updates canonical_user_id | **Active** |
| `core/maintenance.py:409-418` | Global stats counting | **Active** |
| `search/retrieval.py:208-269` | Episode layer retrieval | **Active** |

Supporting infrastructure:
- FTS5 virtual table (`memory_episodes_fts`) with 3 triggers
- 5 indexes on the physical table
- Schema migration history (attention_score column addition)

**Verdict: Cannot retire in v0.9.0. The consolidation pipeline still depends on it. Retirement requires re-architecting the consolidation path to write directly to profile_items.**

### `episode_sources` — Provenance link for episodes

| Module | Operations | Nature |
|--------|-----------|--------|
| `core/consolidation.py` | Insert sources, fetch sources for episodes | **Active** |
| `core/identity.py:230` | User merge updates canonical_user_id | **Active** |

Supporting infrastructure:
- 3 indexes
- Schema migration history (auto-increment → compound PK migration)

**Verdict: Cannot retire independently of `memory_episodes`. Retires when episodes retire.**

## Decision

### Retirement Tiers

#### Tier 1: Immediate Removal in v0.9.0

**`conversations` table** — remove DDL, indexes, purge reference, and test fixtures atomically.

Rationale:
- Zero production read/write paths. The only `INSERT` is in a test fixture.
- Three indexes on `conversations` exist in `init_db()` and must be removed together with the DDL, otherwise fresh database initialization will fail with `sqlite3.OperationalError: no such table: conversations`.
- No data migration needed (no production code writes to it).
- Removal is a pure cleanup with no behavioral change.
- Reversible: the DDL and indexes can be re-added if needed (no data loss risk).

Actions in v0.9.0 (must be a single atomic change):
1. Remove `_DDL_CONVERSATIONS` constant from `core/db.py` (lines 9–22).
2. Remove `conn.execute(_DDL_CONVERSATIONS)` from `init_db()` (line 482).
3. Remove line 585: `CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (canonical_user_id, timestamp)`.
4. Remove line 586: `CREATE INDEX IF NOT EXISTS idx_conversations_distilled ON conversations (distilled, timestamp)`.
5. Remove line 591: `CREATE INDEX IF NOT EXISTS idx_conversations_scope_persona ON conversations (canonical_user_id, scope, persona_id)`.
6. Remove `DELETE FROM conversations WHERE canonical_user_id = ?` from `core/maintenance.py:purge_user_data()` (line 365).
7. Update `tests/test_admin_service.py`: remove `INSERT INTO conversations` (line 381), remove `"conversations"` from the purge table list (line 396), and remove the corresponding assertion (line 406).
8. No runtime migration needed — existing databases keep the table and indexes; new databases simply won't create them.

#### Tier 2: Mark Deprecated, Retire in v0.9.1

**None.** All three remaining tables have active dependencies that cannot be resolved in a single release cycle.

#### Tier 3: Retire in v0.9.1 (Two-Way Door, Requires Per-Dependency Migration)

**`memories` table** — phased retirement across v0.9.1.

This is the most complex retirement. Each dependency must be migrated:

| Dependency | Migration Target | Effort |
|-----------|-----------------|--------|
| `core/memory_ops.py` CRUD | Route through profile item ops; keep memories as read-only legacy view during transition | Medium |
| `core/utils.py` retrieval/injection | Switch `retrieve_memories()` callers to `retrieve_profile_items()` | Medium |
| `core/admin_service.py` WebUI | Migrate `/api/memories` to profile workbench; add legacy read-only mode | High |
| `core/maintenance.py` decay/prune/stats | Re-target to profile_items (decay on `stability`, stats from profile tables) | Medium |
| `core/vector.py` / `hybrid_search.py` / `search/retrieval.py` | Switch search defaults to `profile_items_fts` + `profile_item_vectors` | Medium |
| `core/identity.py` user merge | Extend merge to profile tables (already partially done) | Low |
| FTS5 + triggers + jieba backfill | Replace with `profile_items_fts` equivalents (already partially exist) | Low |

**`memory_episodes` + `episode_sources`** — retire together in v0.9.1.

The consolidation pipeline must be re-architected to write directly to `profile_items` + `profile_item_evidence`, eliminating the intermediate episode aggregation layer. This is the architectural conclusion of ADR-007: if `profile_items` is the primary domain model, the intermediate episode layer is unnecessary indirection.

| Dependency | Migration Target | Effort |
|-----------|-----------------|--------|
| `core/consolidation.py` EpisodeManager | Rewrite to produce `profile_items` directly from `conversation_cache` | High |
| `core/admin_service.py` mindmap | Project profile_items + evidence chain instead of episodes | Medium |
| `search/retrieval.py` episode retrieval | Already redundant with profile item retrieval; remove | Low |
| FTS5 + triggers + indexes | Remove | Low |
| `core/identity.py` merge | Remove episode/source merge logic | Low |

#### Tier 4: Post-v1.0.0 (One-Way Door)

No old table retirements are deferred past v1.0.0. All remaining retirements are two-way doors that should be completed before the v1.0.0 feature expansion begins. The roadmap (§5.4) explicitly gates graph/relationship work on main pipeline convergence.

### Explicit v0.9.0 Recommendation

**Do NOT retire `memories`, `memory_episodes`, or `episode_sources` in v0.9.0.**

Reasoning:
1. v0.9.0 is defined as a "quality and convergence" release, not a rewrite release (§5.2).
2. Each table retirement requires code changes across 5+ modules with corresponding test updates.
3. The P0 priorities (P0-1 through P0-7) already represent 12-19 days of work.
4. Attempting table retirement in parallel with P0 hardening would violate the roadmap principle: "先收敛主链路，再追求新能力."
5. The profile model (ADR-007) can serve as the stable baseline while old tables remain as internal implementation layers — this is the documented coexistence model until v0.9.1.

**DO remove `conversations` DDL, all three indexes, purge reference, and test fixtures in v0.9.0.** The removal must be atomic (all 8 references removed together) to avoid schema-init failure on fresh databases. Zero production risk — no active read/write paths exist.

### Migration Sequence (v0.9.1)

```
1. Profile extraction rewrite: conversation_cache → profile_items directly
   (eliminates need for intermediate episode layer)
2. Profile retrieval becomes sole retrieval path
   (eliminates need for memories in injection hot path)
3. Maintenance/stats re-target to profile tables
4. WebUI legacy /api/memories → read-only mode → removal
5. identity merge covers profile tables (verify, then remove old table merge)
6. Drop old tables: memory_episodes, episode_sources, memories
7. Remove associated FTS5, triggers, indexes, migration code
```

## Consequences

### Positive

- Eliminates ambiguity about what's "current" vs "legacy" in the codebase.
- Provides a clear, dependency-grounded migration sequence rather than abstract preferences.
- Prevents premature retirement that would destabilize active code paths.
- The immediate `conversations` removal (DDL + 3 indexes + purge + test fixtures) reduces noise in schema initialization and eliminates a hidden footgun where index creation would fail on fresh databases.

### Negative

- Three old tables remain in the codebase through v0.9.x, adding to the "coexistence debt" identified in the roadmap audit (R2).
- The v0.9.1 retirement work is non-trivial (estimated 5-8 days across all dependencies).
- During the transition, developers must understand both old and new data paths.

### Risk: Profile extraction rewrite may be larger than estimated

The consolidation pipeline (`core/consolidation.py`, 1016 lines) mixes episode logic with profile extraction logic. Untangling them for direct `conversation_cache → profile_items` extraction may reveal hidden coupling. **Mitigation**: The v0.9.0 P0-4 complexity re-split should explicitly separate episode logic from profile extraction logic as preparation.

### Risk: Some deployments may depend on old table data

If any users have built external tooling on the `memories` table schema, retirement would break their integrations. **Mitigation**: The v0.9.1 retirement must include a read-only compatibility window (at least one release cycle) with documented deprecation warnings.

## Reversibility

- **`conversations` removal (DDL + 3 indexes)**: Fully reversible. Re-add DDL and indexes with no data loss (nothing was writing to it).
- **`memories` retirement**: Reversible within the compatibility window. The table can be retained as read-only indefinitely.
- **`memory_episodes` + `episode_sources` retirement**: Reversible with data migration from profile_items back to episode aggregates, but practically unlikely to be needed once the direct extraction path is proven.

All retirements in this ADR are **two-way doors** — no data is destroyed, only code paths are redirected. The one-way door (v1.0.0 graph layer) is explicitly excluded from this ADR's scope.

## References

- `core/db.py:9-23` — `_DDL_CONVERSATIONS`
- `core/db.py:482` — `conn.execute(_DDL_CONVERSATIONS)` in `init_db()`
- `core/db.py:585` — `CREATE INDEX idx_conversations_user ON conversations`
- `core/db.py:586` — `CREATE INDEX idx_conversations_distilled ON conversations`
- `core/db.py:591` — `CREATE INDEX idx_conversations_scope_persona ON conversations`
- `core/db.py:25-56` — `_DDL_MEMORIES`
- `core/db.py:97-119` — `_DDL_MEMORY_EPISODES`
- `core/db.py:121-131` — `_DDL_EPISODE_SOURCES`
- `core/maintenance.py:350-377` — `purge_user_data()` covering all four tables
- `core/consolidation.py:304-631` — EpisodeManager with active episode/source read/write
- `core/injection.py:51` — profile item retrieval as primary injection path
- ADR-006 — three-tier model definition (now superseded in product narrative)
- ADR-007 — user profile model as stable baseline
- Roadmap v0.9.0 §3.2 R2 (coexistence risk), §5.3 P1-1 (old table cleanup strategy)
