"""Unit tests for StyleAnalyzer rule-based style analysis methods."""

import pytest
from core.style_analyzer import StyleAnalyzer


@pytest.fixture
def analyzer():
    return StyleAnalyzer()


# ── _detect_catchphrases ────────────────────────────────────────────────────

def test_detect_catchphrases_repeated_phrase_detected(analyzer):
    """Repeated 2-char phrase across multiple messages is detected."""
    msgs = ["哈哈真开心", "今天哈哈笑死", "哈哈好吧", "又是哈哈"]
    result = analyzer._detect_catchphrases(msgs)
    assert "哈哈" in result


def test_detect_catchphrases_below_threshold_excluded(analyzer):
    """Phrases appearing only once are excluded when threshold >= 2."""
    # 6 msgs → threshold = max(2, 2) = 2. Each phrase appears in only 1 msg → all excluded.
    msgs = ["你好世界", "今天开心", "测试数据", "哈哈真好", "嗯嗯是的", "学习编程"]
    result = analyzer._detect_catchphrases(msgs)
    assert result == []


def test_detect_catchphrases_empty_input(analyzer):
    """Empty message list returns empty result."""
    assert analyzer._detect_catchphrases([]) == []


def test_detect_catchphrases_short_messages_filtered(analyzer):
    """Messages shorter than 3 chars after stripping are ignored."""
    msgs = ["ab", "cd", "ef"]  # all < 3 chars after stripping, no CJK
    result = analyzer._detect_catchphrases(msgs)
    assert result == []


def test_detect_catchphrases_single_message_below_threshold(analyzer):
    """A single message cannot meet threshold of 2."""
    msgs = ["你好世界"]
    result = analyzer._detect_catchphrases(msgs)
    assert result == []


# ── _analyze_punctuation ────────────────────────────────────────────────────

def test_analyze_punctuation_normal(analyzer):
    """Mixed punctuation and emoji are counted correctly."""
    msgs = ["你好！", "真的吗？", "哈哈...", "😀👍"]
    result = analyzer._analyze_punctuation(msgs)
    assert result["msg_count"] == 4
    assert result["punct_rate"] > 0
    assert result["emoji_count"] >= 2


def test_analyze_punctuation_empty_input(analyzer):
    """All-empty messages → empty dict."""
    assert analyzer._analyze_punctuation(["", "", ""]) == {}


def test_analyze_punctuation_ellipsis_heavy(analyzer):
    """Frequent ellipsis triggers ellipsis_heavy flag."""
    msgs = ["嗯...", "好吧...", "这个...", "那个..."]
    result = analyzer._analyze_punctuation(msgs)
    assert result["ellipsis_heavy"] is True


def test_analyze_punctuation_exclamation_heavy(analyzer):
    """Frequent exclamation marks trigger exclamation_heavy flag."""
    msgs = ["好！", "太棒了！！", "加油！！！"]
    result = analyzer._analyze_punctuation(msgs)
    assert result["exclamation_heavy"] is True


def test_analyze_punctuation_no_emoji(analyzer):
    """Messages without emoji report emoji_rate=0 and emoji_count=0."""
    msgs = ["你好。", "好的，谢谢。", "再见。"]
    result = analyzer._analyze_punctuation(msgs)
    assert result["emoji_count"] == 0
    assert result["emoji_rate"] == 0.0


# ── _analyze_length ─────────────────────────────────────────────────────────

def test_analyze_length_normal_distribution(analyzer):
    """Average, median, min, max computed correctly."""
    msgs = ["ab", "abcd", "abcdef", "abcdefgh"]
    result = analyzer._analyze_length(msgs)
    assert result["min_chars"] == 2
    assert result["max_chars"] == 8
    assert result["median_chars"] == 6  # sorted [2,4,6,8], idx 2 = 6
    assert result["avg_chars"] == 5.0
    assert result["msg_count"] == 4


def test_analyze_length_empty_input(analyzer):
    """Empty messages filtered out (< 2 chars) → empty dict."""
    assert analyzer._analyze_length(["", "x"]) == {}


