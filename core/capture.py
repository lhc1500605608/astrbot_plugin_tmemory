import re
from typing import List
from collections import Counter
from .config import PluginConfig

_NOISE_WORDS = frozenset(
    {
        # 单字语气词
        "嗯", "哦", "啊", "哈", "呢", "吧", "啦", "呀", "哇", "唉",
        # 常见口头禅 / 感叹词（二字）
        "哈哈", "嗯嗯", "哦哦", "呵呵", "嘿嘿", "嗯呐", "好好", "好的", "好吧",
        "好嘞", "嗯哦", "啊啊", "好耶", "收到", "明白", "懂了", "知道",
        # 常见口头禅 / 感叹词（三字及以上）
        "哈哈哈", "呵呵呵", "嘿嘿嘿", "啊啊啊", "好滴好滴", "知道了", "明白了", "没问题",
        # 英文常见
        "ok", "okay", "yeah", "yes", "yep", "no", "nope", "wow", "oh", "ah", "cool",
    }
)

_JUNK_WORD_RE = re.compile(r"^[\U0001F000-\U0001FFFF\u2600-\u27BF😀-🙏🌀-🗿]*$")


class CaptureFilter:
    def __init__(self, cfg: PluginConfig):
        self._cfg = cfg

    def is_low_info_content(self, text: str) -> bool:
        """判断文本是否为低信息量内容（适合在 capture 层跳过）。

        判断逻辑（防误伤优先）：
        1. 先提取实义词（≥2 字符、非噪声词、非颜文字）。
        2. 若有实义词 → 不是低信息量，直接返回 False（保留关键短消息）。
           这确保 restriction / task / preference 类短消息（如"不吃香菜"、
           "明天开会"、"不要辣"）即使字符数低于 capture_min_content_len 也不会被误删。
        3. 若没有实义词，再看有效字符总数：低于 capture_min_content_len 则视为低信息量。
        """
        if self._cfg.capture_min_content_len <= 0:
            return False

        # 步骤 1：计算实义词
        words = [
            w
            for w in re.split(r"[^\w\u4e00-\u9fff]+", text)
            if len(w) >= 2
            and w.lower() not in _NOISE_WORDS
            and not _JUNK_WORD_RE.match(w)
        ]
        # 步骤 2：有实义词 → 保留
        if words:
            return False

        # 步骤 3：无实义词时，用字符长度兜底
        stripped = re.sub(r"[\s\W]+", "", text, flags=re.UNICODE)
        return len(stripped) < self._cfg.capture_min_content_len

    def should_skip_capture(self, text: str) -> bool:
        if self._cfg.no_memory_marker in text:
            return True

        if any(text.startswith(p) for p in self._cfg.capture_skip_prefixes):
            return True

        if self._cfg.capture_skip_regex and self._cfg.capture_skip_regex.search(text):
            return True

        if self.is_low_info_content(text):
            return True

        return False

    @staticmethod
    def get_noise_words():
        return _NOISE_WORDS

    @staticmethod
    def get_junk_word_re():
        return _JUNK_WORD_RE
