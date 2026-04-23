"""防误伤测试：关键短消息保护

承接 TMEAAA-39。验证 restriction / task / preference 三类
短消息在 capture 采集层和 distill 预过滤层不被误删。

覆盖路径：
  - main._is_low_info_content()
  - main._should_skip_capture()
  - main._prefilter_distill_rows()
  - main._insert_conversation() 采集入库

故意不保留的短消息（已有明确规则）：
  - 纯感叹词 / 颜文字 / 口头禅（如"哈哈哈"、"ok"、"嗯"）
  - 单字语气词（如"啊"、"哦"）
  - 纯表情符号

新增防误伤样本数量：36 条（采集层 24 + 蒸馏层 12）
涉及代码路径：_is_low_info_content, _should_skip_capture, _prefilter_distill_rows,
              _insert_conversation
"""

import pytest


# =============================================================================
# capture 层：_is_low_info_content 防误伤样本
# =============================================================================

class TestIsLowInfoContent:
    """_is_low_info_content 的单元测试——只测内部判断逻辑，不走完整过滤链。"""

    # ── restriction 类：禁止事项 ──────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "不吃香菜",       # 4 chars：食物禁忌
        "不要辣",         # 3 chars：调料禁忌
        "别说英文",       # 4 chars：语言约束
        "禁止发广告",     # 5 chars：规则约束
        "不接受催促",     # 5 chars：沟通约束
        "不喜欢被叫名字", # 7 chars：社交禁忌
        "绝对不吃榴莲",   # 6 chars：食物强约束
        "回复不要用emoji", # 有效实义词
    ])
    def test_restriction_short_messages_not_low_info(self, plugin, text):
        """restriction 类关键短消息不应被判定为低信息量。"""
        plugin.capture_min_content_len = 5
        assert plugin._is_low_info_content(text) is False, (
            f"误判：restriction 短消息 {repr(text)} 被错误过滤"
        )

    # ── task 类：待办约束 ─────────────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "明天开会",       # 4 chars：日程
        "记得提醒我",     # 5 chars：提醒意图
        "下周要交报告",   # 6 chars：截止日期
        "今天要健身",     # 5 chars：习惯目标
        "戒糖一个月",     # 5 chars：约束目标
    ])
    def test_task_short_messages_not_low_info(self, plugin, text):
        """task 类关键短消息不应被判定为低信息量。"""
        plugin.capture_min_content_len = 5
        assert plugin._is_low_info_content(text) is False, (
            f"误判：task 短消息 {repr(text)} 被错误过滤"
        )

    # ── preference 类：用户偏好 ───────────────────────────────────────────
    @pytest.mark.parametrize("text", [
        "我叫张三",       # 4 chars：用户姓名（fact/preference）
        "我姓李",         # 3 chars：用户姓氏
        "喜欢猫",         # 3 chars：偏好
        "爱喝绿茶",       # 4 chars：饮食偏好
        "爱看科幻",       # 4 chars：阅读偏好
        "讨厌开会",       # 4 chars：工作偏好
    ])
    def test_preference_short_messages_not_low_info(self, plugin, text):
        """preference / fact 类关键短消息不应被判定为低信息量。"""
        plugin.capture_min_content_len = 5
        assert plugin._is_low_info_content(text) is False, (
            f"误判：preference 短消息 {repr(text)} 被错误过滤"
        )

    # ── 仍应过滤的噪声短消息（不应误放行）────────────────────────────────
    @pytest.mark.parametrize("text", [
        "哈哈哈",   # 纯感叹词
        "ok",       # 英文口头禅
        "嗯",       # 单字语气词
        "好的",     # 纯确认词（噪声词）
        "呵呵",     # 纯感叹词
        "嘿嘿",     # 纯感叹词
        "😂😂😂",   # 纯表情符号
        "好",       # 单字
    ])
    def test_noise_messages_correctly_flagged_as_low_info(self, plugin, text):
        """纯噪声短消息应保持被正确过滤（不引入误放行）。"""
        plugin.capture_min_content_len = 5
        assert plugin._is_low_info_content(text) is True, (
            f"误放行：噪声短消息 {repr(text)} 未被正确过滤"
        )


# =============================================================================
# capture 层：_should_skip_capture 防误伤集成测试
# =============================================================================

class TestShouldSkipCapture:
    """_should_skip_capture 的集成测试——覆盖完整四层过滤链。"""

    @pytest.mark.parametrize("text, category", [
        # restriction
        ("不吃香菜",       "restriction"),
        ("不要辣",         "restriction"),
        ("别说英文",       "restriction"),
        ("禁止发广告",     "restriction"),
        # task
        ("明天开会",       "task"),
        ("记得提醒我",     "task"),
        ("下周要交报告",   "task"),
        # preference
        ("我叫张三",       "fact"),
        ("我姓李",         "fact"),
        ("爱喝绿茶",       "preference"),
        ("讨厌开会",       "preference"),
        # 较长但仍属短消息范畴
        ("用户不接受催促", "restriction"),
        ("用户爱看科幻小说", "preference"),
    ])
    def test_critical_short_messages_pass_capture_filter(self, plugin, text, category):
        """关键短消息（restriction/task/preference）不应被采集层过滤掉。"""
        plugin.capture_min_content_len = 5
        assert plugin._should_skip_capture(text) is False, (
            f"采集层误伤：[{category}] {repr(text)} 被错误跳过"
        )

    @pytest.mark.parametrize("text", [
        "哈哈哈",
        "ok",
        "嗯",
        "好的",
        "嘿嘿",
        "😂😂",
    ])
    def test_noise_messages_still_skipped(self, plugin, text):
        """修复后噪声短消息仍被正确跳过。"""
        plugin.capture_min_content_len = 5
        assert plugin._should_skip_capture(text) is True, (
            f"修复引入误放行：{repr(text)} 未被跳过"
        )


