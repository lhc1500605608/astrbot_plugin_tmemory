# TMEAAA-250: Consolidation Pipeline & Layered Injection Design

- **Issue**: TMEAAA-250 (child of TMEAAA-248)
- **Author**: AI Engineer
- **Date**: 2026-05-03
- **Target**: tmemory 0.8.0 three-layer memory architecture

## 1. Overview

The current pipeline is flat: conversations → distill → flat memory list. The 0.8.0
architecture introduces two intermediate layers between raw conversation and semantic
memories, plus a structured injection strategy. This design covers the LLM/RAG/prompt/
cost-control aspects of that transition.

### Three-Layer Model

```
conversation_cache  ──►  topic_clusters  ──►  episode_summaries  ──►  memories
     (existing)         (new, Stage 1)       (new, Stage 2)        (existing, Stage 3)
```

- **Topic Clustering** groups related conversations into labeled topics.
- **Episodic Summarization** compresses each topic's conversations into a structured
  episode summary.
- **Semantic Distillation** extracts atomic, long-lived facts/preferences/style memories
  from episode summaries (refinement of the existing distill step).

Injection likewise becomes three-tier:
1. **Current episode context** — what's happening right now
2. **Semantic memories** — stable facts, preferences, constraints (existing behavior)
3. **Style guidance** — communication patterns (existing persona channel)

### Design Constraints (from parent TMEAAA-248)

- No new external infrastructure. Everything lives in the existing SQLite file.
- The injection path must not add significant latency to the main LLM request.
- LLM call frequency and token budgets are explicitly controlled.
- All new stages are independently disable-able and fall back gracefully.

---

## 2. Consolidation Pipeline Design

### 2.1 Architecture Overview

The existing `_distill_worker_loop()` (core/distill.py:182) runs a single-stage cycle:
collect undigested rows → LLM distill → insert memories. The new pipeline runs the
same background loop but executes up to three sequential stages per user batch.

```
_distoril_worker_loop()
  └─► for each pending user:
        ├─ Stage 1: topic_cluster(user)      ← NEW
        ├─ Stage 2: summarize_episodes(user)  ← NEW
        └─ Stage 3: distill_memories(user)    ← existing, input changed
```

Each stage is independently gated by config flags and per-user thresholds. If a stage
is disabled or its threshold isn't met, it is skipped for that user/cycle.

### 2.2 Stage 1: Topic Clustering

**Input**: Undigested `conversation_cache` rows for a user since last clustering run.

**Process**:
1. Fetch rows with `distilled=0` (or a new `clustered=0` flag) since last clustering
   timestamp.
2. Apply the existing `_prefilter_distill_rows()` to strip low-info content.
3. If row count < `topic_cluster_min_messages`, skip this user.
4. Build a compact transcript with `[id:N][role] content` format.
5. Call LLM with the topic clustering prompt (see §3.1).
6. Parse JSON output into topic assignments.
7. Write topic_clusters rows and update conversation_topic_map.

**Output**: Topic labels assigned to conversation rows. New/updated rows in a
`topic_clusters` table.

**LLM call**: 1 call per user per cycle, only if enough new messages exist.

**Cost guardrails**:
- Max input tokens: `topic_cluster_max_input_tokens` (default 4000)
- Transcript is truncated from newest-first if it exceeds the budget.
- Uses a configurable, typically cheaper model (`consolidation_provider_id`).

**Fallback**: Rule-based clustering via keyword Jaccard similarity + time proximity
if LLM call fails. Groups conversations within 30-min windows that share ≥2
meaningful tokens.

### 2.3 Stage 2: Episodic Summarization

**Input**: Conversations assigned to a topic cluster in Stage 1, for topics that
have new conversations since last summarization.

**Process**:
1. For each topic cluster with new (unsummarized) conversations:
   - Fetch the existing episode summary (if any).
   - Fetch new conversation rows for this topic.
   - Build a prompt with the previous summary + new conversations.
2. Call LLM with the episodic summarization prompt (see §3.2).
3. Parse output, upsert `episode_summaries` row.
4. Mark conversations as summarized.

