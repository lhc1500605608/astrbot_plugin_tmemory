"""Rule-based conversation style analysis.

Runs on cached conversation rows before LLM distillation (zero token cost).
Produces structured observations about user communication style that feed into
the distill prompt so the LLM can produce "style"-type memories.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List

# ── Tone marker lexicons ────────────────────────────────────────────────

_FORMAL_MARKERS: frozenset[str] = frozenset({
    "您", "请", "谢谢", "麻烦", "能否", "请问", "感谢", "抱歉",
    "您好", "贵姓", "谨", "敬请", "恳请", "劳驾", "幸会",
})

_CASUAL_MARKERS: frozenset[str] = frozenset({
    "哈哈", "嘿嘿", "嗯嗯", "哦哦", "好的呢", "好嘞", "行吧",
    "ok", "okay", "yeah", "yep", "nope", "cool", "wow",
    "啦", "呀", "哇", "嘛", "呗", "咯", "嗷", "哒",
})

_HUMOR_MARKERS: frozenset[str] = frozenset({
    "笑死", "笑哭", "哈哈哈", "呵呵呵", "笑死我了", "离谱",
    "绝了", "绷不住", "破防", "抽象", "典", "乐", "难绷",
})

_PUNCTUATION_RE = re.compile(r"[。！？…~\.!\?,，、：:；;]")
_ELLIPSIS_RE = re.compile(r"\.{2,}|…{1,}")
_EXCLAMATION_RE = re.compile(r"[！!]{1,}")
_QUESTION_RE = re.compile(r"[？?]{1,}")
_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FFFF\u2600-\u27BF\u2B50\u2764\u2705\u2728😀-🙏🌀-🗿🚀-🛿🇦-🇿★-☆]"
)

# ── Public API ───────────────────────────────────────────────────────────


class StyleAnalyzer:
    """Rule-based analyzer for user conversation style patterns.

    Produces structured observations about:
    - Catchphrases (high-frequency n-grams)
    - Punctuation habits
    - Message length profile
    - Tone profile (formal/casual/humorous)
    """

    def analyze(self, rows: list[dict]) -> dict:
        """Analyze conversation rows and return structured style observations."""
        user_msgs = [str(r["content"]) for r in rows if str(r.get("role", "")) == "user"]
        if not user_msgs:
            return {}

        return {
            "catchphrases": self._detect_catchphrases(user_msgs),
            "punctuation": self._analyze_punctuation(user_msgs),
            "length": self._analyze_length(user_msgs),
            "tone": self._detect_tone(user_msgs),
        }

    def build_style_context(self, analysis: dict) -> str:
        """Build a concise text summary of style analysis for the distill prompt."""
        if not analysis:
            return ""

        parts: list[str] = []

        cps = analysis.get("catchphrases") or []
        if cps:
            parts.append(f"口头禅/高频短语: {', '.join(cps[:6])}")

        punc = analysis.get("punctuation") or {}
        if punc:
            punc_parts = []
            if punc.get("ellipsis_heavy"):
                punc_parts.append("频繁使用省略号")
            if punc.get("exclamation_heavy"):
                punc_parts.append("频繁使用感叹号")
            if punc.get("question_heavy"):
                punc_parts.append("频繁使用问号")
            emoji_rate = punc.get("emoji_rate", 0)
            if emoji_rate > 0.1:
                punc_parts.append(f"高频率使用emoji({emoji_rate:.0%})")
            elif emoji_rate == 0 and punc.get("msg_count", 0) >= 5:
                punc_parts.append("几乎不使用emoji")
            if punc_parts:
                parts.append(f"标点习惯: {'; '.join(punc_parts)}")

        length = analysis.get("length") or {}
        if length:
            avg = length.get("avg_chars", 0)
            if avg > 0:
                if avg < 15:
                    parts.append(f"回复极短(平均{avg:.0f}字)")
                elif avg < 40:
                    parts.append(f"回复简短(平均{avg:.0f}字)")
                elif avg > 120:
                    parts.append(f"回复较长(平均{avg:.0f}字)")

        tone = analysis.get("tone") or {}
        if tone:
            dominant = tone.get("dominant")
            if dominant and dominant != "neutral":
                parts.append(f"语气倾向: {dominant}")

        if not parts:
            return ""

        return "── 用户风格特征(规则分析) ──\n" + "\n".join(f"- {p}" for p in parts)

    # ── internal detectors ──────────────────────────────────────────────

    def _detect_catchphrases(self, msgs: list[str], top_n: int = 8) -> list[str]:
        """Extract high-frequency 2-4 character n-grams from user messages.

        Uses character-level n-grams on punctuation-stripped text, which
        works well for Chinese where meaningful phrases are 2-4 characters.
        """
        # Strip punctuation and whitespace, keep only CJK + alphanumeric
        stripped_msgs = [re.sub(r"[^\w\u4e00-\u9fff]", "", m) for m in msgs]
        stripped_msgs = [m for m in stripped_msgs if len(m) >= 3]

        msg_phrase_sets: list[set[str]] = []
        for text in stripped_msgs:
            phrases: set[str] = set()
            for n in (2, 3):
                for i in range(len(text) - n + 1):
                    ngram = text[i : i + n]
                    if re.search(r"[\u4e00-\u9fff]", ngram):
                        phrases.add(ngram)
            if phrases:
                msg_phrase_sets.append(phrases)

        if not msg_phrase_sets:
            return []

        counter: Counter[str] = Counter()
        for pset in msg_phrase_sets:
            counter.update(pset)

        threshold = max(2, len(msgs) // 3)
        return [p for p, c in counter.most_common(top_n) if c >= threshold]

    def _analyze_punctuation(self, msgs: list[str]) -> dict:
        """Analyze punctuation and emoji usage patterns."""
        total_chars = sum(len(m) for m in msgs)
        if total_chars == 0:
            return {}

        punct_count = len(_PUNCTUATION_RE.findall("".join(msgs)))
        ellipsis_count = len(_ELLIPSIS_RE.findall("".join(msgs)))
        excl_count = len(_EXCLAMATION_RE.findall("".join(msgs)))
        quest_count = len(_QUESTION_RE.findall("".join(msgs)))
        emoji_count = len(_EMOJI_RE.findall("".join(msgs)))

        punct_rate = punct_count / total_chars
        emoji_rate = emoji_count / max(1, len(msgs))

        return {
            "msg_count": len(msgs),
            "ellipsis_heavy": ellipsis_count >= max(2, len(msgs) // 2),
            "exclamation_heavy": excl_count >= max(3, len(msgs) // 2),
            "question_heavy": quest_count >= max(3, len(msgs) // 2),
            "punct_rate": round(punct_rate, 3),
            "emoji_rate": round(emoji_rate, 3),
            "emoji_count": emoji_count,
        }

    def _analyze_length(self, msgs: list[str]) -> dict:
        """Analyze message length distribution."""
        lengths = [len(m) for m in msgs if len(m) >= 2]
        if not lengths:
            return {}

        avg = sum(lengths) / len(lengths)
        sorted_lens = sorted(lengths)
        median = sorted_lens[len(sorted_lens) // 2]

        return {
            "avg_chars": round(avg, 1),
            "median_chars": median,
            "min_chars": sorted_lens[0],
            "max_chars": sorted_lens[-1],
            "msg_count": len(lengths),
        }

    def _detect_tone(self, msgs: list[str]) -> dict:
        """Detect tone profile from marker word frequency."""
        all_text = "".join(msgs)

        formal = sum(1 for m in _FORMAL_MARKERS if m in all_text)
        casual = sum(1 for m in _CASUAL_MARKERS if m in all_text)
        humor = sum(1 for m in _HUMOR_MARKERS if m in all_text)

        # Normalize by message count
        n = max(1, len(msgs))
        f_rate = formal / n
        c_rate = casual / n
        h_rate = humor / n

        if f_rate > c_rate and f_rate > h_rate and f_rate > 0.3:
            dominant = "formal"
        elif h_rate > c_rate and h_rate > f_rate and h_rate > 0.3:
            dominant = "humorous"
        elif c_rate > f_rate and c_rate > h_rate and c_rate > 0.3:
            dominant = "casual"
        else:
            dominant = "neutral"

        return {
            "formal_rate": round(f_rate, 3),
            "casual_rate": round(c_rate, 3),
            "humor_rate": round(h_rate, 3),
            "dominant": dominant,
        }


# ── singleton ────────────────────────────────────────────────────────────

_style_analyzer: StyleAnalyzer | None = None


def get_style_analyzer() -> StyleAnalyzer:
    global _style_analyzer
    if _style_analyzer is None:
        _style_analyzer = StyleAnalyzer()
    return _style_analyzer
