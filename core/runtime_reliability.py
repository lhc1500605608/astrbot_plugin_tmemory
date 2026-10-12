"""Persistent extraction queue, token reservations and retention policies."""
from contextlib import contextmanager
import time


class BudgetExceeded(RuntimeError):
    pass


def ensure_schema(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(conversation_cache)")}
    for key, ddl in {
        "processing_status": "TEXT NOT NULL DEFAULT 'pending'",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "REAL NOT NULL DEFAULT 0",
        "claimed_at": "REAL NOT NULL DEFAULT 0",
        "last_error": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if key not in columns:
            conn.execute(f"ALTER TABLE conversation_cache ADD COLUMN {key} {ddl}")
    conn.execute("CREATE TABLE IF NOT EXISTS llm_usage(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 "day TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT NOT NULL DEFAULT '',"
                 "provider_id TEXT NOT NULL,model_id TEXT NOT NULL,reserved INTEGER NOT NULL,"
                 "tokens_input INTEGER NOT NULL DEFAULT 0,tokens_output INTEGER NOT NULL DEFAULT 0,"
                 "tokens_total INTEGER NOT NULL DEFAULT 0,estimated INTEGER NOT NULL DEFAULT 1,"
                 "status TEXT NOT NULL DEFAULT 'reserved')")
    conn.execute("CREATE TABLE IF NOT EXISTS budget_checkpoints(day TEXT PRIMARY KEY,legacy_id INTEGER NOT NULL)")
    episode_columns = {r[1] for r in conn.execute("PRAGMA table_info(memory_episodes)")}
    for key, ddl in {"semantic_retry_count": "INTEGER NOT NULL DEFAULT 0",
                     "next_semantic_retry_at": "REAL NOT NULL DEFAULT 0",
                     "semantic_claimed_at": "REAL NOT NULL DEFAULT 0"}.items():
        if key not in episode_columns:
            conn.execute(f"ALTER TABLE memory_episodes ADD COLUMN {key} {ddl}")


@contextmanager
def anonymous_accounting():
    # Usage carries no user ID or message content; settle even after a user purge.
    from .data_safety import _generation
    token = _generation.set(None)
    try:
        yield
    finally:
        _generation.reset(token)


def daily_usage(conn, day):
    checkpoint = conn.execute("SELECT legacy_id FROM budget_checkpoints WHERE day=?", (day,)).fetchone()
    cutoff = checkpoint[0] if checkpoint else 2**63-1
    legacy = conn.execute("SELECT COALESCE(SUM(tokens_total),0) FROM distill_history WHERE started_at>=? AND started_at<? AND id<=? AND tokens_total>=0",
                          (day + " 00:00:00", day + " 99:99:99", cutoff)).fetchone()[0]
    used = conn.execute("SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved ELSE tokens_total END),0) FROM llm_usage WHERE day=?", (day,)).fetchone()[0]
    return int(legacy) + int(used)


def reserve(plugin, kwargs):
    now = plugin._now()
    prompt_size = sum(len(str(kwargs.get(key, ""))) for key in ("prompt", "system_prompt", "contexts"))
    input_estimate = max(1, prompt_size)
    budget = max(0, int(plugin._cfg.daily_token_budget))
    with anonymous_accounting(), plugin._db() as conn:
        conn.execute("INSERT OR IGNORE INTO budget_checkpoints SELECT ?,COALESCE(MAX(id),0) FROM distill_history", (now[:10],))
        used = daily_usage(conn, now[:10])
        remaining = budget - used
        if budget and remaining <= input_estimate:
            raise BudgetExceeded("plugin daily token budget unavailable")
        # One billable request at a time when a finite daily budget is enabled.
        # This avoids concurrent calls spending the same unreserved balance.
        reservation = remaining if budget else input_estimate + 1024
        if budget:
            kwargs.setdefault("max_tokens", min(1024, remaining - input_estimate))
        cur = conn.execute("INSERT INTO llm_usage(day,started_at,provider_id,model_id,reserved) VALUES(?,?,?,?,?)",
                           (now[:10], now, str(kwargs.get("chat_provider_id", "")), str(kwargs.get("model", "")), reservation))
        return int(cur.lastrowid), input_estimate, reservation


def settle(plugin, reservation, response=None, error=False):
    token, input_estimate, reserved = reservation
    usage = getattr(response, "usage", None)
    estimated = usage is None
    if isinstance(usage, dict):
        inp = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        out = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        total = int(usage.get("total_tokens", inp + out) or 0)
    elif usage is not None:
        inp = int(getattr(usage, "input_other", getattr(usage, "input", 0)) or 0) + int(getattr(usage, "input_cached", 0) or 0)
        out = int(getattr(usage, "output", 0) or 0)
        total = int(getattr(usage, "total_tokens", inp + out) or 0)
    else:
        inp = input_estimate
        out = len(str(getattr(response, "completion_text", "") or ""))
        total = inp + out
    with anonymous_accounting(), plugin._db() as conn:
        conn.execute("UPDATE llm_usage SET finished_at=?,tokens_input=?,tokens_output=?,tokens_total=?,estimated=?,status=? WHERE id=?",
                     (plugin._now(), inp, out, max(0, total), int(estimated), "failed" if error else "settled", token))


def claim(plugin, rows):
    if not rows:
        return False
    now = time.time()
    ids = [int(r["id"]) for r in rows]
    marks = ",".join("?" for _ in ids)
    with plugin._db() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM conversation_cache WHERE id IN ({marks}) AND distilled=0 AND processing_status IN ('pending','retry','waiting_provider') AND next_retry_at<=?", [*ids, now]).fetchone()[0]
        if count != len(ids):
            return False
        conn.execute(f"UPDATE conversation_cache SET processing_status='running',claimed_at=? WHERE id IN ({marks})", [now, *ids])
    return True


def fail(plugin, rows, reason, waiting=False):
    ids = [int(r["id"]) for r in rows]
    with plugin._db() as conn:
        for rid in ids:
            row = conn.execute("SELECT retry_count FROM conversation_cache WHERE id=?", (rid,)).fetchone()
            if not row:
                continue
            attempts = int(row[0]) + (0 if waiting else 1)
            status = "waiting_provider" if waiting else "retry" if attempts <= 3 else "failed"
            delay = 60 if waiting else (60, 300, 1800)[min(max(attempts-1, 0), 2)]
            conn.execute("UPDATE conversation_cache SET processing_status=?,retry_count=?,next_retry_at=?,last_error=? WHERE id=? AND distilled=0",
                         (status, attempts, time.time()+delay, str(reason)[:200], rid))


def recover(plugin, manual=False):
    with plugin._db() as conn:
        conn.execute("UPDATE conversation_cache SET processing_status='retry',next_retry_at=0 WHERE processing_status='running' AND claimed_at<?", (time.time()-600,))
        conn.execute("UPDATE memory_episodes SET consolidation_status='pending_semantic' WHERE consolidation_status='semantic_running' AND semantic_claimed_at<?", (time.time()-600,))
        if manual:
            conn.execute("UPDATE memory_episodes SET consolidation_status='pending_semantic',semantic_retry_count=0,next_semantic_retry_at=0 WHERE consolidation_status IN ('semantic_failed','pending_semantic')")
            conn.execute("UPDATE conversation_cache SET processing_status='pending',retry_count=0,next_retry_at=0 WHERE distilled=0 AND processing_status IN ('failed','retry','waiting_provider')")


def retain(plugin, uid):
    cfg = plugin._cfg
    with plugin._db() as conn:
        if cfg.profile_auto_archive_threshold > 0:
            conn.execute("UPDATE profile_items SET status='archived',updated_at=? WHERE canonical_user_id=? AND status='active' AND review_status='approved' AND is_pinned=0 AND stability<?",
                         (plugin._now(), uid, cfg.profile_auto_archive_threshold))
        count = conn.execute("SELECT COUNT(*) FROM profile_items WHERE canonical_user_id=? AND status='active' AND review_status='approved'", (uid,)).fetchone()[0]
        excess = max(0, count - cfg.profile_max_items_per_user)
        if excess:
            conn.execute("UPDATE profile_items SET status='archived',updated_at=? WHERE id IN (SELECT id FROM profile_items WHERE canonical_user_id=? AND status='active' AND review_status='approved' AND is_pinned=0 ORDER BY importance,confidence,stability,updated_at LIMIT ?)",
                         (plugin._now(), uid, excess))
        conn.execute("UPDATE event_items SET status='archived',updated_at=? WHERE canonical_user_id=? AND status='active' AND is_pinned=0 AND ((valid_until!='' AND valid_until<?) OR task_status IN ('completed','cancelled'))",
                     (plugin._now(), uid, plugin._now()[:10]))


def sync_config(plugin):
    for name in ("_distill_mgr", "_retrieval_mgr", "_capture_filter", "_injection_builder", "_vector_manager", "_proactive_engine"):
        component = getattr(plugin, name, None)
        if component is not None and hasattr(component, "_cfg"):
            component._cfg = plugin._cfg


def next_poll(plugin):
    interval = max(60, int(plugin._cfg.distill_interval_sec))
    with plugin._db() as conn:
        if plugin._cfg.daily_token_budget > 0 and daily_usage(conn, plugin._now()[:10]) >= plugin._cfg.daily_token_budget:
            return min(interval, 3600)
        retry = conn.execute("SELECT MIN(next_retry_at) FROM conversation_cache WHERE distilled=0 AND processing_status IN ('retry','waiting_provider')").fetchone()[0]
        semantic = conn.execute("SELECT MIN(next_semantic_retry_at) FROM memory_episodes WHERE consolidation_status='pending_semantic' AND semantic_retry_count>0").fetchone()[0]
    times = [value for value in (retry, semantic) if value is not None]
    return min(interval, max(30, min(times)-time.time())) if times else interval


async def retry_vectors(plugin):
    if not plugin._vec_available:
        return
    from .data_safety import generation_guard
    with plugin._db() as conn:
        rows = conn.execute("SELECT id,canonical_user_id,content FROM profile_items WHERE status='active' AND review_status='approved' AND embedding_status IN ('pending','failed') LIMIT 20").fetchall()
    for row in rows:
        with generation_guard(plugin, row["canonical_user_id"]):
            try:
                ok = await plugin._upsert_profile_vector(row["id"], row["content"])
                with plugin._db() as conn:
                    conn.execute("UPDATE profile_items SET embedding_status=? WHERE id=?", ("ready" if ok else "failed", row["id"]))
            except Exception:
                continue


def fail_episode(plugin, episode_id, waiting=False):
    with plugin._db() as conn:
        row = conn.execute("SELECT semantic_retry_count FROM memory_episodes WHERE id=?", (episode_id,)).fetchone()
        if not row:
            return
        count = row[0] + (0 if waiting else 1)
        conn.execute("UPDATE memory_episodes SET semantic_retry_count=?,next_semantic_retry_at=?,consolidation_status=? WHERE id=?",
                     (count, time.time()+(60 if waiting else (60,300,1800)[min(max(count-1,0),2)]), "pending_semantic" if count<=3 else "semantic_failed", episode_id))


def claim_episode(plugin, episode_id):
    with plugin._db() as conn:
        return conn.execute("UPDATE memory_episodes SET consolidation_status='semantic_running',semantic_claimed_at=? WHERE id=? AND consolidation_status='pending_semantic' AND next_semantic_retry_at<=?", (time.time(), episode_id, time.time())).rowcount == 1