**Output**: Updated episode summary rows in an `episode_summaries` table.

**LLM calls**: 1 call per updated topic per cycle. Gated by
`episode_summary_min_new_msgs` (default 3).

**Cost guardrails**:
- Max input tokens: `episode_summary_max_input_tokens` (default 3000)
- New conversations are appended to the previous summary; old detail is truncated
  if the combined input exceeds the budget.
- Max output tokens: ~300 per topic (controlled via prompt instruction).

**Fallback**: Concatenate first-100-chars of each new message as a simple append
to the existing summary.

### 2.4 Stage 3: Semantic Distillation (Refined)

This is the existing `run_distill_cycle()` but with a critical input change:
it now consumes **episode summaries** as the primary input, not raw conversation rows.

**Input**: Episode summaries that have been updated since last distillation, plus
their most recent raw conversations for detail.

**Process**:
1. Fetch episode summaries updated since last distill run.
2. For each episode: fetch the episode summary text + the 5 most recent raw
   conversation rows (for detail grounding).
3. Call the existing `build_distill_prompt()` with this combined input.
4. Parse, validate, insert memories (existing flow unchanged).

**LLM calls**: 1 call per updated episode per cycle. This should result in **fewer**
LLM calls than the current flat approach because episodes aggregate multiple
conversations.

**Cost guardrails**: Same as existing distill limits (`distill_batch_limit`, etc.),
plus the transcript is now pre-summarized so it's shorter for the same information
density.

### 2.5 Trigger Timing & Scheduling

All three stages run within the existing `_distill_worker_loop()`:

```
sleep_interval = min(
    distill_interval_sec,          # existing, default 17280s (4.8h)
    topic_cluster_interval_sec,    # new, default 3600s (1h)
    episode_summary_interval_sec,  # new, default 7200s (2h)
)
```

The worker wakes every `sleep_interval`, runs all enabled stages for all eligible
users, then sleeps again. Per-user throttles prevent the same user from being
processed too frequently (`distill_user_throttle_sec` applies across all stages).

**Manual triggers**: The existing `/tm_distill_now` command triggers all three stages
(force mode). New commands `/tm_cluster_now` and `/tm_summarize_now` can trigger
individual stages.

### 2.6 Batching Strategy

- **Per-user batching**: All stages process one user at a time. This is the existing
  pattern and keeps memory usage bounded.
- **Within a user**: Stage 1 processes all pending conversations at once (up to
  `distill_batch_limit`). Stage 2 processes all pending topics for the user
  sequentially. Stage 3 processes all pending episodes sequentially.
- **Cross-user parallelism**: Not introduced. The worker is single-threaded per user
  to avoid SQLite contention. If needed later, user-level parallelism can be added
  with asyncio.gather() since each user's data is independent.

### 2.7 Async Behavior

- The worker loop runs as an `asyncio.Task` (existing pattern).
- Each LLM call within a stage is `await`ed sequentially within a user, but users
  could be parallelized with `asyncio.gather()` if needed (not in initial implementation).
- Each stage has a configurable timeout (`stage_timeout_sec`, default 120s).
- The worker loop can be cancelled via `_worker_running = False` (existing pattern).
- The `distill_pause` config flag pauses all three stages.

### 2.8 Failure Handling

| Failure | Behavior |
|---|---|
| LLM call timeout | Skip stage for this user, log warning, continue to next user |
| LLM returns unparseable JSON | Retry once with stricter prompt; if still bad, fall back to rule-based extraction |
| LLM provider unavailable | Fall back to rule-based clustering/summarization (see §2.2, §2.3) |
| Stage 1 (clustering) fails | Stages 2 and 3 skip for this user (no topics to summarize) |
| Stage 2 (summarization) fails | Stage 3 falls back to raw conversation rows as input (existing behavior) |
| Stage 3 (distillation) fails | Episodes remain marked as un-distilled, retried next cycle |
| DB write fails | Exception logged, transaction rolled back, continue to next user |
| Worker loop crashes | Outer try/except logs and restarts loop (existing pattern) |

