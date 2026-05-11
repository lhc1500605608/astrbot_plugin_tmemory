# ADR-007: User Profile Model as Stable Product Baseline

- Status: Accepted
- Date: 2026-05-08
- Deciders: CTO, Software Architect
- Related: `TMEAAA-312`, `TMEAAA-316`
- Supersedes: ADR-006 three-tier model (product narrative only; ADR-006 schema decisions remain as implementation history)

## Context

Since v0.8.3 (2026-05-04), the product has shifted its core data model from the three-tier `memories`/`memory_episodes`/`conversation_cache` architecture to a user-profile model centered on `profile_items`. This shift happened via implementation without a corresponding ADR, leaving the codebase referencing a non-existent "ADR user-profile-model" (`core/db.py:155`, `core/memory_ops.py:515`, `core/identity.py:73`).

The v0.9.0 roadmap (2026-05-08) declares: "可以把画像模型视为 v0.9.x 的稳定产品基线" and "对外必须统一为'用户画像长期记忆插件'；旧 memories / episodes 只允许作为兼容层或内部实现层存在."

This ADR formalizes the profile model as the stable baseline, closing the gap between code reality and architectural documentation.

## Decision Drivers

- The profile model is already in production (v0.8.3–v0.8.5), with dedicated tests and WebUI management.
- External communication must consistently present "user profile memory" as the product model.
- Future capability expansion (relations/graph, active tools, style) must center on profile_items, not old memories.
- Old tables must not be allowed to drift into competing product narratives.

## Options Considered

### Option A: Keep the three-tier narrative and treat profiles as a "view"

Keep ADR-006's Working/Episodic/Semantic narrative as the product model, positioning profile_items as a derived presentation layer over memories.

**Rejected.** The profile model already has its own ingestion path (`conversation_cache → profile extraction → profile_items`), its own retrieval path (`retrieve_profile_items`), and its own WebUI workbench. It is not a view over memories — it has replaced memories as the primary write/read path.

### Option B: Formalize the profile model as the stable baseline (chosen)

Declare the profile model as the architecture's primary domain model. Old tables become internal implementation layers, compatibility shims, or candidates for retirement.

**Accepted.** This matches the actual code reality and the v0.9.0 roadmap direction.

### Option C: Defer the decision and let both models coexist

Continue the status quo of implicit coexistence without documented boundaries.

**Rejected.** The roadmap explicitly warns against "同时推进两套产品叙事" (R2 in §3.2). Prolonged ambiguity increases maintenance cost and confuses new contributors.

## Decision

The **User Profile Model** is the stable product baseline for v0.9.x and beyond. It consists of four tables:

### Aggregate Root: `user_profiles`

```text
user_profiles (canonical_user_id PK, display_name, profile_version, summary_text)
```

One row per canonical user. Serves as the aggregate root for all profile items belonging to that user.

### Core Entity: `profile_items`

```text
profile_items (id PK, canonical_user_id, facet_type, title, content, normalized_content,
               status, confidence, importance, stability, usage_count,
               last_used_at, last_confirmed_at, source_scope, persona_id,
               embedding_status, created_at, updated_at)
```

Five facet types: `preference`, `fact`, `style`, `restriction`, `task_pattern`.

Four statuses: `active`, `superseded`, `contradicted`, `archived`.

This is the primary unit of memory storage, retrieval, and injection. Each item is a single, verifiable claim about a user, backed by evidence.

### Evidence Chain: `profile_item_evidence`

```text
profile_item_evidence (id PK, profile_item_id FK, conversation_cache_id,
                       canonical_user_id, source_excerpt, source_role,
                       source_timestamp, evidence_kind, confidence_delta, created_at)
```

Every profile item can trace its origin to specific conversation turns (`conversation_cache` rows), manual input, imports, or merges. Evidence kinds: `conversation`, `manual`, `import`, `merge`.

### Relationship Layer: `profile_relations`

```text
profile_relations (id PK, canonical_user_id, from_item_id FK, to_item_id FK,
                   relation_type, status, weight, created_at, updated_at)
```

Five relation types: `supports`, `contradicts`, `depends_on`, `context_for`, `supersedes`.

This is the foundation for future graph/relationship capabilities (deferred to v1.0.0 per ADR-0001 revision).

### Active Data Flow

```text
conversation_cache (ingestion)
  → profile extraction / distill runtime (core/consolidation.py ProfileExtractionRuntimeMixin)
  → profile_items + profile_item_evidence + profile_relations (storage)
  → retrieve_profile_items (retrieval, core/injection.py)
  → prompt injection (output)
  → WebUI profile workbench (management, web_server.py /api/profile/*)
```

### What This ADR Does NOT Cover

- The retirement timeline for `memories`, `memory_episodes`, `episode_sources`, `conversations` — see ADR-008.
- The graph/relationship layer expansion — deferred to v1.0.0 per roadmap §5.4 P2-1.
- The `style_distill` plugin integration — remains decoupled per roadmap §6.

## Consequences

### Positive

- Closes the documentation gap: code references to "ADR user-profile-model" now point to a real document.
- Provides a clear architectural anchor for v0.9.0 quality/convergence work.
- Prevents the "two competing product narratives" risk identified in the roadmap audit.
- Gives future ADRs (graph layer, active tools) a stable domain model to build on.

### Negative

- The `profile_items` schema has 5 facet types and 4 statuses — the state machine needs disciplined maintenance to avoid semantic drift (similar to ADR-006's warning about `memory_episodes`).
- Evidence chain integrity depends on `conversation_cache` rows not being prematurely purged.

### Reversibility

- This is a **two-way door**: the profile model could theoretically be migrated to a different schema. However, since it is already the active write/read path in production, reversal would require a full data migration — making it practically closer to a one-way door.
- The key reversible aspect is that `profile_items` can coexist with `memories` during any transition period.

## Validation Criteria

The following must hold true for this ADR to be considered valid:

1. `profile_items` + `profile_item_evidence` are the primary write target for new memory extraction.
2. `retrieve_profile_items()` is the primary retrieval path for prompt injection.
3. WebUI profile workbench (`/api/profile/*`) is the primary management interface.
4. `tests/test_profile_storage.py` and `tests/test_profile_admin_api.py` pass consistently.
5. New features are designed against `profile_items`, not `memories`.

## References

- Roadmap v0.9.0 §2.3 (current main pipeline), §3.1 (stable baseline assessment)
- `core/db.py:155-224` — profile table DDL
- `core/consolidation.py:829+` — `ProfileExtractionRuntimeMixin`
- `core/injection.py:51` — `retrieve_profile_items` as primary injection source
- `core/memory_ops.py:515+` — profile item CRUD operations
- ADR-006 — three-tier model (superseded in product narrative, retained for schema history)
- ADR-008 — old table retirement boundary