# =============================================================================
# capture 层：采集入库验证（_insert_conversation 端到端）
# =============================================================================

class TestCaptureInsertion:
    """验证关键短消息能成功写入 conversation_cache。"""

    @pytest.mark.parametrize("text, category", [
        ("不吃香菜",   "restriction"),
        ("别说英文",   "restriction"),
        ("明天开会",   "task"),
        ("我叫张三",   "fact"),
        ("爱喝绿茶",   "preference"),
        ("不要辣",     "restriction"),
    ])
    @pytest.mark.asyncio
    async def test_critical_message_stored_in_cache(self, plugin, text, category):
        """关键短消息应写入 conversation_cache，不被采集层拦截。"""
        plugin.capture_min_content_len = 5
        plugin.capture_dedup_window = 0  # 关闭去重，确保每次独立测试
        uid = f"test-{category}-{hash(text) & 0xFFFF}"
        await plugin._insert_conversation(
            canonical_id=uid,
            role="user",
            content=text,
            source_adapter="test",
            source_user_id="u1",
            unified_msg_origin="",
        )
        rows = plugin._fetch_pending_rows(uid, 10)
        assert len(rows) == 1, (
            f"[{category}] {repr(text)} 未写入 cache，被采集层误拦截"
        )
        assert rows[0]["content"] == text


# =============================================================================
# 蒸馏层：_prefilter_distill_rows 防误伤样本
# =============================================================================

class TestPrefilterDistillRows:
    """_prefilter_distill_rows 蒸馏预过滤层防误伤测试。"""

    @pytest.mark.parametrize("content, category", [
        # restriction
        ("不吃香菜",       "restriction"),
        ("不要辣",         "restriction"),
        ("别说英文",       "restriction"),
        ("禁止发广告",     "restriction"),
        # task
        ("明天开会",       "task"),
        ("记得提醒我",     "task"),
        ("下周要交报告",   "task"),
        # preference/fact
        ("我叫张三",       "fact"),
        ("我姓李",         "fact"),
        ("爱喝绿茶",       "preference"),
        ("讨厌开会",       "preference"),
        # 混合场景：短用户消息 + 正常 assistant 回复
        ("好的我记住了",   "assistant-ack"),  # assistant 行也不应被误删
    ])
    def test_critical_content_survives_prefilter(self, plugin, content, category):
        """关键内容行应在 _prefilter_distill_rows 中存活，不被过滤。"""
        plugin.capture_min_content_len = 5
        role = "assistant" if category == "assistant-ack" else "user"
        rows = [{"role": role, "content": content}]
        result = plugin._prefilter_distill_rows(rows)
        assert len(result) == 1, (
            f"蒸馏层误伤：[{category}] {repr(content)} 被 _prefilter_distill_rows 过滤"
        )
        assert result[0]["content"] == content

    def test_noise_rows_still_filtered_in_prefilter(self, plugin):
        """修复后蒸馏层仍正确过滤噪声行。"""
        plugin.capture_min_content_len = 5
        noise_rows = [
            {"role": "user",    "content": "哈哈哈"},
            {"role": "user",    "content": "ok"},
            {"role": "summary", "content": "今日摘要blah"},
        ]
        result = plugin._prefilter_distill_rows(noise_rows)
        assert len(result) == 0, "蒸馏层误放行噪声行"

    def test_mixed_batch_keeps_critical_removes_noise(self, plugin):
        """混合批次：关键短消息保留，纯噪声行过滤。"""
        plugin.capture_min_content_len = 5
        rows = [
            {"role": "user",      "content": "哈哈哈"},         # noise
            {"role": "user",      "content": "不吃香菜"},        # restriction → keep
            {"role": "summary",   "content": "今日摘要"},        # summary → filter
            {"role": "user",      "content": "明天开会"},        # task → keep
            {"role": "assistant", "content": "好的，我记住了"},   # valid → keep
            {"role": "user",      "content": "嗯"},             # noise
        ]
        result = plugin._prefilter_distill_rows(rows)
        kept_contents = {r["content"] for r in result}
        assert "不吃香菜" in kept_contents, "restriction 短消息被蒸馏层过滤"
        assert "明天开会" in kept_contents, "task 短消息被蒸馏层过滤"
        assert "好的，我记住了" in kept_contents, "assistant 回复被蒸馏层过滤"
        assert "哈哈哈" not in kept_contents, "噪声行未被蒸馏层过滤"
        assert "今日摘要" not in kept_contents, "summary 行未被蒸馏层过滤"
        assert len(result) == 3