Key principle: **a failure in one stage for one user never blocks other users or
future cycles.** The system degrades gracefully: without clustering, summarization
works on raw conversations. Without summarization, distillation works on raw
conversations (current behavior). Without distillation, memories are simply not
created but conversations are preserved.

---

## 3. Prompt Sketches

### 3.1 Topic Clustering Prompt

```
你是对话主题聚类器。分析以下用户对话，将消息按主题分组。

输入格式: [id:N][role] 内容

输出格式(仅JSON):
{
  "topics": [
    {
      "topic_label": "简短主题名(2-8字)",
      "conversation_ids": [1, 3, 5],
      "brief": "一句话描述这个主题讨论的内容"
    }
  ],
  "noise_ids": [2, 4]
}

规则:
1. 主题名应简洁具体，如"Python学习""旅行计划""饮食偏好"，不要用"其他""杂项"。
2. 包含实质信息的消息才聚类；寒暄/单次问答归入 noise_ids。
3. 同一主题的消息应在语义上连贯(讨论同一件事/同一领域)。
4. 最多生成 5 个主题，每个主题至少包含 2 条消息。
5. 如果消息太少或没有明显主题，返回空 topics。

对话:
{transcript}
```

**Model**: Cheap/fast model (e.g., gpt-4o-mini, doubao-lite, qwen-turbo).
**Estimated tokens**: ~2K input, ~300 output per batch of 50 messages.
**Cost-control**: Transcript is truncated to `topic_cluster_max_input_tokens` chars
from newest-first. Minimum 2 messages per topic avoids over-fragmentation.

### 3.2 Episodic Summarization Prompt

```
你是对话摘要器。基于已有摘要和新对话，更新对用户当前话题的阶段性理解。

已有摘要(可能为空):
{previous_summary}

新增对话:
{new_conversations}

输出格式(仅JSON):
{
  "topic_label": "主题名",
  "summary": "2-5句话的阶段性摘要，包含:话题背景、用户表达了什么、做了什么决定、什么在进展中",
  "key_entities": ["关键实体1", "关键实体2"],
  "status": "ongoing|resolved|background",
  "resolved_at": null
}

规则:
1. 只记录用户表达的内容(需求、偏好、决定、进展)，不记录AI的回复。
2. 摘要应适合作为未来对话的上下文注入，帮助AI理解用户当前关注点。
3. 如果新对话表明之前的问题已解决，标记 status=resolved。
4. 保持摘要简洁，300字以内。如果已有摘要+新对话很长，优先保留最新信息。

对话:
{new_conversations}
```

**Model**: Same cheap model as clustering.
**Estimated tokens**: ~2K input (previous summary + new convs), ~200 output per topic.
**Cost-control**: `episode_summary_max_input_tokens` caps input. New conversations are
truncated to newest-first within budget.

### 3.3 Semantic Distillation Prompt (Refined)

This is a refinement of the existing `build_distill_prompt()` (core/distill.py:55).
The key change: the input is an **episode summary + key conversation snippets**
instead of a raw transcript dump.

```
你是高质量记忆蒸馏器。从以下阶段性对话摘要和相关对话中提炼**长期有价值**的用户信息。

阶段摘要:
{episode_summary}

关键对话片段:
{key_snippets}

{existing_format_and_rules_from_current_prompt}

特别规则(新增):
- 优先从阶段摘要中提取跨会话的稳定模式，而非单次对话的细节。
- 如果摘要标记为 resolved，相关事实的 importance 可适当降低(用户已不再关注)。
- 置信度低于 0.6 的记忆仍不输出。
```

**Model**: Can use a more capable model if configured (`distill_provider_id`), or
fall back to the cheap consolidation model.
**Estimated tokens**: ~1.5K input (shorter than current raw transcripts), ~400 output.
**Cost savings vs. current**: Episode summaries are denser than raw transcripts, so
the same information fits in fewer input tokens (~40-60% reduction).

### 3.4 Cost-Control Strategy