def test_analyze_length_single_message(analyzer):
    """Single message: avg=median=min=max=its length."""
    msgs = ["hello world"]
    result = analyzer._analyze_length(msgs)
    assert result["avg_chars"] == 11.0
    assert result["median_chars"] == 11
    assert result["min_chars"] == 11
    assert result["max_chars"] == 11
    assert result["msg_count"] == 1


def test_analyze_length_very_long_message(analyzer):
    """Very long message handled correctly."""
    long_msg = "x" * 500
    short_msg = "hi"
    result = analyzer._analyze_length([long_msg, short_msg])
    assert result["max_chars"] == 500
    assert result["min_chars"] == 2


# ── _detect_tone ────────────────────────────────────────────────────────────

def test_detect_tone_formal(analyzer):
    """High density of formal markers → dominant=formal."""
    msgs = ["您好，请问能否麻烦您帮我查一下？", "感谢您的帮助，谢谢！"]
    result = analyzer._detect_tone(msgs)
    assert result["dominant"] == "formal"
    assert result["formal_rate"] > result["casual_rate"]
    assert result["formal_rate"] > 0.3


def test_detect_tone_casual(analyzer):
    """High density of casual markers → dominant=casual."""
    msgs = ["哈哈好的呢", "好嘞行吧", "ok呀"]
    result = analyzer._detect_tone(msgs)
    assert result["dominant"] == "casual"
    assert result["casual_rate"] > 0.3


def test_detect_tone_humorous(analyzer):
    """High density of humor markers → dominant=humorous."""
    msgs = ["笑死了哈哈哈", "离谱抽象", "绷不住了"]
    result = analyzer._detect_tone(msgs)
    assert result["dominant"] == "humorous"
    assert result["humor_rate"] > 0.3


def test_detect_tone_neutral_when_balanced(analyzer):
    """Equal formal and casual rates → no single dominant → neutral."""
    msgs = ["谢谢", "哈哈"]
    result = analyzer._detect_tone(msgs)
    assert result["dominant"] == "neutral"
    assert result["formal_rate"] == result["casual_rate"]


def test_detect_tone_neutral_below_threshold(analyzer):
    """Rates all below 0.3 → dominant=neutral."""
    msgs = ["你好", "今天天气不错", "嗯好的"]
    result = analyzer._detect_tone(msgs)
    assert result["dominant"] == "neutral"


def test_detect_tone_empty_input(analyzer):
    """Empty input → all rates 0, dominant=neutral."""
    result = analyzer._detect_tone([])
    assert result["dominant"] == "neutral"
    assert result["formal_rate"] == 0.0
    assert result["casual_rate"] == 0.0
    assert result["humor_rate"] == 0.0


# ── analyze (integration) ───────────────────────────────────────────────────

def test_analyze_integration(analyzer):
    """Full analyze() returns all four keys."""
    rows = [
        {"role": "user", "content": "哈哈好的呢！"},
        {"role": "user", "content": "今天真开心哈哈"},
        {"role": "assistant", "content": "是的呢"},
    ]
    result = analyzer.analyze(rows)
    assert "catchphrases" in result
    assert "punctuation" in result
    assert "length" in result
    assert "tone" in result
    assert "哈哈" in result["catchphrases"]


def test_analyze_no_user_messages(analyzer):
    """No user-role messages → empty dict."""
    rows = [{"role": "assistant", "content": "你好"}]
    assert analyzer.analyze(rows) == {}


# ── build_style_context ─────────────────────────────────────────────────────

def test_build_style_context_empty_analysis(analyzer):
    """Empty analysis → empty string."""
    assert analyzer.build_style_context({}) == ""


def test_build_style_context_includes_catchphrases_and_tone(analyzer):
    """Context string includes catchphrases and tone when present."""
    analysis = {
        "catchphrases": ["哈哈", "好的"],
        "tone": {"dominant": "casual"},
    }
    result = analyzer.build_style_context(analysis)
    assert "哈哈" in result
    assert "casual" in result
