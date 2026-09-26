"""TMEAAA-588: 时间/节日源 + event 记忆 schema/读取管道。

覆盖验收：calendar 固定日期（10-01 国庆节、含星期）；legacy DB 迁移后
memories 含 event_date/valid_until；_safe_memory_type('event')；retrieve_events
窗口/过期用例。
"""
import datetime
import sqlite3

import pytest


@pytest.fixture()
def cal(plugin_module):
    from astrbot_plugin_tmemory.core import calendar as _cal

    return _cal


# ── calendar ──────────────────────────────────────────────────────────────


def test_today_context_national_day_has_name_and_weekday(cal):
    out = cal.today_context(datetime.datetime(2026, 10, 1, 9, 5, tzinfo=datetime.timezone.utc))
    assert out.startswith("2026-10-01 星期四 09:05")
    assert "今日是 国庆节" in out


def test_today_context_fallback_matches_spec_example(cal, monkeypatch):
    """依赖不可用时仅用固定阳历兜底：09-26 → 无节日，下一个 国庆节 10-01。"""
    monkeypatch.setattr(cal, "_cc", None)
    cal._next_cache.clear()
    out = cal.today_context(datetime.datetime(2026, 9, 26, 21, 30, tzinfo=datetime.timezone.utc))
    assert out == "2026-09-26 星期六 21:30 · 今日节日：无（下一个：国庆节 10-01）"


def test_today_context_never_raises(cal, monkeypatch):
    def _boom(_d):
        raise RuntimeError("festival source down")

    monkeypatch.setattr(cal, "_festival_name", _boom)
    out = cal.today_context(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.timezone.utc))
    assert "2026-10-01" in out and "星期四" in out
    assert "节日" not in out


def test_today_context_accepts_date_and_garbage(cal):
    assert "2026-10-01" in cal.today_context(datetime.date(2026, 10, 1))
    # 非法输入不抛错，退化为当前时间。
    assert isinstance(cal.today_context(object()), str)


def test_next_festival_is_cached_per_day(cal, monkeypatch):
    monkeypatch.setattr(cal, "_cc", None)
    cal._next_cache.clear()
    calls = {"n": 0}
    real = cal._festival_name

    def _counting(d):
        calls["n"] += 1
        return real(d)

    monkeypatch.setattr(cal, "_festival_name", _counting)
    first = cal._next_festival(datetime.date(2026, 9, 26))
    assert first == (datetime.date(2026, 10, 1), "国庆节")
    after_first = calls["n"]
    assert cal._next_festival(datetime.date(2026, 9, 26)) == first
    assert calls["n"] == after_first  # 命中缓存，未再扫描


# ── schema 迁移 ────────────────────────────────────────────────────────────


def test_migrate_schema_adds_event_columns(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(context=None, config={})
    with sqlite3.connect(plugin.db_path) as conn:
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, canonical_user_id TEXT, "
            "source_adapter TEXT, source_user_id TEXT, memory TEXT, memory_hash TEXT, "
            "score REAL, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO memories(id, canonical_user_id, memory, memory_hash, score, created_at, updated_at) "
            "VALUES(1, 'legacy', 'old row', 'h', 0.5, '2020-01-01 00:00:00', '2020-01-01 00:00:00')"
        )

    plugin._migrate_schema()

    with plugin._db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        row = conn.execute("SELECT event_date, valid_until FROM memories WHERE id=1").fetchone()
    assert {"event_date", "valid_until"}.issubset(cols)
    # 老行空值，行为不变。
    assert row["event_date"] == "" and row["valid_until"] == ""
    plugin._close_db()


# ── config ────────────────────────────────────────────────────────────────


def test_new_injection_config_keys(plugin_module):
    from astrbot_plugin_tmemory.core.config import parse_config

    defaults = parse_config({})
    assert defaults.inject_time_context is True
    assert defaults.event_inject_window_days == 3

    cfg = parse_config(
        {"injection": {"inject_time_context": False, "event_inject_window_days": 7}}
    )
    assert cfg.inject_time_context is False
    assert cfg.event_inject_window_days == 7


# ── memory type ───────────────────────────────────────────────────────────


def test_event_is_valid_memory_type(plugin):
    from astrbot_plugin_tmemory.core.utils_shared import (
        _safe_memory_type as shared_safe,
    )

    assert plugin._safe_memory_type("event") == "event"
    assert plugin._safe_memory_type("EVENT") == "event"
    assert shared_safe("event") == "event"


# ── retrieve_events ───────────────────────────────────────────────────────


def _seed_event(plugin, text, event_date, valid_until="", memory_type="event"):
    return plugin._insert_memory(
        "u1", "webchat", "u1", text, 0.6, memory_type, 0.6, 0.6,
        event_date=event_date, valid_until=valid_until,
    )


def test_retrieve_events_window_and_expiry(plugin):
    _seed_event(plugin, "明天要考试", "2026-09-27")
    _seed_event(plugin, "下月旅行", "2026-10-06")
    _seed_event(plugin, "过期活动", "2026-09-24", valid_until="2026-09-25")
    plugin._insert_memory("u1", "webchat", "u1", "普通事实", 0.6, "fact", 0.6, 0.6)

    res = plugin._retrieval_mgr.retrieve_events("u1", "2026-09-26", 3)
    assert [r["memory"] for r in res] == ["明天要考试"]
    assert res[0]["event_date"] == "2026-09-27"
    assert res[0]["memory_type"] == "event"


def test_retrieve_events_order_and_valid_until(plugin):
    _seed_event(plugin, "晚一点的事", "2026-09-28", valid_until="2026-09-28")
    _seed_event(plugin, "早一点的事", "2026-09-24")

    res = plugin._retrieval_mgr.retrieve_events("u1", datetime.date(2026, 9, 26), 3)
    assert [r["event_date"] for r in res] == ["2026-09-24", "2026-09-28"]

    # 窗口 0 天只取当天。
    only = plugin._retrieval_mgr.retrieve_events("u1", "2026-09-24", 0)
    assert [r["memory"] for r in only] == ["早一点的事"]
