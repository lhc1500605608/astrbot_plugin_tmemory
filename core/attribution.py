"""Role-based attribution guard for distillation / profile extraction (TMEAAA-457).

Assistant-authored utterances must never be persisted as user facts, preferences
or style. Prompts already instruct the LLM to ignore assistant speech, but a
prompt is a soft constraint: rule fallbacks and paraphrasing models can still
attribute assistant content to the user.

This module provides a deterministic, LLM-independent safety net. Candidate
memories that trace back verbatim to an assistant turn — and cannot be traced to
any user turn — are dropped before persistence. It is intentionally conservative:
content that also appears in a user turn is always kept (the user did say it).

Used by the flat distill pipeline, profile extraction and consolidation
semantic extraction so all three memory-formation paths share one boundary.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence

#: Minimum normalized length for a full assistant utterance to count as an
#: embedded leak when it appears inside a larger candidate memory.
_MIN_UTTERANCE_LEN = 8


def _norm(text: object) -> str:
    """Case-fold and strip every non-word character (incl. CJK-safe spaces)."""
    return re.sub(r"[^\w]", "", str(text or "")).lower()


def _role_texts(rows: Sequence[Dict], role: str) -> List[str]:
    return [
        _norm(r.get("content", ""))
        for r in rows
        if str(r.get("role", "")) == role
    ]


def _contained(needle: str, haystacks: Sequence[str]) -> bool:
    return any(needle in h for h in haystacks if h)


def is_assistant_attributed(
    text: str,
    user_texts: Sequence[str],
    assistant_texts: Sequence[str],
) -> bool:
    """Return True when ``text`` is attributable to an assistant turn only.

    A candidate is *user-attributable* (and therefore kept) when its normalized
    text is contained in any user turn. Otherwise it is rejected when it is
    contained in an assistant turn, or when a substantial assistant utterance is
    embedded inside it.
    """
    needle = _norm(text)
    if not needle:
        return False
    if _contained(needle, user_texts):
        return False
    if _contained(needle, assistant_texts):
        return True
    for utterance in assistant_texts:
        if (
            len(utterance) >= _MIN_UTTERANCE_LEN
            and utterance in needle
            and not _contained(utterance, user_texts)
        ):
            return True
    return False


def filter_assistant_attributed(
    items: Sequence[Dict],
    rows: Sequence[Dict],
    key: str = "memory",
) -> List[Dict]:
    """Drop candidate items whose payload is attributable to assistant speech.

    ``rows`` are the source conversation rows (each with ``role`` / ``content``).
    Only ``role == "assistant"`` utterances are treated as non-user material;
    everything else is ignored by the guard.
    """
    assistant_texts = _role_texts(rows, "assistant")
    if not assistant_texts:
        return list(items)

    user_texts = _role_texts(rows, "user")
    kept: List[Dict] = []
    for item in items:
        if is_assistant_attributed(str(item.get(key, "")), user_texts, assistant_texts):
            continue
        kept.append(item)
    return kept
