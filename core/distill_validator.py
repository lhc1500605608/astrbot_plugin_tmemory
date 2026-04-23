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
UNSAFE_PATTERNS = [
    re.compile(r"(password|passwd|密码|secret|token|api.?key|bearer)", re.IGNORECASE),
    re.compile(r"(杀|死|炸|毒|枪|赌博|色情|porn)", re.IGNORECASE),
    re.compile(
        r"(ignore.*(previous|above)|忽略.*(之前|以上)|system.?prompt|越狱|jailbreak)",
        re.IGNORECASE,
    ),
]


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
    plugin, items: List[Dict[str, object]]
) -> List[Dict[str, object]]:
    """校验 LLM 蒸馏输出:安全审计 + 废话过滤 + 低置信度剪枝。"""
    valid: List[Dict[str, object]] = []
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
        if mtype not in {"preference", "fact", "task", "restriction", "style"}:
            item["memory_type"] = plugin._distill_mgr.infer_memory_type(mem)

        for field in ("score", "importance", "confidence"):
            try:
                v = float(item.get(field, 0.5))
                item[field] = max(0.0, min(1.0, v))
            except (TypeError, ValueError):
                item[field] = 0.5

        if float(item.get("confidence", 0)) < 0.4:
            logger.debug(
                "[tmemory] low confidence pruned: %.2f %s",
                item["confidence"],
                mem[:60],
            )
            continue

        if float(item.get("importance", 0)) < 0.3:
            logger.debug(
                "[tmemory] low importance pruned: %.2f %s",
                item["importance"],
                mem[:60],
            )
            continue

        valid.append(item)
    return valid


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
):
    with plugin._db() as conn:
        conn.execute(
            """
            INSERT INTO distill_history(
                started_at, finished_at, trigger_type, users_processed,
                memories_created, users_failed, errors, duration_sec,
                tokens_input, tokens_output, tokens_total
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )


def get_distill_history(plugin, limit: int = 20) -> List[Dict]:
    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT * FROM distill_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


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
