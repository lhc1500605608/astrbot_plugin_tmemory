"""Stage C: Semantic Extraction — extracts atomic, long-lived memories from episode summaries."""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List

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


class SemanticExtractor:
    """Extracts atomic, long-lived memories from episode summaries."""

    def __init__(self, cfg: PluginConfig):
        self._cfg = cfg

    def build_extraction_prompt(self, episode_summary: str, source_snippets: str) -> str:
        """Build prompt for distilling memories from an episode summary."""
        memory_types = "preference|fact|task|restriction|style"
        lines = [
            "你是高质量记忆蒸馏器。从以下情节摘要和相关对话中提炼长期有价值的用户信息。",
            "仅输出 JSON，不要输出任何解释文字或 markdown 标记。",
            "",
            "输出格式(必须严格遵守):",
            "{",
            '  "memories": [',
            "    {",
            '      "memory": "一句话，主语必须是用户，10-80字，简洁精确",',
            f'      "memory_type": "{memory_types}",',
            '      "importance": 0.0到1.0,',
            '      "confidence": 0.0到1.0,',
            '      "score": 0.0到1.0',
            "    }",
            "  ]",
            "}",
            "",
            "质量规则(严格执行):",
            "1. 只提炼关于用户本人的稳定信息:偏好、身份、习惯、长期目标、约束条件、沟通风格。",
            "2. 严格排除:一次性寒暄、单次提问、AI说的话、情绪化表达、安全敏感信息。",
            "3. memory 字段必须是一个完整的陈述句，主语是用户。",
            "4. 如果没有任何值得长期记住的信息，返回空数组。",
            "5. confidence 低于 0.6 的不要输出。importance 低于 0.4 的不要输出。",
            "6. 最多返回 5 条，宁缺毋滥。",
            "7. 优先从摘要中提取跨会话的稳定模式，而非单次对话的细节。",
            "",
            "情节摘要:",
            episode_summary,
            "",
            "关键对话片段:",
            source_snippets,
        ]
        return "\n".join(lines)

    def parse_memories_json(
        self, raw_text: str, normalize_text_func, safe_memory_type_func, clamp01_func
    ) -> List[Dict]:
        """Parse LLM output into memory items. Reuses the existing parsing pattern."""
        if not raw_text:
            return []
        raw_text = _strip_think_tags(raw_text)
        data = _extract_json_object(raw_text)
        if not isinstance(data, dict):
            return []

        items = data.get("memories")
        if not isinstance(items, list):
            return []

        result = []
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            mem = normalize_text_func(str(item.get("memory", "")))
            if not mem:
                continue
            result.append({
                "memory": mem,
                "memory_type": safe_memory_type_func(item.get("memory_type", "fact")),
                "importance": clamp01_func(item.get("importance", 0.6)),
                "confidence": clamp01_func(item.get("confidence", 0.7)),
                "score": clamp01_func(item.get("score", 0.7)),
            })
        return result