| Mechanism | How |
|---|---|
| **Separate model tier** | Clustering + summarization use a cheap model (`consolidation_provider_id`). Distillation can use a better model (`distill_provider_id`). Default: same as chat provider if not configured. |
| **Per-stage enable flags** | Each stage can be independently disabled, reducing LLM calls to zero for that stage. |
| **Minimum thresholds** | Each stage has a min-messages/configurable threshold before it triggers an LLM call. |
| **Token budgets** | `topic_cluster_max_input_tokens`, `episode_summary_max_input_tokens`, existing `distill_batch_limit` all cap input size. |
| **Per-user throttle** | `distill_user_throttle_sec` prevents rapid repeated LLM calls for the same user. |
| **Per-cycle user cap** | `distill_max_users_per_cycle` (new) caps how many users are processed per cycle, preventing cost spikes when many users have pending data. |
| **Batching efficiency** | Episode summarization reduces total LLM calls vs. per-conversation distillation. |
| **Fallback to rule-based** | Every LLM call has a zero-cost rule-based fallback (keyword clustering, template summarization, regex distillation). |
| **Token tracking** | Existing `distill_history` table records tokens_input/output per cycle. Extended to record per-stage breakdown. |
| **Cost summary API** | Existing `/tm_distill_history` and `/tm_stats` commands already expose token data. Extended with per-stage cost breakdown. |

**Estimated cost per active user per day** (assuming 50 messages/day, gpt-4o-mini pricing):

| Stage | Calls/day | Input tokens | Output tokens | Cost (gpt-4o-mini) |
|---|---|---|---|---|
| Topic Clustering | 1-2 | ~4K | ~600 | ~$0.001 |
| Episodic Summarization | 1-3 | ~6K | ~600 | ~$0.0015 |
| Semantic Distillation | 0.5-1 | ~3K | ~400 | ~$0.0008 |
| **Total** | | | | **~$0.0033/user/day** |

At $0.0033/active-user/day with 100 active users: ~$0.33/day, ~$10/month.

---

## 4. Retrieval & Injection Strategy

### 4.1 Three-Layer Context Block

The current injection produces a flat block:
```
[用户记忆]
- (fact) ...
[用户风格指导]
- ...
```

The new injection produces a three-layer block:

```
[当前对话背景]
当前话题: {topic_label}
相关背景: {episode_summary_brief}

[用户记忆]
- (preference) 用户偏好使用 TypeScript
- (fact) 用户是一名后端工程师
...

[用户风格指导]
- 用户沟通风格随意，常用"哈哈"表达情绪
...
```

### 4.2 Retrieval Flow

The existing `_build_knowledge_injection()` (core/utils.py:76) becomes a three-step
process:

```
_build_knowledge_injection():
  1. Episode context retrieval (NEW, synchronous, no LLM call)
     - Find the current topic by matching the user's latest query against
       topic_clusters (keyword overlap) or by looking up the most recent topic.
     - Fetch the associated episode_summary if one exists.
     - Format as "[当前对话背景]" block.
     - Cost: one SQL query, <5ms.

  2. Semantic memory retrieval (EXISTING, hybrid search)
     - Same as current "canonical" channel.
     - Vector + FTS5 hybrid search against memories table.
     - Cost: embedding API call + hybrid search, <200ms typical.

  3. Style guidance retrieval (EXISTING, importance-sorted)
     - Same as current "persona" channel.
     - Top-N style memories by importance.
     - Cost: one SQL query, <5ms.
```

### 4.3 Injection Path Performance

The critical constraint: injection must not add significant latency to the main LLM
request. All three steps above are I/O-bound (DB reads, optional embedding API call).

| Step | Latency (P95) | Notes |
|---|---|---|
| Episode context lookup | <5ms | Pure SQLite, no network |
| Semantic retrieval (no vector) | <20ms | FTS5 only |
| Semantic retrieval (with vector) | <200ms | Embedding API call dominates |
| Style retrieval | <5ms | Pure SQLite |
| Block formatting | <1ms | String ops |
| **Total (no vector)** | **<30ms** | Negligible vs. LLM response time |
| **Total (with vector)** | **<200ms** | Acceptable (LLM response is 2-30s) |

