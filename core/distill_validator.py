"""蒸馏输出校验与历史/成本记录辅助。

所有函数/类接受 plugin 实例以复用其 `_db`, `_now`, `_distill_mgr` 等依赖。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List

logger = logging.getLogger("astrbot")


# ── 废话/低质量关键词 ──
JUNK_PATTERNS = [
    re.compile(
        r"^(你好|您好|嗨|hi|hello|hey|哈哈|嗯|哦|好的|ok|okay|谢谢|再见|拜拜)",
        re.IGNORECASE,
    ),
    re.compile(r"^(用户说|用户问|用户发送|assistant|AI|助手)", re.IGNORECASE),
    re.compile(r"^.{0,5}$"),  # 太短
]
# 真实凭据「值形态」：只匹配密钥/令牌本身，而不是 password/token/key 等裸词，
# 否则「token 消耗」「token plan」「API key 轮换流程」等正常记忆会被误拦。
CREDENTIAL_PATTERNS = [
    re.compile(r"\b(?:sk|tp|abk)-[A-Za-z0-9_-]{16,}"),  # 常见 API Key 前缀
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
    re.compile(
        r"(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token"
        r"|password|passwd|密码|密钥)\s*(?:[:：=]|是|为)\s*\S{8,}",
        re.IGNORECASE,
    ),
]
UNSAFE_PATTERNS = [
    *CREDENTIAL_PATTERNS,
    re.compile(r"(杀|死|炸|毒|枪|赌博|色情|porn)", re.IGNORECASE),
    re.compile(
        r"(ignore.*(previous|above)|忽略.*(之前|以上)|system.?prompt|越狱|jailbreak)",
        re.IGNORECASE,
    ),
]

# ── 日期限定内容防误记（TMEAAA-593）──
# 相对时间词把语句锚定在「当下」，由此换算出的日期不得固化为稳定属性。
RELATIVE_TIME_TOKENS = (
    "这月", "本月", "这个月", "这几个月", "这周", "本周", "这星期", "这个星期",
    "这礼拜", "这几天", "这两天", "这阵子", "这段时间", "最近", "近期", "今年",
    "这次", "今天", "明天", "后天", "昨天", "前天", "下周", "下个月", "下月",
)
# 记忆文本中出现的日期引用：说明该条可能由相对时间换算而来。
DATE_REF_PATTERNS = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{1,2}\s*月"),
    re.compile(r"\d{1,2}\s*[日号]"),
)
ABSOLUTE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 只能作为稳定属性存在的类型；event 允许承载日期限定内容。
_ATTRIBUTE_TYPES = frozenset({"fact", "preference", "task", "restriction"})


def has_relative_time(text: str) -> bool:
    return any(tok in text for tok in RELATIVE_TIME_TOKENS)


def has_date_ref(text: str) -> bool:
    return any(pat.search(text) for pat in DATE_REF_PATTERNS)


def guard_relative_date_items(
    items: list[dict[str, object]], transcript: str
) -> list[dict[str, object]]:
    """剔除「日期限定内容被误记为稳定属性」的条目。

    仅当本批用户发言含相对时间词时生效：此时任何被标为
    fact/preference/task/restriction 却带日期引用的条目都属当下信息。
    - 记忆内含绝对日期 ``YYYY-MM-DD`` → 规范化为 ``event`` 并补 ``event_date``；
    - 否则无法确定为稳定属性 → 丢弃。

    ``event`` 条目与非相对时间来源的条目不受影响。
    """
    if not transcript or not has_relative_time(transcript):
        return items
    kept: list[dict[str, object]] = []
    for item in items:
        mtype = str(item.get("memory_type", ""))
        mem = str(item.get("memory", ""))
        if mtype in _ATTRIBUTE_TYPES and has_date_ref(mem):
            matched = ABSOLUTE_DATE_RE.search(mem)
            if not matched:
                logger.debug(
                    "[tmemory] date-limited attribute dropped (relative time): %s",
                    mem[:60],
                )
                continue
            item = dict(item)
            item["memory_type"] = "event"
            if not str(item.get("event_date", "") or "").strip():
                item["event_date"] = matched.group(0)
            logger.debug(
                "[tmemory] date-limited attribute normalized to event: %s", mem[:60]
            )
        kept.append(item)
    return kept


def is_junk_memory(text: str) -> bool:
    """检测废话记忆。"""
    for pat in JUNK_PATTERNS:
        if pat.search(text):
            return True
    if len(set(text.replace(" ", ""))) <= 3:
        return True
    meaningful_chars = len(re.sub(r"[^\w一-鿿]", "", text))
    if meaningful_chars < 5:
        return True
    return False


def is_unsafe_memory(text: str) -> bool:
    """安全审计:检测不安全/有害/注入内容。"""
    for pat in UNSAFE_PATTERNS:
        if pat.search(text):
            return True
    return False


def validate_distill_output(
    plugin, items: List[Dict[str, object]], transcript: str = ""
) -> List[Dict[str, object]]:
    """校验 LLM 蒸馏输出:安全审计 + 废话过滤 + 低置信度剪枝 + 日期限定防误记。

    ``transcript`` 为本批用户发言原文；提供时用于剔除由相对时间词换算出的
    伪稳定属性（TMEAAA-593），未提供则跳过该步。
    """
    valid: List[Dict[str, object]] = []
    cfg = plugin._cfg
    for item in items:
        mem = str(item.get("memory", "")).strip()

        if not mem or len(mem) < 6:
            continue
        if len(mem) > 300:
            mem = mem[:300]
            item["memory"] = mem

        if is_junk_memory(mem):
            logger.debug("[tmemory] junk memory filtered: %s", mem[:60])
            continue

        if is_unsafe_memory(mem):
            logger.warning("[tmemory] unsafe memory blocked: %s", mem[:60])
            continue

        mtype = str(item.get("memory_type", ""))
        if mtype not in {"preference", "fact", "task", "restriction", "style", "event"}:
            item["memory_type"] = plugin._distill_mgr.infer_memory_type(mem)
            mtype = str(item.get("memory_type", ""))

        for field in ("score", "importance", "confidence"):
            try:
                v = float(item.get(field, 0.5))
                item[field] = max(0.0, min(1.0, v))
            except (TypeError, ValueError):
                item[field] = 0.5

        confidence = float(item.get("confidence", 0))
        importance = float(item.get("importance", 0))

        if confidence < 0.4:
            logger.debug(
                "[tmemory] low confidence pruned: %.2f %s",
                confidence, mem[:60],
            )
            continue

        if importance < 0.3:
            logger.debug(
                "[tmemory] low importance pruned: %.2f %s",
                importance, mem[:60],
            )
            continue

        valid.append(item)
    return guard_relative_date_items(valid, transcript)


def record_distill_history(
    plugin,
    started_at: str,
    trigger: str,
    users_processed: int,
    memories_created: int,
    users_failed: int,
    errors: list,
    duration: float,
    tokens_input: int = -1,
    tokens_output: int = -1,
    tokens_total: int = -1,
    rule_gated_batches: int = -1,
    prompt_cache_hits: int = -1,
):
    with plugin._db() as conn:
        conn.execute(
            """
            INSERT INTO distill_history(
                started_at, finished_at, trigger_type, users_processed,
                memories_created, users_failed, errors, duration_sec,
                tokens_input, tokens_output, tokens_total,
                rule_gated_batches, prompt_cache_hits
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                plugin._now(),
                trigger,
                users_processed,
                memories_created,
                users_failed,
                json.dumps(errors, ensure_ascii=False),
                duration,
                tokens_input,
                tokens_output,
                tokens_total,
                rule_gated_batches,
                prompt_cache_hits,
            ),
        )


