"""Stage B: Episodic Summarization — groups conversations into episodes and summarizes them via LLM."""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from .config import PluginConfig

logger = logging.getLogger("astrbot")

_THINK_RE = re.compile(
    r"<th(?:ink(?:ing)?|ought)>.*?</th(?:ink(?:ing)?|ought)>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_think_tags(text: str) -> str:
    stripped = _THINK_RE.sub("", text).strip()
    return stripped if stripped else text


def _extract_json_object(text: str) -> Optional[Dict]:
    """Extract a JSON object from text that may have surrounding content."""
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _parse_iso_timestamp(ts: str) -> float:
    """Parse an ISO-ish timestamp to Unix epoch. Returns 0 on failure."""
    if not ts:
        return 0.0
    try:
        if "T" in ts:
            import datetime
            ts_clean = ts.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(ts_clean)
            return dt.timestamp()
        return 0.0
    except (ValueError, TypeError):
        return 0.0


class EpisodeManager:
    """Groups conversations into episodes and summarizes them via LLM."""

    def __init__(self, cfg: PluginConfig):
        self._cfg = cfg

    def build_summarization_prompt(self, transcript: str) -> str:
        lines = [
            "你是对话情节摘要器。从以下用户对话中提取一个连贯的情节摘要。",
            "",
            "输出格式(仅JSON，不要任何解释或markdown):",
            "{",
            '  "episode_title": "简短标题(2-10字)",',
            '  "episode_summary": "2-5句话概括本段对话的情节。300字以内。",',
            '  "topic_tags": ["标签1", "标签2"],',
            '  "key_entities": ["关键实体1", "关键实体2"],',
            '  "status": "ongoing|resolved|background",',
            '  "importance": 0.0到1.0,',
            '  "confidence": 0.0到1.0',
            "}",
            "",
            "规则:",
            "1. 只提炼关于用户的信息(需求、偏好、决定、进展)，忽略AI回复。",
            "2. 如果对话太少或没有实质内容，importance和confidence设低(<0.3)。",
            "3. topic_tags最多5个，每个2-6字。",
            "4. key_entities最多5个。",
            "5. status: ongoing=话题仍在进行, resolved=问题已解决, background=背景信息。",
            "",
            "对话:",
            transcript,
        ]
        return "\n".join(lines)

    def build_stricter_prompt(self, transcript: str) -> str:
        """Retry prompt with stricter formatting instructions after a parse failure."""
        lines = [
            "你是对话情节摘要器。从以下用户对话中提取一个连贯的情节摘要。",
            "",
            "重要:你必须且只能输出一个合法的JSON对象，不要输出任何其他文字。",
            "不要使用markdown代码块(```)。确保JSON中的字符串使用双引号。",
            "",
            "输出格式:",
            '{"episode_title":"标题","episode_summary":"摘要","topic_tags":["标签"],'
            '"key_entities":["实体"],"status":"ongoing","importance":0.5,"confidence":0.5}',
            "",
            "对话:",
            transcript,
        ]
        return "\n".join(lines)

    def parse_episode_json(self, raw_text: str) -> Optional[Dict]:
        """Parse LLM output into episode dict. Returns None on failure."""
        if not raw_text:
            return None
        raw_text = _strip_think_tags(raw_text)
        data = _extract_json_object(raw_text)
        if not isinstance(data, dict):
            return None

        title = str(data.get("episode_title", "")).strip()
        summary = str(data.get("episode_summary", "")).strip()
        if not title or not summary or len(summary) < 10:
            return None

        tags = data.get("topic_tags", [])
        if not isinstance(tags, list):
            tags = []
        entities = data.get("key_entities", [])
        if not isinstance(entities, list):
            entities = []

        status = str(data.get("status", "ongoing")).strip().lower()
        if status not in {"ongoing", "resolved", "background"}:
            status = "ongoing"

        importance = _clamp(float(data.get("importance", 0.5)))
        confidence = _clamp(float(data.get("confidence", 0.5)))

        return {
            "episode_title": title[:100],
            "episode_summary": summary[:600],
            "topic_tags": json.dumps(tags[:5], ensure_ascii=False),
            "key_entities": json.dumps(entities[:5], ensure_ascii=False),
            "status": status,
            "importance": importance,
            "confidence": confidence,
        }

    def extractive_summary(self, rows: List[Dict]) -> Dict:
        """Rule-based fallback: concatenate first 100 chars of each user message."""
        user_lines = []
        for r in rows:
            if str(r.get("role", "")) == "user":
                content = str(r.get("content", ""))[:100]
                if content.strip():
                    user_lines.append(content)
        if not user_lines:
            return {
                "episode_title": "未命名对话",
                "episode_summary": "(无内容)",
                "topic_tags": "[]",
                "key_entities": "[]",
                "status": "ongoing",
                "importance": 0.3,
                "confidence": 0.2,
            }
        combined = "; ".join(user_lines[:20])
        return {
            "episode_title": user_lines[0][:30] if user_lines else "未命名对话",
            "episode_summary": combined[:600],
            "topic_tags": "[]",
            "key_entities": "[]",
            "status": "ongoing",
            "importance": 0.3,
            "confidence": 0.2,
        }

    def group_conversations_into_sessions(
        self, rows: List[Dict]
    ) -> List[List[Dict]]:
        """Group conversation rows into sessions by time proximity.

        Rows are assumed sorted by created_at ASC. A new session starts when
        the gap between consecutive messages exceeds episode_session_gap_minutes.
        """
        if not rows:
            return []

        gap_sec = self._cfg.episode_session_gap_minutes * 60
        sessions: List[List[Dict]] = []
        current: List[Dict] = []

        for row in rows:
            if not current:
                current.append(row)
                continue

            prev_ts = _parse_iso_timestamp(str(current[-1].get("created_at", "")))
            this_ts = _parse_iso_timestamp(str(row.get("created_at", "")))
            if prev_ts > 0 and this_ts > 0 and (this_ts - prev_ts) > gap_sec:
                sessions.append(current)
                current = [row]
            else:
                current.append(row)

        if current:
            sessions.append(current)
        return sessions