No new network calls are added. The episode context lookup is pure SQLite. The
existing embedding API call for semantic search remains the only network hop.

### 4.4 Context Block Size Budget

New config: `inject_max_chars` already exists and caps the total block.
Recommendation: increase default from 0 (unlimited) to 1200 to keep injection
concise. The three sections are allocated proportionally:

| Section | Default char budget | Config field |
|---|---|---|
| Episode context | 300 | `inject_episode_max_chars` |
| Semantic memories | 600 | (uses existing `inject_max_chars` - other sections) |
| Style guidance | 300 | `inject_style_max_chars` |

The block is truncated at the section level if the total exceeds `inject_max_chars`.

---

## 5. PluginConfig Field Recommendations

### 5.1 New Fields

```python
# ── Consolidation Pipeline ──

# Master switch for the three-stage pipeline.
# When False, behavior is identical to current (flat distill).
enable_consolidation_pipeline: bool = False

# Stage 1: Topic Clustering
enable_topic_clustering: bool = True
topic_cluster_interval_sec: int = 3600        # min interval between clustering runs (1h)
topic_cluster_min_messages: int = 10          # min undigested messages to trigger clustering
topic_cluster_max_input_tokens: int = 4000    # max input tokens for clustering LLM call

# Stage 2: Episodic Summarization
enable_episodic_summarization: bool = True
episode_summary_interval_sec: int = 7200      # min interval between summarization runs (2h)
episode_summary_min_new_msgs: int = 3         # min new messages in a topic to trigger re-summary
episode_summary_max_input_tokens: int = 3000  # max input tokens for summarization LLM call

# Shared consolidation model settings
use_independent_consolidation_model: bool = False  # use a separate model for clustering + summarization
consolidation_provider_id: str = ""           # provider for consolidation LLM calls
consolidation_model_id: str = ""              # model for consolidation LLM calls

# ── Injection ──

# Episode context injection
inject_episode_context: bool = True           # include episode context in injection block
inject_episode_max_chars: int = 300           # max chars for the episode context section
inject_style_max_chars: int = 300             # max chars for the style guidance section

# ── Cost Control ──

# Max users processed per distill cycle (prevents cost spikes)
distill_max_users_per_cycle: int = 10
# Per-stage LLM call timeout
stage_timeout_sec: int = 120
```

### 5.2 Modified Existing Fields

| Field | Change | Reason |
|---|---|---|
| `inject_max_chars` | Default 0 → 1200 | Prevents unbounded injection |
| `distill_interval_sec` | No change to default | New stages have their own intervals |
| `distill_batch_limit` | No change | Still applies to Stage 3 input |

### 5.3 Config Migration

All new fields have defaults that preserve current behavior:
- `enable_consolidation_pipeline = False` means the pipeline behaves exactly as today.
- When enabled, each stage can be independently toggled.
- `parse_config()` handles missing keys by using defaults (existing behavior).

---

## 6. Data Model Additions

### 6.1 New Tables

```sql
-- Topic clusters: one row per discovered topic per user
CREATE TABLE IF NOT EXISTS topic_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    topic_label TEXT NOT NULL,
    brief TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

-- Episode summaries: one row per topic, updated incrementally
CREATE TABLE IF NOT EXISTS episode_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_user_id TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    key_entities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'ongoing',
    resolved_at TEXT,
    conversation_count INTEGER NOT NULL DEFAULT 0,
    first_conversation_at TEXT NOT NULL,
    last_conversation_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    distilled INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (topic_id) REFERENCES topic_clusters(id)
);
```

### 6.2 Modified Existing Tables

```sql
-- Add topic_id to conversation_cache for tracking cluster membership
ALTER TABLE conversation_cache ADD COLUMN topic_id INTEGER DEFAULT 0;
ALTER TABLE conversation_cache ADD COLUMN clustered INTEGER DEFAULT 0;
ALTER TABLE conversation_cache ADD COLUMN summarized INTEGER DEFAULT 0;
```

