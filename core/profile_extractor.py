"""Profile Extractor — extracts structured user profile items directly from conversation transcripts."""

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


class ProfileExtractor:
    """Extracts structured user profile items directly from conversation transcripts."""

    FACET_TYPES = ("preference", "fact", "style", "restriction", "task_pattern")

    def __init__(self, cfg: "PluginConfig"):
        self._cfg = cfg

    def build_extraction_prompt(self, transcript: str) -> str:
        lines = [
            "你是用户画像提取器。从对话中提取关于用户的稳定、长期有价值的结构化画像信息。",
            "仅输出 JSON，不要输出任何解释文字或 markdown 标记。",
            "",
            "输出格式(必须严格遵守):",
            "{",
            '  "profile_items": [',
            "    {",
            '      "facet_type": "preference|fact|style|restriction|task_pattern",',
            '      "title": "简短标题(2-10字，供UI展示)",',
            '      "content": "一句话，主语必须是用户，10-80字，简洁精确",',
            '      "importance": 0.0到1.0,',
            '      "confidence": 0.0到1.0',
            "    }",
            "  ]",
            "}",
            "",
            "facet_type 定义:",
            "- preference: 用户的偏好、喜好、习惯",
            "- fact: 关于用户的客观事实(身份、技能、经历)",
            "- style: 用户的沟通风格特征(口头禅、语气、回复习惯)",
            "- restriction: 用户的限制条件、禁忌、边界",
            "- task_pattern: 用户反复出现的任务模式、工作流",
            "",
            "质量规则(严格执行):",
            "1. 只提炼关于用户本人的稳定信息。",
            "2. 严格排除:一次性寒暄、单次提问、AI说的话、情绪化表达、安全敏感信息。",
            "3. content 字段必须是一个完整的陈述句，主语是用户。",
            "4. confidence 低于 0.6 的不要输出。importance 低于 0.4 的不要输出。",
            "5. 最多返回 8 条，宁缺毋滥。空数组是合法的。",
            "6. 优先提取跨对话的稳定模式，而非单次对话的细节。",
            "",
            "对话:",
            transcript,
        ]
        return "\n".join(lines)

    def parse_profile_json(
        self, raw_text: str, normalize_text_func, safe_facet_func, clamp01_func
    ) -> List[Dict]:
        if not raw_text:
            return []
        raw_text = _strip_think_tags(raw_text)
        data = _extract_json_object(raw_text)
        if not isinstance(data, dict):
            return []

        items = data.get("profile_items")
        if not isinstance(items, list):
            return []

        result = []
        for item in items[:9]:
            if not isinstance(item, dict):
                continue
            content = normalize_text_func(str(item.get("content", "")))
            if not content:
                continue
            title = normalize_text_func(str(item.get("title", "")))[:100]
            result.append({
                "facet_type": safe_facet_func(item.get("facet_type", "fact")),
                "title": title,
                "content": content,
                "importance": clamp01_func(item.get("importance", 0.6)),
                "confidence": clamp01_func(item.get("confidence", 0.7)),
            })
        return result

    @staticmethod
    def safe_facet_type(raw: str) -> str:
        t = str(raw).strip().lower()
        if t in ProfileExtractor.FACET_TYPES:
            return t
        if t in ("pref", "preferences"):
            return "preference"
        if t in ("facts",):
            return "fact"
        if t in ("styles",):
            return "style"
        if t in ("restrictions", "constraint", "constraints", "boundary"):
            return "restriction"
        if t in ("task_patterns", "task", "tasks", "workflow"):
            return "task_pattern"
        return "fact"
