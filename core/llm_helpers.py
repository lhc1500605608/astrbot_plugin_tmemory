import json
import logging
from typing import Dict, Any, List, Optional
import re

logger = logging.getLogger("astrbot:db.py")

class LLMHelpers:
    @staticmethod
    def parse_json_object(text: str) -> Optional[Dict[str, object]]:
        """从文本中提取 JSON 对象。"""
        if not text:
            return None
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                    return data if isinstance(data, dict) else None
                except Exception:
                    return None
        return None

    @staticmethod
    def strip_think_tags(text: str) -> str:
        """剥离 <think> 的思维链块，只保留最终 JSON 输出。"""
        stripped = re.sub(
            r"<th(?:ink(?:ing)?|ought)>.*?</th(?:ink(?:ing)?|ought)>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        return stripped if stripped else text

    @staticmethod
    def parse_llm_json_memories(raw_text: str, normalize_text_func, safe_memory_type_func, clamp01_func) -> List[Dict[str, object]]:
        if not raw_text:
            return []

        raw_text = LLMHelpers.strip_think_tags(raw_text)

        data = None
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                chunk = raw_text[start : end + 1]
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    return []
            else:
                return []

        items = data.get("memories") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        result = []
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            mem = normalize_text_func(str(item.get("memory", "")))
            if not mem:
                continue
            result.append(
                {
                    "memory": mem,
                    "memory_type": safe_memory_type_func(item.get("memory_type", "fact")),
                    "importance": clamp01_func(item.get("importance", 0.6)),
                    "confidence": clamp01_func(item.get("confidence", 0.7)),
                    "score": clamp01_func(item.get("score", 0.7)),
                }
            )
        return result