### 6.3 Indices

```sql
CREATE INDEX IF NOT EXISTS idx_topic_clusters_user ON topic_clusters(canonical_user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_episode_summaries_user ON episode_summaries(canonical_user_id, distilled);
CREATE INDEX IF NOT EXISTS idx_episode_summaries_topic ON episode_summaries(topic_id);
CREATE INDEX IF NOT EXISTS idx_conversation_cache_topic ON conversation_cache(topic_id);
CREATE INDEX IF NOT EXISTS idx_conversation_cache_clustered ON conversation_cache(canonical_user_id, clustered);
```

### 6.4 Compatibility

- All new tables and columns are additive. Existing queries continue to work.
- If `enable_consolidation_pipeline = False`, the new tables are simply unused.
- `topic_id = 0` and `clustered = 0` are the defaults, meaning "not processed."
- The existing `distilled` flag on `conversation_cache` still controls Stage 3 input.
  Stage 1 uses `clustered`, Stage 2 uses `summarized`.

---

## 7. Migration Path

### Phase 1: Schema + Config (backward-compatible)
1. Add new tables and columns via `_ensure_columns()` (existing migration pattern in db.py).
2. Add new config fields with safe defaults (parse_config handles missing keys).
3. No behavior change. All existing tests continue to pass.

### Phase 2: Pipeline Core
1. Implement Stage 1 clustering in `core/cluster.py`.
2. Implement Stage 2 summarization in `core/summarize.py`.
3. Wire stages into `_distill_worker_loop()` behind `enable_consolidation_pipeline` flag.
4. Implement rule-based fallbacks for each stage.

### Phase 3: Injection Upgrade
1. Implement `_build_knowledge_injection()` v2 with three-layer output.
2. Add episode context retrieval (pure SQLite, no new LLM calls).
3. Wire behind `inject_episode_context` flag.

### Phase 4: Validation
1. Offline evaluation: run new pipeline on historical conversation data.
2. Compare memory quality (precision/recall) against current flat pipeline.
3. Measure cost delta (token usage per active user per day).
4. Measure injection latency delta (P95).

### Rollback
- Set `enable_consolidation_pipeline = False` → identical to current behavior.
- New tables are ignored.
- No data loss: `conversation_cache` and `memories` are untouched.

---

## 8. Evaluation Criteria

Before marking this design as validated for production:

| Criterion | Measurement | Target |
|---|---|---|
| Memory precision | % of distilled memories rated "useful" by human review | ≥ current baseline |
| Memory recall | % of known user facts captured in memories | ≥ current baseline |
| LLM calls per user per day | Count from distill_history | ≤ current baseline (fewer calls expected due to batching) |
| Token usage per user per day | tokens_input + tokens_output from distill_history | ≤ 120% of current baseline |
| Injection latency P95 | Time from `on_llm_request` entry to injection complete | ≤ +50ms vs. current |
| Pipeline failure rate | % of cycles with at least one user failure | ≤ 5% |
| Fallback effectiveness | % of fallback outputs that pass validation | ≥ 80% |

---

## 9. Open Questions for CTO

1. **Model selection for consolidation**: Do we standardize on a specific cheap model
   (e.g., gpt-4o-mini) or leave it configurable per deployment? Recommendation: fully
   configurable via `consolidation_provider_id` / `consolidation_model_id`, with
   fallback to the chat provider.

2. **Episode retention**: How long do we keep resolved episodes? Recommendation: mark
   `is_active=0` after 30 days of no new messages, but keep the data. Purge via
   existing maintenance cycle.

3. **Cross-session topics**: If a user talks about Python on Monday and again on
   Friday, should those be merged into one topic? Recommendation: Stage 1 clustering
   prompt includes the existing topic labels as context so the LLM can decide to merge.

4. **Injection order**: Episode context before or after semantic memories?
   Recommendation: episode context first (sets scene), then memories (facts), then
   style (how to say it). This matches how humans contextualize: where are we → what
   do I know about this person → how should I talk to them.
