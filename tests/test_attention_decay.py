"""注意力衰减记忆评分测试 (TMEAAA-199)."""
import math
import time
import asyncio


def _insert_memory_with_age(plugin, canonical_id, memory_text, days_old, score=0.3, importance=0.2, confidence=0.3):
    mem_id = plugin._insert_memory(
        canonical_id=canonical_id,
        adapter="qq",
        adapter_user="99",
        memory=memory_text,
        score=score,
        memory_type="fact",
        importance=importance,
        confidence=confidence,
    )
    old_ts = time.time() - days_old * 86400
    old_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old_ts))
    with plugin._db() as conn:
        conn.execute(
            "UPDATE memories SET last_seen_at=?, created_at=?, updated_at=? WHERE id=?",
            (old_dt, old_dt, old_dt, mem_id),
        )
    return mem_id


def test_attention_decay_applies_exponential_decay(plugin):
    """attention_score 应按指数衰减: 14天后从0.7衰减至~0.35。"""
    mem_id = _insert_memory_with_age(plugin, "attn-decay", "测试注意力衰减", days_old=14)
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET attention_score=0.7 WHERE id=?", (mem_id,))

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT attention_score FROM memories WHERE id=?", (mem_id,)).fetchone()
    expected = 0.7 * math.exp(-0.05 * 14)
    assert abs(float(row["attention_score"]) - expected) < 0.01
    assert float(row["attention_score"]) < 0.5


def test_attention_decay_preserves_recent_memory(plugin):
    """最近（0天）的记忆不应显著衰减。"""
    mem_id = _insert_memory_with_age(plugin, "attn-fresh", "刚刚召回过", days_old=0)
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET attention_score=0.8 WHERE id=?", (mem_id,))

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT attention_score FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert float(row["attention_score"]) > 0.79


def test_attention_decay_floor(plugin):
    """attention_score 衰减后不低于下限 0.05。"""
    mem_id = _insert_memory_with_age(plugin, "attn-floor", "衰减到底", days_old=200)
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET attention_score=0.1 WHERE id=?", (mem_id,))

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT attention_score FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert float(row["attention_score"]) >= 0.05


def test_attention_decay_ignores_pinned(plugin):
    """is_pinned=1 的记忆注意力不应被衰减。"""
    mem_id = _insert_memory_with_age(plugin, "attn-pinned", "常驻记忆", days_old=30)
    with plugin._db() as conn:
        conn.execute("UPDATE memories SET is_pinned=1, attention_score=0.7 WHERE id=?", (mem_id,))

    plugin._decay_stale_memories()

    with plugin._db() as conn:
        row = conn.execute("SELECT attention_score FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert float(row["attention_score"]) > 0.69  # 几乎不变


def test_attention_score_initialized_from_importance(plugin):
    """新记忆的 attention_score 应根据 importance 初始化。"""
    mem_id = plugin._insert_memory(
        canonical_id="attn-init",
        adapter="mock",
        adapter_user="99",
        memory="初始注意力测试",
        score=0.7,
        memory_type="fact",
        importance=0.75,
        confidence=0.7,
    )
    with plugin._db() as conn:
        row = conn.execute("SELECT attention_score FROM memories WHERE id=?", (mem_id,)).fetchone()
    assert float(row["attention_score"]) == 0.75


def test_attention_boost_on_duplicate_insert(plugin):
    """重复插入相同记忆时 attention_score 应 +0.05。"""
    # 第一次插入
    plugin._insert_memory(
        canonical_id="attn-dup",
        adapter="mock",
        adapter_user="99",
        memory="重复插入测试",
        score=0.7,
        memory_type="fact",
        importance=0.6,
        confidence=0.7,
    )
    # 第二次插入相同内容
    mem_id = plugin._insert_memory(
        canonical_id="attn-dup",
        adapter="mock",
        adapter_user="99",
        memory="重复插入测试",
        score=0.7,
        memory_type="fact",
        importance=0.6,
        confidence=0.7,
    )
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT attention_score, reinforce_count FROM memories WHERE id=?", (mem_id,)
        ).fetchone()
    # 初始 0.6 + 0.05 = 0.65
    assert float(row["attention_score"]) >= 0.64
    assert int(row["reinforce_count"]) >= 2
