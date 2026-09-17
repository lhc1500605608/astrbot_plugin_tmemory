"""蒸馏规则分级门控（Plan TMEAAA-379 T3 / BC-4）。

目标：在 LLM 蒸馏之前用**高精度规则**判断一批对话是否值得消耗 LLM token。

- 命中任一"长期记忆信号" → ``complex`` → 升级 LLM 蒸馏；
- 全部行均无信号 → ``simple`` → 规则直接判定"无可提取记忆"，零 LLM 调用。

设计约束（对应验收）：
- **不改蒸馏对外行为**：规则只做高精度判定，不做宽松抽取，避免规则回退产生低质记忆；
  关闭 ``distill_rule_gating`` 即恢复纯 LLM 路径（回滚开关）。
- **阈值可调**：``distill_rule_gate_min_chars`` 控制长文本兜底升级阈值。
- 本模块为纯函数，无 astrbot/网络依赖，便于离线基准与单测复现。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

# 高精度长期记忆信号：偏好 / 身份事实 / 例行安排 / 目标待办 / 约束禁忌 / 沟通风格。
# 只有明确指向"关于用户的稳定信息"的模式才升级 LLM，降低误升级与误跳过两端风险。
_SIGNAL_PATTERNS = (
    # 偏好
    re.compile(r"喜欢|偏好|爱吃|爱喝|讨厌|不喜欢|最爱|习惯于|习惯用|通常|常用"),
    # 身份 / 事实
    re.compile(r"我是|我叫|我在|我的(?:职业|工作|家乡|生日|年龄|名字)|来自|住在|是[^，。]{0,8}(?:工程师|老师|医生|学生|设计师|律师|会计)"),
    # 例行 / 日程
    re.compile(r"每(?:天|周|月|年|晚|早)|定期|固定(?:时间|安排)|每周[一二三四五六日天]"),
    # 目标 / 待办
    re.compile(r"计划|目标|准备|打算|想要|需要|deadline|待办|提醒我|记一下|记住"),
    # 约束 / 禁忌
    re.compile(r"不要|不能|禁止|忌口|过敏|不吃|戒了|戒掉|限制|控制(?:糖|盐|油|卡)"),
    # 沟通风格
    re.compile(r"回答(?:时|先)|回复(?:时|先)|建议.*先|先(?:给|列).*(?:结论|风险|建议)|语气|风格|措辞|简洁|详细|结论先行"),
    # 英文
    re.compile(r"\b(?:prefer|favorite|usually|always|never|every\s+(?:day|week|month)|remember|i\s+am|i'm|my\s+(?:job|name|hometown|birthday))\b", re.IGNORECASE),
)

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?\n;；]+")


def has_memory_signal(text: str) -> bool:
    """判断单条文本是否包含长期记忆信号（高精度，不做宽松抽取）。"""
    if not text:
        return False
    normalized = str(text).strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _SIGNAL_PATTERNS)


def classify_batch(rows: Iterable[Dict], *, min_chars: int = 40) -> str:
    """对一批对话行分级：``complex``（升级 LLM）或 ``simple``（规则处理）。

    升级条件（满足任一）：
    - 任一 ``user`` 行命中长期记忆信号；
    - 任一 ``user`` 行规范化后长度 >= ``min_chars``（长文本可能暗含稳定信息）。

    仅看 ``user`` 行：助手回复不能证明用户长期属性（与 ``run_distill_cycle`` 现有
    assistant-only 跳过策略一致）。
    """
    threshold = max(1, int(min_chars or 0))
    for row in rows or []:
        if str(row.get("role", "")) != "user":
            continue
        content = str(row.get("content", "") or "").strip()
        if not content:
            continue
        if has_memory_signal(content):
            return "complex"
        if len(content) >= threshold:
            return "complex"
    return "simple"


def split_sentences(text: str) -> List[str]:
    """按句读切分文本（供规则路径/评测复用）。"""
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(str(text or "")) if part.strip()]


__all__ = [
    "classify_batch",
    "has_memory_signal",
    "split_sentences",
]
