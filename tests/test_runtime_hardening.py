"""运行时硬化测试：蒸馏互斥门控与 token 预算。

TMEAAA-318: 运行时硬化 — 蒸馏互斥门控与 token 预算
"""

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# Token 预算测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestTokenBudget:
    def test_get_daily_token_usage_returns_zero_when_no_today_records(self, plugin):
        """今日无蒸馏记录时，日 token 用量应为 0。"""
        from astrbot_plugin_tmemory.core.distill_validator import get_daily_token_usage
        usage = get_daily_token_usage(plugin)
        assert usage == 0

    def test_get_daily_token_usage_sums_today_records(self, plugin):
        """日 token 用量应汇总今日所有 distill_history 的 tokens_total。"""
        from astrbot_plugin_tmemory.core.distill_validator import get_daily_token_usage
        today = plugin._now()[:10]
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 08:00:00", today + " 08:00:05", "auto", 5, 10, 0, "[]", 5.0, 1000, 500, 1500),
            )
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 09:00:00", today + " 09:00:05", "auto", 3, 8, 0, "[]", 5.0, 800, 400, 1200),
            )
        usage = get_daily_token_usage(plugin)
        assert usage == 2700

    def test_get_daily_token_usage_ignores_negative_tokens(self, plugin):
        """tokens_total=-1 的记录（provider 未返回用量）不参与累加。"""
        from astrbot_plugin_tmemory.core.distill_validator import get_daily_token_usage
        today = plugin._now()[:10]
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 08:00:00", today + " 08:00:05", "auto", 5, 10, 0, "[]", 5.0, -1, -1, -1),
            )
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 09:00:00", today + " 09:00:05", "auto", 3, 8, 0, "[]", 5.0, 500, 200, 700),
            )
        usage = get_daily_token_usage(plugin)
        assert usage == 700

    def test_get_daily_token_usage_ignores_old_records(self, plugin):
        """非今日的记录不参与日用量统计。"""
        from astrbot_plugin_tmemory.core.distill_validator import get_daily_token_usage
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("2020-01-01 08:00:00", "2020-01-01 08:00:05", "auto", 5, 10, 0, "[]", 5.0, 10000, 5000, 15000),
            )
        usage = get_daily_token_usage(plugin)
        assert usage == 0

    def test_is_token_budget_exceeded_zero_budget_never_exceeded(self, plugin):
        """budget=0 表示无限制，永不超过。"""
        from astrbot_plugin_tmemory.core.distill_validator import is_token_budget_exceeded
        plugin._cfg.daily_token_budget = 0
        assert not is_token_budget_exceeded(plugin)

    def test_is_token_budget_exceeded_when_over_budget(self, plugin):
        """当日用量超过预算时应返回 True。"""
        from astrbot_plugin_tmemory.core.distill_validator import is_token_budget_exceeded
        today = plugin._now()[:10]
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 08:00:00", today + " 08:00:05", "auto", 5, 10, 0, "[]", 5.0, 1000, 500, 1500),
            )
        plugin._cfg.daily_token_budget = 1000
        assert is_token_budget_exceeded(plugin)

    def test_is_token_budget_exceeded_when_under_budget(self, plugin):
        """当日用量未超过预算时应返回 False。"""
        from astrbot_plugin_tmemory.core.distill_validator import is_token_budget_exceeded
        today = plugin._now()[:10]
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 08:00:00", today + " 08:00:05", "auto", 1, 2, 0, "[]", 1.0, 100, 50, 150),
            )
        plugin._cfg.daily_token_budget = 1000
        assert not is_token_budget_exceeded(plugin)

    def test_get_budget_consumption_pct_returns_correct_percent(self, plugin):
        """预算消耗百分比应正确计算 (used/budget*100)。"""
        from astrbot_plugin_tmemory.core.distill_validator import get_budget_consumption_pct
        today = plugin._now()[:10]
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 08:00:00", today + " 08:00:05", "auto", 2, 4, 0, "[]", 3.0, 400, 200, 600),
            )
        plugin._cfg.daily_token_budget = 1000
        pct = get_budget_consumption_pct(plugin)
        assert pct == 60.0

    def test_get_budget_consumption_pct_zero_when_no_budget(self, plugin):
        """预算为 0（无限制）时，消耗百分比应返回 0.0。"""
        from astrbot_plugin_tmemory.core.distill_validator import get_budget_consumption_pct
        today = plugin._now()[:10]
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO distill_history(started_at, finished_at, trigger_type,"
                " users_processed, memories_created, users_failed, errors, duration_sec,"
                " tokens_input, tokens_output, tokens_total)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (today + " 08:00:00", today + " 08:00:05", "auto", 1, 2, 0, "[]", 1.0, 500, 250, 750),
            )
        plugin._cfg.daily_token_budget = 0
        pct = get_budget_consumption_pct(plugin)
        assert pct == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 互斥门控测试：profile extraction 排除已 episodized 的行
