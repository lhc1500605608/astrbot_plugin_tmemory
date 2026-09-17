"""Proactive memory engine — Plan TMEAAA-379 T2 (B2) / BC-3.

默认关闭（``proactive_enabled=false``）：关闭时**零调度、零副作用**。
开启后按固定周期运行，触发两类主动消息：

- ``reminder``：``proactive_reminders`` 中 ``due_at`` 到期且 ``status='pending'`` 的提醒；
- ``recall``：基于用户画像（``profile_items``）的主动回忆。

发送前依次校验：平台能力（``adapters.send``）→ 用户 opt-in → 限流（每用户/窗口 +
最小间隔）→ 每日预算。任一不通过则跳过并写审计（``memory_events`` +
``proactive_send_log``）。

本模块属于 ``core/``，不 import astrbot（R1）；上游发送能力经 ``adapters.send``
由运行时 mixin 注入，便于无 AstrBot 环境单测。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("astrbot")

TRIGGER_REMINDER = "reminder"
TRIGGER_RECALL = "recall"

REASON_DISABLED = "disabled"
REASON_CAPABILITY = "capability_unavailable"
REASON_NOT_OPT_IN = "not_opt_in"
REASON_RATE_LIMITED = "rate_limited"
REASON_BUDGET = "budget_exhausted"
REASON_SEND_FAILED = "send_failed"

EVENT_SENT = "proactive_sent"
EVENT_SKIPPED = "proactive_skipped"

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt_ts(epoch: float) -> str:
    return time.strftime(_TS_FMT, time.localtime(epoch))


def _parse_ts(value: str) -> Optional[float]:
    try:
        return time.mktime(time.strptime(str(value), _TS_FMT))
    except Exception:
        return None


def _today_start(epoch: float) -> str:
    return time.strftime("%Y-%m-%d 00:00:00", time.localtime(epoch))


@dataclass(frozen=True)
class ProactiveCandidate:
    """一条待发送的主动消息候选。"""

    canonical_user_id: str
    umo: str
    trigger: str
    text: str
    reminder_id: int = 0


class ProactiveEngine:
    """主动消息调度/门控/审计引擎（纯领域逻辑，可独立单测）。"""

    def __init__(
        self,
        db_manager: Any,
        config: Any,
        memory_logger: Any = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._db_mgr = db_manager
        self._cfg = config
        self._memory_logger = memory_logger
        self._now = now_fn or time.time
        self._capability_available = True
        self._capability_reason = ""

    # ── config / capability ────────────────────────────────────────────────

    def set_config(self, config: Any) -> None:
        self._cfg = config

    def set_capability(self, available: bool, reason: str = "") -> None:
        self._capability_available = bool(available)
        self._capability_reason = str(reason or "")

    @property
    def capability_reason(self) -> str:
        return self._capability_reason

    def is_enabled(self) -> bool:
        return bool(getattr(self._cfg, "proactive_enabled", False))

    # ── user-level policy (opt-in / opt-out) ───────────────────────────────

    def set_user_opt_in(
        self, canonical_user_id: str, opt_in: bool, source: str = "user"
    ) -> None:
        now = _fmt_ts(self._now())
        with self._db_mgr.db() as conn:
            conn.execute(
                "INSERT INTO proactive_user_policy"
                "(canonical_user_id, opt_in, source, updated_at) VALUES(?, ?, ?, ?)"
                " ON CONFLICT(canonical_user_id) DO UPDATE SET"
                " opt_in=excluded.opt_in, source=excluded.source,"
                " updated_at=excluded.updated_at",
                (canonical_user_id, 1 if opt_in else 0, str(source), now),
            )

    def get_user_opt_in(self, canonical_user_id: str) -> bool:
        with self._db_mgr.db() as conn:
            row = conn.execute(
                "SELECT opt_in FROM proactive_user_policy WHERE canonical_user_id=?",
                (canonical_user_id,),
            ).fetchone()
        if row is None:
            return bool(getattr(self._cfg, "proactive_opt_in_default", False))
        return bool(row["opt_in"])

    # ── usage accounting ───────────────────────────────────────────────────

    def count_sent_in_window(
        self, canonical_user_id: str, window_sec: int, now: Optional[float] = None
    ) -> int:
        now = float(now if now is not None else self._now())
        cutoff = _fmt_ts(now - max(0, int(window_sec)))
        with self._db_mgr.db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM proactive_send_log"
                " WHERE canonical_user_id=? AND status='sent' AND created_at >= ?",
                (canonical_user_id, cutoff),
            ).fetchone()
        return int(row["c"]) if row else 0

    def count_sent_today(self, now: Optional[float] = None) -> int:
        now = float(now if now is not None else self._now())
        with self._db_mgr.db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM proactive_send_log"
                " WHERE status='sent' AND created_at >= ?",
                (_today_start(now),),
            ).fetchone()
        return int(row["c"]) if row else 0

    def _last_sent_ts(self, canonical_user_id: str) -> Optional[float]:
        with self._db_mgr.db() as conn:
            row = conn.execute(
                "SELECT created_at FROM proactive_send_log"
                " WHERE canonical_user_id=? AND status='sent'"
                " ORDER BY id DESC LIMIT 1",
                (canonical_user_id,),
            ).fetchone()
        if not row:
            return None
        return _parse_ts(row["created_at"])

    # ── gating ─────────────────────────────────────────────────────────────

    def evaluate(
        self, canonical_user_id: str, now: Optional[float] = None
    ) -> Tuple[bool, str]:
        """返回 (是否允许发送, 跳过原因)。"""
        now = float(now if now is not None else self._now())
        if not self.is_enabled():
            return False, REASON_DISABLED
        if not self._capability_available:
            return False, REASON_CAPABILITY
        if not self.get_user_opt_in(canonical_user_id):
            return False, REASON_NOT_OPT_IN

        window = max(0, int(getattr(self._cfg, "proactive_per_user_window_sec", 0)))
        max_in_window = max(
            0, int(getattr(self._cfg, "proactive_per_user_max_per_window", 0))
        )
        if window > 0 and self.count_sent_in_window(canonical_user_id, window, now) >= max_in_window:
            return False, REASON_RATE_LIMITED

        min_interval = max(0, int(getattr(self._cfg, "proactive_min_interval_sec", 0)))
        last = self._last_sent_ts(canonical_user_id)
        if last is not None and min_interval > 0 and now - last < min_interval:
            return False, REASON_RATE_LIMITED

        budget = max(0, int(getattr(self._cfg, "proactive_daily_budget", 0)))
        if budget > 0 and self.count_sent_today(now) >= budget:
            return False, REASON_BUDGET
        return True, ""

    # ── candidate collection ───────────────────────────────────────────────

    def _clamp(self, text: str) -> str:
        limit = max(20, int(getattr(self._cfg, "proactive_max_message_chars", 200)))
        text = str(text or "")
        if len(text) > limit:
            return text[: limit - 1] + "…"
        return text

    def add_reminder(
        self,
        canonical_user_id: str,
        umo: str,
        text: str,
        due_at: Any,
        now: Optional[float] = None,
    ) -> int:
        now = float(now if now is not None else self._now())
        due = due_at if isinstance(due_at, str) else _fmt_ts(float(due_at))
        with self._db_mgr.db() as conn:
            cur = conn.execute(
                "INSERT INTO proactive_reminders"
                "(canonical_user_id, unified_msg_origin, text, due_at, status, created_at, sent_at)"
                " VALUES(?, ?, ?, ?, 'pending', ?, '')",
                (canonical_user_id, umo, str(text), due, _fmt_ts(now)),
            )
            return int(cur.lastrowid or 0)

    def cancel_reminder(self, reminder_id: int) -> bool:
        with self._db_mgr.db() as conn:
            cur = conn.execute(
                "UPDATE proactive_reminders SET status='cancelled'"
                " WHERE id=? AND status='pending'",
                (int(reminder_id),),
            )
        return bool(cur.rowcount)

    def collect_due_reminders(
        self, now: Optional[float] = None, limit: int = 5
    ) -> List[ProactiveCandidate]:
        now = float(now if now is not None else self._now())
        with self._db_mgr.db() as conn:
            rows = conn.execute(
                "SELECT id, canonical_user_id, unified_msg_origin, text"
                " FROM proactive_reminders"
                " WHERE status='pending' AND due_at <= ?"
                " ORDER BY due_at ASC LIMIT ?",
                (_fmt_ts(now), max(1, int(limit))),
            ).fetchall()
        return [
            ProactiveCandidate(
                canonical_user_id=str(r["canonical_user_id"]),
                umo=str(r["unified_msg_origin"]),
                trigger=TRIGGER_REMINDER,
                text=self._clamp(str(r["text"])),
                reminder_id=int(r["id"]),
            )
            for r in rows
        ]

    def _recall_user_ids(self, limit: int) -> List[str]:
        with self._db_mgr.db() as conn:
            if bool(getattr(self._cfg, "proactive_opt_in_default", False)):
                rows = conn.execute(
                    "SELECT DISTINCT canonical_user_id FROM profile_items"
                    " WHERE status='active' LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT canonical_user_id FROM proactive_user_policy"
                    " WHERE opt_in=1 ORDER BY updated_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        return [str(r["canonical_user_id"]) for r in rows]

    def _recall_seed(self, canonical_user_id: str) -> str:
        with self._db_mgr.db() as conn:
            row = conn.execute(
                "SELECT title, content FROM profile_items"
                " WHERE canonical_user_id=? AND status='active'"
                " ORDER BY importance DESC, updated_at DESC LIMIT 1",
                (canonical_user_id,),
            ).fetchone()
        if not row:
            return ""
        return str(row["content"] or row["title"] or "").strip()

    def _latest_umo(self, canonical_user_id: str) -> str:
        with self._db_mgr.db() as conn:
            row = conn.execute(
                "SELECT unified_msg_origin FROM conversation_cache"
                " WHERE canonical_user_id=? AND unified_msg_origin != ''"
                " ORDER BY id DESC LIMIT 1",
                (canonical_user_id,),
            ).fetchone()
        return str(row["unified_msg_origin"]) if row else ""

    def _build_recall_text(self, seed: str) -> str:
        return self._clamp(f"好久没聊啦～我还记得：{seed}。最近一切都好吗？")

    def collect_recall_candidates(
        self,
        now: Optional[float] = None,
        limit: int = 5,
        exclude: Sequence[str] = (),
    ) -> List[ProactiveCandidate]:
        excluded = set(exclude or ())
        out: List[ProactiveCandidate] = []
        for uid in self._recall_user_ids(max(1, int(limit)) + len(excluded)):
            if uid in excluded:
                continue
            seed = self._recall_seed(uid)
            if not seed:
                continue
            umo = self._latest_umo(uid)
            if not umo:
                continue
            out.append(
                ProactiveCandidate(
                    canonical_user_id=uid,
                    umo=umo,
                    trigger=TRIGGER_RECALL,
                    text=self._build_recall_text(seed),
                )
            )
            if len(out) >= max(1, int(limit)):
                break
        return out

    def collect_candidates(self, now: Optional[float] = None) -> List[ProactiveCandidate]:
        now = float(now if now is not None else self._now())
        limit = max(1, int(getattr(self._cfg, "proactive_max_candidates_per_cycle", 5)))
        out: List[ProactiveCandidate] = []
        seen: set = set()
        if bool(getattr(self._cfg, "proactive_reminder_enabled", True)):
            for cand in self.collect_due_reminders(now, limit):
                out.append(cand)
                seen.add(cand.canonical_user_id)
        if bool(getattr(self._cfg, "proactive_recall_enabled", True)) and len(out) < limit:
            out.extend(
                self.collect_recall_candidates(now, limit - len(out), exclude=sorted(seen))
            )
        return out

    # ── audit ──────────────────────────────────────────────────────────────

    def _insert_log(
        self, cand: ProactiveCandidate, status: str, reason: str, now: float
    ) -> None:
        with self._db_mgr.db() as conn:
            conn.execute(
                "INSERT INTO proactive_send_log"
                "(canonical_user_id, trigger_type, status, reason, message, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (
                    cand.canonical_user_id,
                    cand.trigger,
                    status,
                    reason,
                    cand.text,
                    _fmt_ts(now),
                ),
            )

    def _log_event(
        self, canonical_user_id: str, event_type: str, payload: Dict[str, object]
    ) -> None:
        if self._memory_logger is not None:
            self._memory_logger.log_memory_event(canonical_user_id, event_type, payload)
            return
        with self._db_mgr.db() as conn:
            conn.execute(
                "INSERT INTO memory_events"
                "(canonical_user_id, event_type, payload_json, created_at)"
                " VALUES(?, ?, ?, ?)",
                (
                    canonical_user_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    _fmt_ts(self._now()),
                ),
            )

    def record_sent(self, cand: ProactiveCandidate, now: float) -> None:
        self._insert_log(cand, "sent", "", now)
        self._log_event(
            cand.canonical_user_id,
            EVENT_SENT,
            {"trigger": cand.trigger, "reason": "", "chars": len(cand.text)},
        )
        if cand.trigger == TRIGGER_REMINDER and cand.reminder_id:
            with self._db_mgr.db() as conn:
                conn.execute(
                    "UPDATE proactive_reminders SET status='sent', sent_at=?"
                    " WHERE id=?",
                    (_fmt_ts(now), cand.reminder_id),
                )

    def record_skip(self, cand: ProactiveCandidate, reason: str, now: float) -> None:
        self._insert_log(cand, "skipped", reason, now)
        self._log_event(
            cand.canonical_user_id,
            EVENT_SKIPPED,
            {"trigger": cand.trigger, "reason": reason},
        )

    # ── orchestration ──────────────────────────────────────────────────────

    async def process_candidate(
        self,
        cand: ProactiveCandidate,
        send_fn: Callable[[str, List[Dict[str, str]]], Any],
        now: Optional[float] = None,
    ) -> Dict[str, object]:
        now = float(now if now is not None else self._now())
        allowed, reason = self.evaluate(cand.canonical_user_id, now)
        if not allowed:
            self.record_skip(cand, reason, now)
            return {
                "status": "skipped",
                "reason": reason,
                "trigger": cand.trigger,
                "user": cand.canonical_user_id,
            }

        chain = [{"type": "plain", "text": cand.text}]
        sent = False
        try:
            sent = bool(await send_fn(cand.umo, chain))
        except Exception as e:  # 上游发送失败不应中断调度
            logger.warning("[tmemory] proactive send failed for %s: %s", cand.umo, e)
            sent = False

        if not sent:
            self.record_skip(cand, REASON_SEND_FAILED, now)
            return {
                "status": "skipped",
                "reason": REASON_SEND_FAILED,
                "trigger": cand.trigger,
                "user": cand.canonical_user_id,
            }

        self.record_sent(cand, now)
        return {
            "status": "sent",
            "reason": "",
            "trigger": cand.trigger,
            "user": cand.canonical_user_id,
        }

    async def run_cycle(
        self,
        send_fn: Callable[[str, List[Dict[str, str]]], Any],
        now: Optional[float] = None,
    ) -> List[Dict[str, object]]:
        """执行一轮主动消息调度；关闭/能力不可用时零副作用返回 []。"""
        if not self.is_enabled():
            return []
        if not self._capability_available:
            return []
        now = float(now if now is not None else self._now())
        results: List[Dict[str, object]] = []
        for cand in self.collect_candidates(now):
            results.append(await self.process_candidate(cand, send_fn, now))
        return results


class ProactiveRuntimeMixin:
    """插件运行时接入：worker 生命周期 + 上游发送端口绑定。"""

    def _build_proactive_engine(self):
        from .proactive import ProactiveEngine

        if getattr(self, "_proactive_engine", None) is None:
            self._proactive_engine = ProactiveEngine(
                self._db_mgr, self._cfg, getattr(self, "_memory_logger", None)
            )
        return self._proactive_engine

    def _proactive_send_capability(self):
        from ..adapters import send as _send_adapter

        return _send_adapter.probe_send_capability(getattr(self, "context", None))

    async def _start_proactive_worker(self):
        """按配置启动主动消息 worker；默认关闭时零调度。

        平台能力不可用时禁用并告警（BC-3 风险缓解）。
        """
        if not bool(getattr(self._cfg, "proactive_enabled", False)):
            logger.debug("[tmemory] proactive disabled (proactive_enabled=false)")
            return None

        from ..adapters import send as _send_adapter

        cap = self._proactive_send_capability()
        if not cap.available:
            logger.warning(
                "[tmemory] proactive disabled: %s (%s)",
                cap.reason,
                _send_adapter.SEND_FALLBACK_HINT,
            )
            return None

        engine = self._build_proactive_engine()
        engine.set_capability(True, "")
        self._proactive_task = asyncio.create_task(self._proactive_worker_loop())
        logger.info(
            "[tmemory] proactive worker started (interval=%ss, daily_budget=%s)",
            self._cfg.proactive_interval_sec,
            self._cfg.proactive_daily_budget,
        )
        return self._proactive_task

    async def _proactive_worker_loop(self):
        await asyncio.sleep(15)
        while self._worker_running:
            try:
                from .config import parse_config

                self._cfg = parse_config(self.config or {})
            except Exception:
                pass

            engine = self._build_proactive_engine()
            engine.set_config(self._cfg)
            if self._cfg.proactive_enabled:
                try:
                    await self._run_proactive_cycle()
                except Exception as e:
                    logger.warning("[tmemory] proactive cycle error: %s", e)

            await asyncio.sleep(max(30, int(self._cfg.proactive_interval_sec)))

    async def _run_proactive_cycle(self) -> List[Dict[str, object]]:
        from ..adapters import send as _send_adapter

        engine = self._build_proactive_engine()
        engine.set_config(self._cfg)
        cap = _send_adapter.probe_send_capability(getattr(self, "context", None))
        engine.set_capability(cap.available, cap.reason)
        if not cap.available:
            logger.warning("[tmemory] proactive skipped: %s", cap.reason)
            return []

        async def _send(umo: str, chain):
            return await _send_adapter.send_message(self.context, umo, chain)

        return await engine.run_cycle(_send)


__all__ = [
    "EVENT_SENT",
    "EVENT_SKIPPED",
    "REASON_BUDGET",
    "REASON_CAPABILITY",
    "REASON_DISABLED",
    "REASON_NOT_OPT_IN",
    "REASON_RATE_LIMITED",
    "REASON_SEND_FAILED",
    "TRIGGER_RECALL",
    "TRIGGER_REMINDER",
    "ProactiveCandidate",
    "ProactiveEngine",
    "ProactiveRuntimeMixin",
]