def get_distill_history(plugin, limit: int = 20) -> List[Dict]:
    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT * FROM distill_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_budget_consumption_pct(plugin) -> float:
    """返回当日 token 预算消耗百分比（0.0 ~ 100.0）。

    预算为 0 时返回 0.0（表示无限制）。
    """
    budget = max(0, getattr(plugin._cfg, "daily_token_budget", 0))
    if budget <= 0:
        return 0.0
    used = get_daily_token_usage(plugin)
    return round(used / budget * 100, 1)


def get_daily_token_usage(plugin) -> int:
    """查询今日所有 distill_history 记录的 tokens_total 总和。

    tokens_total < 0 的记录（provider 未返回用量）不参与累加。
    """
    today = plugin._now()[:10]
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens_total), 0) AS total"
            " FROM distill_history"
            " WHERE started_at >= ? AND tokens_total >= 0",
            (today + " 00:00:00",),
        ).fetchone()
    return int(row["total"])


def is_token_budget_exceeded(plugin) -> bool:
    """检查当日 token 用量是否已超过日预算。

    预算为 0 时表示无限制，永不超过。
    """
    budget = getattr(plugin._cfg, "daily_token_budget", 0)
    if budget <= 0:
        return False
    return get_daily_token_usage(plugin) >= budget


def get_distill_cost_summary(plugin, last_n: int = 10) -> Dict:
    """汇总最近 N 轮蒸馏的 token 消耗。"""
    rows = get_distill_history(plugin, limit=last_n)
    total_in = 0
    total_out = 0
    total_total = 0
    has_usage = False
    for r in rows:
        ti = r.get("tokens_input", -1)
        to_ = r.get("tokens_output", -1)
        tt = r.get("tokens_total", -1)
        if ti >= 0:
            total_in += ti
            has_usage = True
        if to_ >= 0:
            total_out += to_
        if tt >= 0:
            total_total += tt
    return {
        "runs": len(rows),
        "has_usage": has_usage,
        "tokens_input": total_in,
        "tokens_output": total_out,
        "tokens_total": total_total,
    }