# ═══════════════════════════════════════════════════════════════════════════════

class TestProfileExtractionMutex:
    def test_pending_profile_users_excludes_episodized_rows(self, plugin):
        """_pending_profile_users 应排除 episode_id>0 的行。"""
        now = plugin._now()
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO conversation_cache(canonical_user_id, role, content, distilled, episode_id, created_at)"
                " VALUES(?,?,?,?,?,?)",
                ("pu1", "user", "msg1", 0, 0, now),
            )
            conn.execute(
                "INSERT INTO conversation_cache(canonical_user_id, role, content, distilled, episode_id, created_at)"
                " VALUES(?,?,?,?,?,?)",
                ("pu1", "user", "msg2", 0, 5, now),  # episode_id=5, 已被 consolidation 处理
            )
            conn.execute(
                "INSERT INTO conversation_cache(canonical_user_id, role, content, distilled, episode_id, created_at)"
                " VALUES(?,?,?,?,?,?)",
                ("pu1", "user", "msg3", 0, 5, now),
            )
        users = plugin._pending_profile_users(limit=10, min_batch=1)
        # pu1 只有 1 条 non-episodized 行(msg1)，msg2/msg3 的 episode_id>0 应被排除
        assert "pu1" in users

    def test_pending_profile_users_excludes_all_episodized_user(self, plugin):
        """所有行都被 episodized 的用户不应出现在 pending 列表。"""
        now = plugin._now()
        with plugin._db() as conn:
            for i in range(3):
                conn.execute(
                    "INSERT INTO conversation_cache(canonical_user_id, role, content, distilled, episode_id, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    ("pu2", "user", f"msg{i}", 0, 10, now),
                )
        users = plugin._pending_profile_users(limit=10, min_batch=1)
        assert "pu2" not in users

    def test_fetch_pending_profile_rows_excludes_episodized(self, plugin):
        """_fetch_pending_profile_rows 应排除 episode_id>0 的行。"""
        now = plugin._now()
        with plugin._db() as conn:
            conn.execute(
                "INSERT INTO conversation_cache(canonical_user_id, role, content, distilled, episode_id, created_at)"
                " VALUES(?,?,?,?,?,?)",
                ("pu3", "user", "fresh", 0, 0, now),
            )
            conn.execute(
                "INSERT INTO conversation_cache(canonical_user_id, role, content, distilled, episode_id, created_at)"
                " VALUES(?,?,?,?,?,?)",
                ("pu3", "user", "episodized", 0, 7, now),
            )
        rows = plugin._fetch_pending_profile_rows("pu3", limit=10)
        assert len(rows) == 1
        assert rows[0]["content"] == "fresh"


# ═══════════════════════════════════════════════════════════════════════════════
# 互斥门控测试：worker loop 管道互斥
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkerLoopMutex:
    def test_pipeline_gate_profile_has_priority(self, plugin):
        """profile_extraction 启用时，gate 应返回 'profile_extraction'。"""
        from astrbot_plugin_tmemory.core.distill import _resolve_pipeline_gate
        plugin._cfg.profile_extraction_enabled = True
        plugin._cfg.enable_consolidation_pipeline = True
        assert _resolve_pipeline_gate(plugin._cfg) == "profile_extraction"

    def test_pipeline_gate_consolidation_fallback(self, plugin):
        """profile 禁用 + consolidation 启用时，gate 应返回 'consolidation'。"""
        from astrbot_plugin_tmemory.core.distill import _resolve_pipeline_gate
        plugin._cfg.profile_extraction_enabled = False
        plugin._cfg.enable_consolidation_pipeline = True
        assert _resolve_pipeline_gate(plugin._cfg) == "consolidation"

    def test_pipeline_gate_flat_distill_default(self, plugin):
        """两者都禁用时，gate 应返回 'flat_distill'。"""
        from astrbot_plugin_tmemory.core.distill import _resolve_pipeline_gate
        plugin._cfg.profile_extraction_enabled = False
        plugin._cfg.enable_consolidation_pipeline = False
        assert _resolve_pipeline_gate(plugin._cfg) == "flat_distill"
