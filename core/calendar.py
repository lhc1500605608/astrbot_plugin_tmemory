"""Offline time / festival context source (TMEAAA-588).

Produces a one-line "today" context for prompt injection. The module is
**fail-closed**: it never raises, and degrades to date + weekday when the
optional festival source is unavailable.

Source priority:
1. ``chinese_calendar`` (guarded import) — accurate CN public holidays.
2. Fixed solar fallback — 元旦 01-01 / 劳动节 05-01 / 国庆节 10-01.
3. No festival.
"""

from __future__ import annotations

import datetime
import logging

logger = logging.getLogger("astrbot")

try:  # guarded: optional dependency, plugin must keep working without it
    import chinese_calendar as _cc
except Exception:  # noqa: BLE001 - any import failure is non-fatal
    _cc = None

# chinese_calendar.Holiday values are English strings; map to user-facing Chinese.
_HOLIDAY_CN = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "Mid-autumn Festival": "中秋节",
    "National Day": "国庆节",
}

# Fallback used when chinese_calendar is missing or lacks data for the year
# (the library only covers 2004-2026, so years beyond that rely on this list).
_SOLAR_FALLBACK = {
    (1, 1): "元旦",
    (5, 1): "劳动节",
    (10, 1): "国庆节",
}

_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_NEXT_SCAN_DAYS = 400
_next_cache: dict[str, tuple[datetime.date, str] | None] = {}


def _coerce(now) -> datetime.datetime:
    if isinstance(now, datetime.datetime):
        return now
    if isinstance(now, datetime.date):
        return datetime.datetime.combine(now, datetime.time.min)
    return datetime.datetime.now(tz=datetime.timezone.utc).astimezone()


def _festival_name(d: datetime.date) -> str | None:
    """Return the Chinese festival name for a date, or None."""
    if _cc is not None:
        try:
            is_holiday, raw_name = _cc.get_holiday_detail(d)
            if is_holiday and raw_name:
                mapped = _HOLIDAY_CN.get(str(raw_name))
                if mapped:
                    return mapped
        except Exception as _e:  # noqa: BLE001 - out-of-range year etc.
            logger.debug("[tmemory] calendar lookup failed for %s: %s", d, _e)
    return _SOLAR_FALLBACK.get((d.month, d.day))


def _next_festival(today: datetime.date) -> tuple[datetime.date, str] | None:
    """First festival strictly after ``today``; cached per day (hot path O(1))."""
    key = today.isoformat()
    if key in _next_cache:
        return _next_cache[key]
    result: tuple[datetime.date, str] | None = None
    try:
        d = today + datetime.timedelta(days=1)
        for _ in range(_NEXT_SCAN_DAYS):
            name = _festival_name(d)
            if name:
                result = (d, name)
                break
            d += datetime.timedelta(days=1)
    except Exception as _e:  # noqa: BLE001 - never raise
        logger.debug("[tmemory] next-festival scan failed: %s", _e)
        result = None
    _next_cache[key] = result
    return result


def today_context(now=None) -> str:
    """One-line time context, e.g. ``2026-09-26 星期六 21:30 · 今日节日：无（下一个：国庆节 10-01）``."""
    try:
        d = _coerce(now)
        base = f"{d:%Y-%m-%d} {_WEEKDAY_CN[d.weekday()]} {d:%H:%M}"
        name = _festival_name(d.date())
        if name:
            return f"{base} · 今日是 {name}"
        nxt = _next_festival(d.date())
        if nxt:
            nd, nname = nxt
            return f"{base} · 今日节日：无（下一个：{nname} {nd:%m-%d}）"
        return f"{base} · 今日节日：无"
    except Exception as _e:  # noqa: BLE001 - absolute fail-closed
        logger.debug("[tmemory] today_context degraded: %s", _e)
        try:
            d = _coerce(now)
            return f"{d:%Y-%m-%d} {_WEEKDAY_CN[d.weekday()]} {d:%H:%M}"
        except Exception:  # noqa: BLE001
            return ""


__all__ = ["today_context"]
