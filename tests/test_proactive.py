"""主动性记忆（Proactive / send_message）单测 — Plan TMEAAA-379 T2 (B2)。

覆盖验收：默认关闭零副作用、opt-in 门控、限流、每日预算、能力探测降级、
触发（到期提醒 + 主动回忆）与审计。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

BASE_TS = 1_700_000_000.0
UMO_A = "aiocqhttp:FriendMessage:u1"
UMO_B = "aiocqhttp:FriendMessage:u2"


@pytest.fixture()
def proactive(plugin_module):
    from astrbot_plugin_tmemory.core import proactive as _proactive

    return _proactive


@pytest.fixture()
def parse_cfg(plugin_module):
    from astrbot_plugin_tmemory.core.config import parse_config

    return parse_config


@pytest.fixture()
def send_port(plugin_module):
    from astrbot_plugin_tmemory.adapters import send as _send

    return _send


class _FakeContext:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list = []

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        if not self.ok:
            raise RuntimeError("boom")
        return None


def _ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _make_engine(plugin, parse_cfg, now: float = BASE_TS, **overrides):
    from astrbot_plugin_tmemory.core.proactive import ProactiveEngine

    cfg = parse_cfg({"proactive": overrides})
    return ProactiveEngine(
        plugin._db_mgr, cfg, plugin._memory_logger, now_fn=lambda: now
    )


def _enabled(plugin, parse_cfg, now: float = BASE_TS, **overrides):
    opts = {"proactive_enabled": True}
    opts.update(overrides)
    return _make_engine(plugin, parse_cfg, now, **opts)


def _send_log(plugin):
    with plugin._db() as conn:
        return conn.execute(
            "SELECT canonical_user_id, trigger_type, status, reason"
            " FROM proactive_send_log ORDER BY id"
        ).fetchall()


def _events(plugin):
    with plugin._db() as conn:
        return conn.execute(
            "SELECT canonical_user_id, event_type FROM memory_events ORDER BY id"
        ).fetchall()


# ── 默认关闭 / 零副作用 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disabled_is_zero_side_effect(plugin, parse_cfg):
    engine = _make_engine(plugin, parse_cfg)  # proactive_enabled 默认 False
    assert engine.is_enabled() is False

    called = []

    async def _send(umo, chain):
        called.append(umo)
        return True

    engine.add_reminder("u1", UMO_A, "到期提醒", BASE_TS - 10)
    results = await engine.run_cycle(_send, now=BASE_TS)

    assert results == []
    assert called == []
    assert _send_log(plugin) == []
    assert _events(plugin) == []


@pytest.mark.asyncio
async def test_worker_not_started_when_disabled(plugin, parse_cfg):
    plugin._cfg = parse_cfg({})
    plugin.context = _FakeContext()
    task = await plugin._start_proactive_worker()
    assert task is None
    assert plugin._proactive_task is None
    assert plugin._proactive_engine is None


# ── opt-in 门控 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_non_opt_in_user_skipped_with_audit(plugin, parse_cfg, proactive):
    engine = _enabled(plugin, parse_cfg)
    engine.add_reminder("u1", UMO_A, "该吃药了", BASE_TS - 10)
    sent = []

    async def _send(umo, chain):
        sent.append(umo)
        return True

    results = await engine.run_cycle(_send, now=BASE_TS)

    assert sent == []
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == proactive.REASON_NOT_OPT_IN
    log = _send_log(plugin)
    assert len(log) == 1 and log[0]["status"] == "skipped"
    assert log[0]["reason"] == proactive.REASON_NOT_OPT_IN
    events = _events(plugin)
    assert events[0]["canonical_user_id"] == "u1"
    assert events[0]["event_type"] == proactive.EVENT_SKIPPED


@pytest.mark.asyncio
async def test_only_opt_in_user_receives(plugin, parse_cfg):
    engine = _enabled(plugin, parse_cfg, proactive_min_interval_sec=0)
    engine.set_user_opt_in("u1", True)
    engine.add_reminder("u1", UMO_A, "开会", BASE_TS - 10)
    engine.add_reminder("u2", UMO_B, "打卡", BASE_TS - 10)
    sent = []

    async def _send(umo, chain):
        sent.append(umo)
        return True

    await engine.run_cycle(_send, now=BASE_TS)

    assert sent == [UMO_A]
    by_user = {r["canonical_user_id"]: r["status"] for r in _send_log(plugin)}
    assert by_user == {"u1": "sent", "u2": "skipped"}


def test_opt_in_toggle(plugin, parse_cfg):
    engine = _enabled(plugin, parse_cfg)
    assert engine.get_user_opt_in("u1") is False
    engine.set_user_opt_in("u1", True)
    assert engine.get_user_opt_in("u1") is True
    engine.set_user_opt_in("u1", False)
    assert engine.get_user_opt_in("u1") is False


def test_opt_in_default_applies_without_row(plugin, parse_cfg):
    engine = _enabled(plugin, parse_cfg, proactive_opt_in_default=True)
    assert engine.get_user_opt_in("never-seen") is True


# ── 限流 / 预算 ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_window_skips_and_audits(plugin, parse_cfg, proactive):
    engine = _enabled(
        plugin,
        parse_cfg,
        proactive_per_user_max_per_window=2,
        proactive_min_interval_sec=0,
        proactive_daily_budget=0,
    )
    engine.set_user_opt_in("u1", True)
    for i in range(3):
        engine.add_reminder("u1", UMO_A, f"提醒{i}", BASE_TS - 100 + i)
    sent = []

    async def _send(umo, chain):
        sent.append(umo)
        return True

    results = await engine.run_cycle(_send, now=BASE_TS)

    assert len(sent) == 2
    assert [r["status"] for r in results] == ["sent", "sent", "skipped"]
    assert results[2]["reason"] == proactive.REASON_RATE_LIMITED
    assert any(
        r["status"] == "skipped" and r["reason"] == proactive.REASON_RATE_LIMITED
        for r in _send_log(plugin)
    )


@pytest.mark.asyncio
async def test_min_interval_skips(plugin, parse_cfg, proactive):
    engine = _enabled(
        plugin,
        parse_cfg,
        proactive_per_user_max_per_window=10,
        proactive_min_interval_sec=300,
        proactive_daily_budget=0,
    )
    engine.set_user_opt_in("u1", True)
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO proactive_send_log"
            "(canonical_user_id, trigger_type, status, reason, message, created_at)"
            " VALUES(?,?,?,?,?,?)",
            ("u1", proactive.TRIGGER_RECALL, "sent", "", "hi", _ts(BASE_TS - 100)),
        )
    engine.add_reminder("u1", UMO_A, "提醒", BASE_TS - 10)
    sent = []

    async def _send(umo, chain):
        sent.append(umo)
        return True

    results = await engine.run_cycle(_send, now=BASE_TS)
    assert sent == []
    assert results[0]["reason"] == proactive.REASON_RATE_LIMITED


@pytest.mark.asyncio
async def test_daily_budget_exhausted(plugin, parse_cfg, proactive):
    engine = _enabled(
        plugin,
        parse_cfg,
        proactive_per_user_max_per_window=100,
        proactive_min_interval_sec=0,
        proactive_daily_budget=1,
    )
    engine.set_user_opt_in("u1", True)
    engine.add_reminder("u1", UMO_A, "第一条", BASE_TS - 20)
    engine.add_reminder("u1", UMO_A, "第二条", BASE_TS - 10)
    sent = []

    async def _send(umo, chain):
        sent.append(umo)
        return True

    results = await engine.run_cycle(_send, now=BASE_TS)

    assert len(sent) == 1
    assert results[0]["status"] == "sent"
    assert results[1]["status"] == "skipped"
    assert results[1]["reason"] == proactive.REASON_BUDGET


# ── 触发：到期提醒 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reminder_trigger_marks_sent(plugin, parse_cfg, proactive):
    engine = _enabled(plugin, parse_cfg, proactive_min_interval_sec=0)
    engine.set_user_opt_in("u1", True)
    rid = engine.add_reminder("u1", UMO_A, "交报告", BASE_TS - 5)
    sent = []

    async def _send(umo, chain):
        sent.append(chain)
        return True

    results = await engine.run_cycle(_send, now=BASE_TS)

    assert results[0]["trigger"] == proactive.TRIGGER_REMINDER
    assert results[0]["status"] == "sent"
    assert sent and sent[0][0]["text"] == "交报告"
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT status FROM proactive_reminders WHERE id=?", (rid,)
        ).fetchone()
    assert row["status"] == "sent"
    assert any(e["event_type"] == proactive.EVENT_SENT for e in _events(plugin))


def test_future_reminder_not_collected(plugin, parse_cfg):
    engine = _enabled(plugin, parse_cfg)
    engine.add_reminder("u1", UMO_A, "未来提醒", BASE_TS + 3600)
    assert engine.collect_due_reminders(BASE_TS, 5) == []


# ── 触发：主动回忆（画像） ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recall_trigger_from_profile(plugin, parse_cfg, proactive):
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ops = ProfileItemOps(plugin)
    ops.upsert_profile_item("u1", "preference", "偏好", "用户喜欢喝手冲咖啡", 0.9, 0.9)
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO conversation_cache"
            "(canonical_user_id, role, content, unified_msg_origin, created_at)"
            " VALUES(?,?,?,?,?)",
            ("u1", "user", "hi", UMO_A, "2025-01-01 00:00:00"),
        )

    engine = _enabled(plugin, parse_cfg, proactive_reminder_enabled=False)
    engine.set_user_opt_in("u1", True)
    sent = []

    async def _send(umo, chain):
        sent.append((umo, chain[0]["text"]))
        return True

    results = await engine.run_cycle(_send, now=BASE_TS)

    assert results[0]["trigger"] == proactive.TRIGGER_RECALL
    assert results[0]["status"] == "sent"
    assert sent[0][0] == UMO_A
    assert "手冲咖啡" in sent[0][1]


# ── 能力探测降级 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capability_unavailable_disables_sending(plugin, parse_cfg, proactive):
    engine = _enabled(plugin, parse_cfg)
    engine.set_user_opt_in("u1", True)
    engine.set_capability(False, "context_send_message_missing")
    engine.add_reminder("u1", UMO_A, "提醒", BASE_TS - 5)
    sent = []

    async def _send(umo, chain):
        sent.append(umo)
        return True

    results = await engine.run_cycle(_send, now=BASE_TS)
    assert results == []
    assert sent == []
    assert _send_log(plugin) == []
    allowed, reason = engine.evaluate("u1", BASE_TS)
    assert allowed is False and reason == proactive.REASON_CAPABILITY


@pytest.mark.asyncio
async def test_worker_not_started_when_capability_missing(plugin, parse_cfg):
    plugin._cfg = parse_cfg({"proactive": {"proactive_enabled": True}})
    plugin.context = None
    task = await plugin._start_proactive_worker()
    assert task is None
    assert plugin._proactive_engine is None


@pytest.mark.asyncio
async def test_worker_starts_and_cancels_with_capability(plugin, parse_cfg):
    plugin.context = _FakeContext()
    plugin._cfg = parse_cfg(
        {"proactive": {"proactive_enabled": True, "proactive_interval_sec": 30}}
    )
    plugin._worker_running = True
    task = await plugin._start_proactive_worker()
    assert task is not None
    assert plugin._proactive_engine is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


# ── 发送失败审计 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_failure_audited(plugin, parse_cfg, proactive):
    engine = _enabled(plugin, parse_cfg, proactive_min_interval_sec=0)
    engine.set_user_opt_in("u1", True)
    rid = engine.add_reminder("u1", UMO_A, "提醒", BASE_TS - 5)

    async def _send(umo, chain):
        return False

    results = await engine.run_cycle(_send, now=BASE_TS)
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == proactive.REASON_SEND_FAILED
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT status FROM proactive_reminders WHERE id=?", (rid,)
        ).fetchone()
    assert row["status"] == "pending"


# ── 配置解析 ───────────────────────────────────────────────────────────────

def test_parse_config_proactive_defaults_and_overrides(parse_cfg):
    cfg = parse_cfg({})
    assert cfg.proactive_enabled is False
    assert cfg.proactive_daily_budget == 20
    assert cfg.proactive_opt_in_default is False

    cfg2 = parse_cfg(
        {
            "proactive": {
                "proactive_enabled": True,
                "proactive_daily_budget": 5,
                "proactive_per_user_max_per_window": 1,
            }
        }
    )
    assert cfg2.proactive_enabled is True
    assert cfg2.proactive_daily_budget == 5
    assert cfg2.proactive_per_user_max_per_window == 1


def test_parse_config_proactive_flat_legacy_keys(parse_cfg):
    cfg = parse_cfg({"proactive_enabled": True, "proactive_daily_budget": 3})
    assert cfg.proactive_enabled is True
    assert cfg.proactive_daily_budget == 3


# ── adapters/send 端口 ─────────────────────────────────────────────────────

def test_send_port_capability(send_port):
    assert send_port.probe_send_capability(None).available is False
    cap = send_port.probe_send_capability(_FakeContext())
    assert cap.available is True and cap.method == "send_message"
    assert send_port.probe_send_capability(object()).reason == (
        "context_send_message_missing"
    )


def test_version_capability_reports_send_message(plugin_module):
    from astrbot_plugin_tmemory.adapters import version

    version.reset_capability_cache()
    caps = version.get_capabilities()
    # stub 环境无 astrbot.core.star.context → 探测失败，能力为 False（降级路径）
    assert caps.has_send_message is False
    assert "has_send_message" in caps.to_dict()


@pytest.mark.asyncio
async def test_send_port_sends_and_handles_failure(send_port):
    ctx = _FakeContext()
    ok = await send_port.send_message(
        ctx, UMO_A, [{"type": "plain", "text": "hi"}]
    )
    assert ok is True
    assert ctx.calls and ctx.calls[0][0] == UMO_A

    bad = _FakeContext(ok=False)
    assert (
        await send_port.send_message(bad, UMO_A, [{"type": "plain", "text": "x"}])
        is False
    )
    assert (
        await send_port.send_message(ctx, "", [{"type": "plain", "text": "x"}])
        is False
    )
