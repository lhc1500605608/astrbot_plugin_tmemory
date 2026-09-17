import re
import asyncio
import logging
import os
from typing import List, Optional, Dict
from dataclasses import dataclass, field

logger = logging.getLogger("astrbot")

@dataclass
class PluginConfig:
    # Base
    cache_max_rows: int = 20
    memory_max_chars: int = 220
    
    # Capture
    enable_auto_capture: bool = True
    capture_assistant_reply: bool = True
    capture_skip_prefixes: List[str] = field(default_factory=lambda: ["提醒 #"])
    capture_skip_regex: Optional[re.Pattern] = None
    capture_min_content_len: int = 5
    capture_dedup_window: int = 10
    no_memory_marker: str = "\x00[astrbot:no-memory]\x00"
    
    # Distill
    distill_interval_sec: int = 17280
    distill_min_batch_count: int = 20
    distill_batch_limit: int = 80
    distill_pause: bool = False
    distill_user_throttle_sec: int = 0
    use_independent_distill_model: bool = False
    distill_provider_id: str = ""
    distill_model_id: str = ""

    # ── Distill cost reduction (Plan TMEAAA-379 B3 / BC-4) ──
    # 默认关闭：distill_rule_gating=false 时与 v0.10.0 完全一致（纯 LLM 路径）。
    distill_rule_gating: bool = False
    distill_rule_gate_min_chars: int = 40
    # 蒸馏 prompt/结果缓存：相同 transcript 复用上次 LLM 产出，缓存命中不计 token。
    distill_prompt_cache: bool = True
    distill_prompt_cache_max_rows: int = 1000
    
    # Purify / Refine
    purify_interval_days: int = 0
    purify_provider_id: str = ""
    purify_model_id: str = ""
    purify_min_score: float = 0.0
    manual_purify_default_mode: str = "both"
    manual_purify_default_limit: int = 20
    
    # Vector
    enable_vector_search: bool = False
    embedding_source: str = "provider"  # provider | standalone | local（默认 provider 优先）
    embedding_provider_id: str = ""     # AstrBot Embedding Provider ID（留空自动选）
    embed_provider_id: str = ""
    embed_model_id: str = ""
    embed_dim: int = 1536
    auto_rebuild_on_dim_change: bool = True
    vector_weight: float = 0.4
    min_vector_sim: float = 0.15
    embed_base_url: str = ""
    embed_api_key: str = ""

    # ── Local embedding（Plan TMEAAA-379 B3）──
    # 本地 bge-small-zh-v1.5 ONNX（tools/download_bge_onnx.py 下载），零 API 成本。
    local_embedding_path: str = "data/bge-small-zh-v1.5"
    local_embedding_model_file: str = "model_quantized.onnx"
    local_embedding_max_length: int = 512
        
    # Rerank
    enable_reranker: bool = False
    rerank_provider_id: str = ""
    rerank_model_id: str = ""
    rerank_top_n: int = 5
    rerank_base_url: str = ""
    
    # ── Token Budget ──
    daily_token_budget: int = 0  # 0 = unlimited

    # Active Tool Mode
    memory_mode: str = "hybrid"  # distill_only | active_only | hybrid

    # ── Profile Storage ──
    profile_extraction_enabled: bool = False
    profile_extraction_min_messages: int = 8
    profile_extraction_max_users_per_cycle: int = 10
    profile_extraction_timeout_sec: int = 120
    profile_stability_default: float = 0.5
    profile_auto_archive_threshold: float = 0.0
    profile_max_items_per_user: int = 200

    # ── Consolidation Pipeline (deprecated: replaced by profile extraction) ──
    enable_consolidation_pipeline: bool = False
    enable_episodic_summarization: bool = True
    enable_episode_semantic_distill: bool = True
    distill_max_users_per_cycle: int = 10
    stage_timeout_sec: int = 120
    use_independent_consolidation_model: bool = False
    consolidation_provider_id: str = ""
    consolidation_model_id: str = ""
    episode_summary_min_messages: int = 5
    episode_summary_max_input_tokens: int = 3000
    episode_session_gap_minutes: int = 60

    # Injection & Scope
    enable_memory_injection: bool = True
    inject_enable_vector_search: bool = False
    memory_scope: str = "user"
    private_memory_in_group: bool = False
    inject_position: str = "system_prompt"
    inject_slot_marker: str = "{{tmemory}}"
    inject_memory_limit: int = 5
    inject_max_chars: int = 0

    # ── Deprecated injection configs (no longer drive main logic; kept for backward compat) ──
    enable_layered_injection: bool = False
    inject_working_turns: int = 5
    inject_episode_limit: int = 3
    inject_episode_max_chars: int = 600
    inject_style_max_chars: int = 400

    # ── Session Lifecycle (/new /reset) — Plan TMEAAA-354 Phase 4a ──
    # keep（默认）: 仅记录日志，缓存与长期记忆全部保留
    # archive: 会话缓存软归档（标记 archived_at），工作上下文不再召回
    # clear: 删除该会话缓存（被证据引用的行保留）
    session_reset_policy: str = "keep"

    # ── Proactive memory (Plan TMEAAA-379 B2 / BC-3) ──
    # 默认关闭：proactive_enabled=false 时零调度、零副作用；回滚即置回 false。
    proactive_enabled: bool = False
    proactive_interval_sec: int = 300
    proactive_max_candidates_per_cycle: int = 5
    proactive_per_user_window_sec: int = 3600
    proactive_per_user_max_per_window: int = 3
    proactive_min_interval_sec: int = 300
    proactive_daily_budget: int = 20  # 0 = 不限制
    proactive_reminder_enabled: bool = True
    proactive_recall_enabled: bool = True
    proactive_opt_in_default: bool = False
    proactive_max_message_chars: int = 200


def _safe_int(value, default: int, *, label: str = "") -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as _e:
        if label:
            logger.warning("[tmemory] config %s invalid (%r), using default %s: %s", label, value, default, _e)
        return default

def _safe_float(value, default: float, *, label: str = "") -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as _e:
        if label:
            logger.warning("[tmemory] config %s invalid (%r), using default %s: %s", label, value, default, _e)
        return default

def _safe_bool(value, default: bool, *, label: str = "") -> bool:
    if isinstance(value, str):
        if value.strip().lower() in {"false", "0", "no", "off", ""}:
            return False
        if value.strip().lower() in {"true", "1", "yes", "on"}:
            return True
        if label:
            logger.warning("[tmemory] config %s invalid bool string (%r), using default %s", label, value, default)
        return default
    return bool(value)

def parse_config(raw_config: dict) -> PluginConfig:
    """从原始字典解析并返回类型安全的 PluginConfig。"""
    c = PluginConfig()
    
    # ── 基础配置 ──
    c.cache_max_rows = _safe_int(raw_config.get("cache_max_rows", 20), 20, label="cache_max_rows")
    c.memory_max_chars = _safe_int(raw_config.get("memory_max_chars", 220), 220, label="memory_max_chars")

    # ── 自动采集 ──
    c.enable_auto_capture = _safe_bool(raw_config.get("enable_auto_capture", True), True, label="enable_auto_capture")
    c.capture_assistant_reply = _safe_bool(raw_config.get("capture_assistant_reply", True), True, label="capture_assistant_reply")
    
    _raw_prefixes = raw_config.get("capture_skip_prefixes", "")
    _user_prefixes = [p.strip() for p in str(_raw_prefixes).split(",") if p.strip()] if _raw_prefixes else []
    c.capture_skip_prefixes = ["提醒 #"] + _user_prefixes
    
    _raw_regex = raw_config.get("capture_skip_regex", "")
    if _raw_regex:
        try:
            c.capture_skip_regex = re.compile(_raw_regex)
        except re.error as _e:
            logger.warning("[tmemory] invalid capture_skip_regex: %s", _e)
            
    c.capture_min_content_len = max(0, _safe_int(raw_config.get("capture_min_content_len", 5), 5, label="capture_min_content_len"))
    c.capture_dedup_window = max(0, _safe_int(raw_config.get("capture_dedup_window", 10), 10, label="capture_dedup_window"))

    # ── 蒸馏调度 ──
    c.distill_interval_sec = max(4 * 3600, _safe_int(raw_config.get("distill_interval_sec", 17280), 17280, label="distill_interval_sec"))
    c.distill_min_batch_count = max(8, _safe_int(raw_config.get("distill_min_batch_count", 20), 20, label="distill_min_batch_count"))
    c.distill_batch_limit = max(20, _safe_int(raw_config.get("distill_batch_limit", 80), 80, label="distill_batch_limit"))
    c.distill_pause = _safe_bool(raw_config.get("distill_pause", False), False, label="distill_pause")
    c.distill_user_throttle_sec = max(0, _safe_int(raw_config.get("distill_user_throttle_sec", 0), 0, label="distill_user_throttle_sec"))

    distill_cfg = raw_config.get("distill_model_settings", {})
    c.use_independent_distill_model = _safe_bool(distill_cfg.get("use_independent_distill_model", False), False, label="use_independent_distill_model")
    c.distill_provider_id = str(distill_cfg.get("distill_provider_id", raw_config.get("distill_provider_id", ""))).strip()
    c.distill_model_id = str(distill_cfg.get("distill_model_id", raw_config.get("distill_model_id", ""))).strip()

    # ── 蒸馏降本（Plan TMEAAA-379 B3 / BC-4）──
    c.distill_rule_gating = _safe_bool(raw_config.get("distill_rule_gating", False), False, label="distill_rule_gating")
    c.distill_rule_gate_min_chars = max(1, _safe_int(raw_config.get("distill_rule_gate_min_chars", 40), 40, label="distill_rule_gate_min_chars"))
    c.distill_prompt_cache = _safe_bool(raw_config.get("distill_prompt_cache", True), True, label="distill_prompt_cache")
    c.distill_prompt_cache_max_rows = max(0, _safe_int(raw_config.get("distill_prompt_cache_max_rows", 1000), 1000, label="distill_prompt_cache_max_rows"))

    # ── 提纯 ──
    c.purify_interval_days = max(0, _safe_int(raw_config.get("purify_interval_days", raw_config.get("refine_quality_interval_days", 0)), 0, label="purify_interval_days"))
    c.purify_provider_id = str(distill_cfg.get("purify_provider_id", raw_config.get("purify_provider_id", ""))).strip()
    c.purify_model_id = str(distill_cfg.get("purify_model_id", raw_config.get("purify_model_id", raw_config.get("refine_quality_model_id", "")))).strip()
    c.purify_min_score = max(0.0, min(1.0, _safe_float(raw_config.get("purify_min_score", raw_config.get("refine_quality_min_score", 0.0)), 0.0, label="purify_min_score")))
    
    c.manual_purify_default_mode = str(raw_config.get("manual_purify_default_mode", raw_config.get("manual_refine_default_mode", "both"))).strip().lower()
    if c.manual_purify_default_mode not in {"merge", "split", "both"}:
        c.manual_purify_default_mode = "both"
    c.manual_purify_default_limit = max(1, min(200, _safe_int(raw_config.get("manual_purify_default_limit", raw_config.get("manual_refine_default_limit", 20)), 20, label="manual_purify_default_limit")))

    # ── 向量检索 ──
    vr = raw_config.get("vector_retrieval", {})
    if not isinstance(vr, dict):
        vr = {}
    vr_merged = dict(vr)
    for key in ("enable_vector_search", "embedding_source", "embedding_provider_id", "embedding_provider", "embedding_api_key", "embedding_model", "embedding_base_url", "vector_dim", "auto_rebuild_on_dim_change"):
        if key not in vr_merged and key in raw_config:
            vr_merged[key] = raw_config.get(key)
            
    c.enable_vector_search = _safe_bool(vr_merged.get("enable_vector_search", False), False, label="enable_vector_search")
    c.embedding_source = str(vr_merged.get("embedding_source", "provider") or "provider").strip().lower()
    if c.embedding_source not in {"provider", "standalone", "local"}:
        c.embedding_source = "provider"
    c.embedding_provider_id = str(vr_merged.get("embedding_provider_id", "")).strip()
    c.embed_provider_id = str(vr_merged.get("embedding_provider", "")).strip()
    c.embed_model_id = str(vr_merged.get("embedding_model", "")).strip()
    c.embed_dim = max(64, _safe_int(vr_merged.get("vector_dim", 2048), 2048, label="vector_dim"))
    c.auto_rebuild_on_dim_change = _safe_bool(vr_merged.get("auto_rebuild_on_dim_change", True), True, label="auto_rebuild_on_dim_change")
    c.embed_base_url = str(vr_merged.get("embedding_base_url", "")).strip()
    c.embed_api_key = str(vr_merged.get("embedding_api_key", "")).strip()

    # ── 本地 embedding（Plan TMEAAA-379 B3）──
    c.local_embedding_path = str(
        vr_merged.get("local_embedding_path", raw_config.get("local_embedding_path", "data/bge-small-zh-v1.5"))
        or "data/bge-small-zh-v1.5"
    ).strip()
    c.local_embedding_model_file = str(
        vr_merged.get("local_embedding_model_file", raw_config.get("local_embedding_model_file", "model_quantized.onnx"))
        or "model_quantized.onnx"
    ).strip()
    c.local_embedding_max_length = max(16, _safe_int(vr_merged.get("local_embedding_max_length", 512), 512, label="local_embedding_max_length"))


    # ── Rerank ──
    c.enable_reranker = _safe_bool(raw_config.get("enable_reranker", False), False, label="enable_reranker")
    c.rerank_provider_id = str(raw_config.get("rerank_provider_id", "")).strip()
    c.rerank_model_id = str(raw_config.get("rerank_model_id", raw_config.get("rerank_model", ""))).strip()
    c.rerank_top_n = max(1, _safe_int(raw_config.get("rerank_top_n", 5), 5, label="rerank_top_n"))
    c.rerank_base_url = str(raw_config.get("rerank_base_url", "")).strip()

    # ── Token Budget ──
    c.daily_token_budget = max(0, _safe_int(raw_config.get("daily_token_budget", 0), 0, label="daily_token_budget"))

    # ── 主动工具模式 ──
    c.memory_mode = str(raw_config.get("memory_mode", "hybrid")).strip().lower()
    if c.memory_mode not in {"distill_only", "active_only", "hybrid"}:
        c.memory_mode = "hybrid"

    # ── Profile Storage ──
    ps = raw_config.get("profile_storage", {})
    if not isinstance(ps, dict):
        ps = {}
    c.profile_extraction_enabled = _safe_bool(ps.get("profile_extraction_enabled", False), False, label="profile_extraction_enabled")
    c.profile_extraction_min_messages = max(2, _safe_int(ps.get("profile_extraction_min_messages", 8), 8, label="profile_extraction_min_messages"))
    c.profile_extraction_max_users_per_cycle = max(1, _safe_int(ps.get("profile_extraction_max_users_per_cycle", 10), 10, label="profile_extraction_max_users_per_cycle"))
    c.profile_extraction_timeout_sec = max(30, _safe_int(ps.get("profile_extraction_timeout_sec", 120), 120, label="profile_extraction_timeout_sec"))
    c.profile_stability_default = max(0.0, min(1.0, _safe_float(ps.get("profile_stability_default", 0.5), 0.5, label="profile_stability_default")))
    c.profile_auto_archive_threshold = max(0.0, min(1.0, _safe_float(ps.get("profile_auto_archive_threshold", 0.0), 0.0, label="profile_auto_archive_threshold")))
    c.profile_max_items_per_user = max(10, _safe_int(ps.get("profile_max_items_per_user", 200), 200, label="profile_max_items_per_user"))

    # ── Consolidation Pipeline (deprecated) ──
    cp = raw_config.get("consolidation_pipeline", {})
    if not isinstance(cp, dict):
        cp = {}
    cp_merged = dict(cp)
    for key in ("enable_consolidation_pipeline", "enable_episodic_summarization",
                "enable_episode_semantic_distill", "distill_max_users_per_cycle",
                "stage_timeout_sec", "episode_summary_min_messages",
                "episode_summary_max_input_tokens", "episode_session_gap_minutes"):
        if key not in cp_merged and key in raw_config:
            cp_merged[key] = raw_config.get(key)

    c.enable_consolidation_pipeline = _safe_bool(cp_merged.get("enable_consolidation_pipeline", False), False, label="enable_consolidation_pipeline")
    c.enable_episodic_summarization = _safe_bool(cp_merged.get("enable_episodic_summarization", True), True, label="enable_episodic_summarization")
    c.enable_episode_semantic_distill = _safe_bool(cp_merged.get("enable_episode_semantic_distill", True), True, label="enable_episode_semantic_distill")
    c.distill_max_users_per_cycle = max(1, _safe_int(cp_merged.get("distill_max_users_per_cycle", 10), 10, label="distill_max_users_per_cycle"))
    c.stage_timeout_sec = max(30, _safe_int(cp_merged.get("stage_timeout_sec", 120), 120, label="stage_timeout_sec"))
    c.episode_summary_min_messages = max(2, _safe_int(cp_merged.get("episode_summary_min_messages", 5), 5, label="episode_summary_min_messages"))
    c.episode_summary_max_input_tokens = max(500, _safe_int(cp_merged.get("episode_summary_max_input_tokens", 3000), 3000, label="episode_summary_max_input_tokens"))
    c.episode_session_gap_minutes = max(5, _safe_int(cp_merged.get("episode_session_gap_minutes", 60), 60, label="episode_session_gap_minutes"))

    consolidation_cfg = raw_config.get("consolidation_model_settings", {})
    c.use_independent_consolidation_model = _safe_bool(consolidation_cfg.get("use_independent_consolidation_model", False), False, label="use_independent_consolidation_model")
    c.consolidation_provider_id = str(consolidation_cfg.get("consolidation_provider_id", raw_config.get("consolidation_provider_id", ""))).strip()
    c.consolidation_model_id = str(consolidation_cfg.get("consolidation_model_id", raw_config.get("consolidation_model_id", ""))).strip()

    # ── 注入与隔离 ──
    c.enable_memory_injection = _safe_bool(raw_config.get("enable_memory_injection", True), True, label="enable_memory_injection")
    c.inject_enable_vector_search = _safe_bool(raw_config.get("inject_enable_vector_search", False), False, label="inject_enable_vector_search")
    c.memory_scope = str(raw_config.get("memory_scope", "user")).strip().lower()
    if c.memory_scope not in {"user", "session"}:
        c.memory_scope = "user"
    c.private_memory_in_group = _safe_bool(raw_config.get("private_memory_in_group", False), False, label="private_memory_in_group")
    
    c.inject_position = str(raw_config.get("inject_position", "system_prompt")).strip().lower()
    if c.inject_position not in {"system_prompt", "user_message_before", "user_message_after", "slot", "extra_user_temp"}:
        c.inject_position = "system_prompt"
    c.inject_slot_marker = str(raw_config.get("inject_slot_marker", "{{tmemory}}")).strip()
    c.inject_memory_limit = _safe_int(raw_config.get("inject_memory_limit", 5), 5, label="inject_memory_limit")
    c.inject_max_chars = _safe_int(raw_config.get("inject_max_chars", 0), 0, label="inject_max_chars")
    c.enable_layered_injection = _safe_bool(raw_config.get("enable_layered_injection", False), False, label="enable_layered_injection")
    c.inject_working_turns = max(0, _safe_int(raw_config.get("inject_working_turns", 5), 5, label="inject_working_turns"))
    c.inject_episode_limit = max(0, _safe_int(raw_config.get("inject_episode_limit", 3), 3, label="inject_episode_limit"))
    c.inject_episode_max_chars = max(0, _safe_int(raw_config.get("inject_episode_max_chars", 600), 600, label="inject_episode_max_chars"))
    c.inject_style_max_chars = max(0, _safe_int(raw_config.get("inject_style_max_chars", 400), 400, label="inject_style_max_chars"))

    # ── 会话生命周期 (/new /reset) ──
    c.session_reset_policy = str(raw_config.get("session_reset_policy", "keep")).strip().lower()
    if c.session_reset_policy not in {"keep", "archive", "clear"}:
        logger.warning(
            "[tmemory] config session_reset_policy invalid (%r), using default keep",
            raw_config.get("session_reset_policy"),
        )
        c.session_reset_policy = "keep"

    # ── 主动记忆（Proactive，Plan TMEAAA-379 B2）──
    pr = raw_config.get("proactive", {})
    if not isinstance(pr, dict):
        pr = {}
    pr_merged = dict(pr)
    for key in (
        "proactive_enabled",
        "proactive_interval_sec",
        "proactive_max_candidates_per_cycle",
        "proactive_per_user_window_sec",
        "proactive_per_user_max_per_window",
        "proactive_min_interval_sec",
        "proactive_daily_budget",
        "proactive_reminder_enabled",
        "proactive_recall_enabled",
        "proactive_opt_in_default",
        "proactive_max_message_chars",
    ):
        if key not in pr_merged and key in raw_config:
            pr_merged[key] = raw_config.get(key)

    c.proactive_enabled = _safe_bool(pr_merged.get("proactive_enabled", False), False, label="proactive_enabled")
    c.proactive_interval_sec = max(30, _safe_int(pr_merged.get("proactive_interval_sec", 300), 300, label="proactive_interval_sec"))
    c.proactive_max_candidates_per_cycle = max(1, _safe_int(pr_merged.get("proactive_max_candidates_per_cycle", 5), 5, label="proactive_max_candidates_per_cycle"))
    c.proactive_per_user_window_sec = max(0, _safe_int(pr_merged.get("proactive_per_user_window_sec", 3600), 3600, label="proactive_per_user_window_sec"))
    c.proactive_per_user_max_per_window = max(0, _safe_int(pr_merged.get("proactive_per_user_max_per_window", 3), 3, label="proactive_per_user_max_per_window"))
    c.proactive_min_interval_sec = max(0, _safe_int(pr_merged.get("proactive_min_interval_sec", 300), 300, label="proactive_min_interval_sec"))
    c.proactive_daily_budget = max(0, _safe_int(pr_merged.get("proactive_daily_budget", 20), 20, label="proactive_daily_budget"))
    c.proactive_reminder_enabled = _safe_bool(pr_merged.get("proactive_reminder_enabled", True), True, label="proactive_reminder_enabled")
    c.proactive_recall_enabled = _safe_bool(pr_merged.get("proactive_recall_enabled", True), True, label="proactive_recall_enabled")
    c.proactive_opt_in_default = _safe_bool(pr_merged.get("proactive_opt_in_default", False), False, label="proactive_opt_in_default")
    c.proactive_max_message_chars = max(20, _safe_int(pr_merged.get("proactive_max_message_chars", 200), 200, label="proactive_max_message_chars"))

    return c


def apply_safe_defaults(plugin) -> None:
    """为 plugin 实例应用所有配置与运行时属性的安全默认值。
    
    抽取自 main.TmemoryPlugin._set_safe_defaults，供主类在 __init__ 中调用。
    保持原先语义，不改变任何字段默认值。
    """
    c = plugin._cfg
    c.cache_max_rows = 20
    c.memory_max_chars = 220
    c.enable_auto_capture = True
    c.capture_assistant_reply = True
    c.no_memory_marker = "\x00[astrbot:no-memory]\x00"
    c.capture_skip_prefixes = ["提醒 #"]
    c.capture_skip_regex = None
    c.distill_interval_sec = 17280
    c.distill_min_batch_count = 20
    c.distill_batch_limit = 80
    c.distill_model_id = ""
    c.distill_provider_id = ""
    c.enable_memory_injection = True
    c.inject_enable_vector_search = False
    c.manual_purify_default_mode = "both"
    c.manual_purify_default_limit = 20
    plugin.manual_refine_default_mode = "both"
    plugin.manual_refine_default_limit = 20
    c.distill_pause = False
    c.distill_rule_gating = False
    c.distill_rule_gate_min_chars = 40
    c.distill_prompt_cache = True
    c.distill_prompt_cache_max_rows = 1000
    c.purify_interval_days = 0
    c.purify_model_id = ""
    c.purify_min_score = 0.0
    # ── 向量检索管理器 ──────────────────────────────────────────
    plugin._vector_manager = None
    c.embedding_source = "provider"
    c.embedding_provider_id = ""
    c.embed_provider_id = ""
    c.embed_model_id = ""
    plugin.embed_model = ""
    c.embed_dim = 1536
    c.auto_rebuild_on_dim_change = True
    plugin.vector_weight = 0.4
    plugin.min_vector_sim = 0.15
    plugin._sqlite_vec = None
    plugin._vec_available = False
    c.embed_base_url = ""
    c.embed_api_key = ""
    c.local_embedding_path = "data/bge-small-zh-v1.5"
    c.local_embedding_model_file = "model_quantized.onnx"
    c.local_embedding_max_length = 512
    c.enable_reranker = False
    c.rerank_provider_id = ""
    c.rerank_model_id = ""
    plugin.rerank_model = ""
    c.rerank_top_n = 5
    c.rerank_base_url = ""
    c.memory_scope = "user"
    c.memory_mode = "hybrid"
    c.private_memory_in_group = False
    c.inject_position = "system_prompt"
    c.inject_slot_marker = "{{tmemory}}"
    c.inject_memory_limit = 5
    c.inject_max_chars = 0
    c.enable_layered_injection = False
    c.inject_working_turns = 5
    c.inject_episode_limit = 3
    c.inject_episode_max_chars = 600
    c.inject_style_max_chars = 400
    c.session_reset_policy = "keep"
    # ── 主动记忆（Plan TMEAAA-379 B2 / BC-3）──
    c.proactive_enabled = False
    c.proactive_interval_sec = 300
    c.proactive_max_candidates_per_cycle = 5
    c.proactive_per_user_window_sec = 3600
    c.proactive_per_user_max_per_window = 3
    c.proactive_min_interval_sec = 300
    c.proactive_daily_budget = 20
    c.proactive_reminder_enabled = True
    c.proactive_recall_enabled = True
    c.proactive_opt_in_default = False
    c.proactive_max_message_chars = 200
    plugin._proactive_task = None
    plugin._proactive_engine = None
    c.enable_consolidation_pipeline = False
    c.enable_episodic_summarization = True
    c.enable_episode_semantic_distill = True
    c.profile_extraction_enabled = False
    c.profile_extraction_min_messages = 8
    c.profile_extraction_max_users_per_cycle = 10
    c.profile_extraction_timeout_sec = 120
    c.profile_stability_default = 0.5
    c.profile_auto_archive_threshold = 0.0
    c.profile_max_items_per_user = 200
    c.distill_max_users_per_cycle = 10
    c.stage_timeout_sec = 120
    c.use_independent_consolidation_model = False
    c.consolidation_provider_id = ""
    c.consolidation_model_id = ""
    c.episode_summary_min_messages = 5
    c.episode_summary_max_input_tokens = 3000
    c.episode_session_gap_minutes = 60
    plugin._sanitize_patterns = []
    plugin._distill_task = None
    plugin._worker_running = False
    plugin._merge_needs_vector_rebuild = False
    plugin._fts5_needs_rebuild = False
    plugin._last_purify_ts = 0.0
    plugin._embed_ok_count = 0
    plugin._embed_fail_count = 0
    plugin._embed_provider_fail_count = 0
    plugin._embed_last_source = ""
    plugin._embed_last_error = ""
    plugin._vec_query_count = 0
    plugin._vec_hit_count = 0
    plugin._embed_cache_hit_count = 0
    plugin._embed_cache_miss_count = 0
    plugin._embed_semaphore = None
    plugin._http_session = None
    # ── 触发门控与批处理效率 ──────────────────────────────────────────────
    c.capture_min_content_len = 5
    c.capture_dedup_window = 10
    c.distill_user_throttle_sec = 0
    # 运行时统计：每次蒸馏周期中因门控被跳过的行数
    plugin._distill_skipped_rows = 0
    # ── 蒸馏降本运行时统计（Plan TMEAAA-379 B3）──
    plugin._distill_rule_gated_batches = 0
    plugin._distill_rule_gated_rows = 0
    plugin._distill_rule_deferred_batches = 0
    plugin._distill_prompt_cache_hits = 0
    # 内存缓存：per-user 最近蒸馏完成时间戳（用于节流）
    plugin._user_last_distilled_ts = {}
    # ── 会话生命周期（Phase 4a）─────────────────────────────────────────
    plugin._session_conv_ids = {}
    plugin._agent_begin_count = 0
    plugin._agent_done_count = 0
    # ── 兼容性打点（Plan TMEAAA-354 Phase 1）────────────────────────────
    plugin._extra_user_temp_fallback_count = 0
    plugin._persona_private_fallback_count = 0


# =============================================================================
# Plugin lifecycle mixin
# =============================================================================

import importlib.util


class _NullWebServer:
    """WebUI 降级替身，保证核心功能不受 WebUI 加载失败影响。"""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class PluginLifecycleMixin:
    def _set_safe_defaults(self):
        """设置所有配置属性的安全默认值，确保任何配置解析失败都不会导致 AttributeError。
        
        实现细节见 core.config.apply_safe_defaults。
        """
        from .config import apply_safe_defaults
        apply_safe_defaults(self)

    def _get_vector_retrieval_config(self) -> Dict:
        """兼容旧平铺配置和新嵌套配置的向量检索配置读取。"""
        vector_cfg = self.config.get("vector_retrieval", {})
        if not isinstance(vector_cfg, dict):
            vector_cfg = {}

        merged = dict(vector_cfg)
        legacy_keys = (
            "enable_vector_search",
            "embedding_provider",
            "embedding_api_key",
            "embedding_model",
            "embedding_base_url",
            "vector_dim",
            "auto_rebuild_on_dim_change",
        )
        for key in legacy_keys:
            if key not in merged and key in self.config:
                merged[key] = self.config.get(key)
        return merged

    def _get_vector_retrieval_config_from_cfg(self) -> Dict:
        """从 self._cfg 返回 vector_retrieval 字典用于传递给 VectorManager"""
        return {
            "enable_vector_search": self._cfg.enable_vector_search,
            "embedding_source": self._cfg.embedding_source,
            "embedding_provider_id": self._cfg.embedding_provider_id,
            "embedding_provider": self._cfg.embed_provider_id,
            "embedding_api_key": self._cfg.embed_api_key,
            "embedding_model": self._cfg.embed_model_id,
            "embedding_base_url": self._cfg.embed_base_url,
            "vector_dim": self._cfg.embed_dim,
            "auto_rebuild_on_dim_change": self._cfg.auto_rebuild_on_dim_change,
            "rerank_provider_id": self._cfg.rerank_provider_id,
            "local_embedding_path": self._cfg.local_embedding_path,
            "local_embedding_model_file": self._cfg.local_embedding_model_file,
            "local_embedding_max_length": self._cfg.local_embedding_max_length,
        }

    def _legacy_webui_config(self) -> Dict:
        """合并顶层配置与 webui_settings 嵌套配置。"""
        webui_cfg = dict(self.config)
        webui_sub = self.config.get("webui_settings", {})
        if isinstance(webui_sub, dict):
            webui_cfg.update(webui_sub)
        return webui_cfg

    def _safe_load_web_server(self):
        """安全加载 legacy WebUI 服务器，失败或未启用时降级为 _NullWebServer。

        Plan TMEAAA-354 Phase 3：默认由 Dashboard 托管的 Pages bridge 取代
        legacy aiohttp 面板；``webui_legacy_enabled=true`` 时保留一版回滚。
        """
        try:
            webui_cfg = self._legacy_webui_config()
            if not bool(webui_cfg.get("webui_legacy_enabled", False)):
                logger.info(
                    "[tmemory] legacy WebUI 未启用（webui_legacy_enabled=false），"
                    "使用 Dashboard Plugin Pages bridge。"
                )
                return _NullWebServer()
            TMemoryWebServer = self._load_web_server_class()
            return TMemoryWebServer(self, webui_cfg)
        except Exception as e:
            logger.warning(
                "[tmemory] WebUI 加载失败，核心功能不受影响: %s", e
            )
            return _NullWebServer()

    def _safe_register_pages_bridge(self):
        """注册 Dashboard Plugin Pages bridge（能力不可用时返回 None）。"""
        try:
            from ..adapters import version as _version_adapter

            if not _version_adapter.has_plugin_pages():
                logger.info(
                    "[tmemory] 当前 AstrBot 无 Plugin Pages 能力（需 >=4.28），跳过 bridge 注册。"
                )
                return None

            context = getattr(self, "context", None)
            if context is None or not callable(
                getattr(context, "register_web_api", None)
            ):
                return None

            from ..web.bridge import PluginPagesBridge

            bridge = PluginPagesBridge(self)
            count = bridge.register(context)
            logger.info("[tmemory] Plugin Pages bridge 已注册 %s 条路由", count)
            return bridge
        except Exception as e:
            logger.warning("[tmemory] Plugin Pages bridge 注册失败: %s", e)
            return None

    def _load_web_server_class(self):
        """通过文件路径动态加载 web/legacy_server.py。

        避免 `No module named 'web_server'`，同时自建父包上下文，使
        ``from ..web_handlers import ...`` 等相对 import 在脱离 sys.path 时仍可解析。
        """
        import types

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        web_server_path = os.path.join(root_dir, "web", "legacy_server.py")
        if not os.path.exists(web_server_path):
            raise ImportError(f"web/legacy_server.py not found: {web_server_path}")

        plugin_module = self.__class__.__module__
        module_prefix = plugin_module.rsplit(".", 1)[0] if "." in plugin_module else self.plugin_name
        module_name = f"{module_prefix}.web.legacy_server"

        # 保证父包 `module_prefix` / `module_prefix.web` 可在相对 import 时被解析。
        sys_modules = __import__("sys").modules
        parent_pkg = sys_modules.get(module_prefix)
        if parent_pkg is None:
            parent_pkg = types.ModuleType(module_prefix)
            parent_pkg.__path__ = [root_dir]  # type: ignore[attr-defined]
            sys_modules[module_prefix] = parent_pkg
        web_pkg_name = f"{module_prefix}.web"
        if web_pkg_name not in sys_modules:
            web_pkg = types.ModuleType(web_pkg_name)
            web_pkg.__path__ = [os.path.join(root_dir, "web")]  # type: ignore[attr-defined]
            sys_modules[web_pkg_name] = web_pkg
            setattr(parent_pkg, "web", web_pkg)

        spec = importlib.util.spec_from_file_location(module_name, web_server_path)
        if spec is None or spec.loader is None:
            raise ImportError("failed to create module spec for web/legacy_server.py")

        module = importlib.util.module_from_spec(spec)
        sys_modules[module_name] = module
        spec.loader.exec_module(module)

        cls = getattr(module, "TMemoryWebServer", None)
        if cls is None:
            raise ImportError("TMemoryWebServer not found in web/legacy_server.py")
        return cls

    async def initialize(self):
        self._load_sqlite_vec()
        self._init_db()
        self._migrate_schema()

        # 上游能力探测：集中记录一次，并在 extra_user_temp 不可用时显式告警。
        try:
            from ..adapters import version as _version_adapter

            caps = _version_adapter.log_capabilities()
            if (
                self._cfg.inject_position == "extra_user_temp"
                and not caps.has_mark_as_temp
            ):
                logger.warning(
                    "[tmemory] 当前 inject_position=extra_user_temp，但 %s",
                    _version_adapter.extra_user_temp_hint(),
                )
        except Exception as e:  # 能力探测绝不应阻断启动
            logger.debug("[tmemory] capability probe skipped: %s", e)

        # 初始化 VectorManager(如果向量检索启用)
        if self._cfg.enable_vector_search:
            try:
                from ..vector_manager import VectorManager
                vr = self._get_vector_retrieval_config_from_cfg()
                self._vector_manager = VectorManager(self.db_path, vr)
                # 注入 AstrBot Context（Provider 解析用）；兼容只有两参构造的 VectorManager。
                try:
                    self._vector_manager.context = getattr(self, "context", None)
                except Exception:
                    pass
                await self._vector_manager.initialize()
                logger.info(
                    "[tmemory] VectorManager initialized: %s",
                    self._vector_manager.status()
                    if hasattr(self._vector_manager, "status")
                    else "source=standalone",
                )
            except Exception as e:
                logger.error("[tmemory] Failed to initialize VectorManager: %s", e)
                self._vector_manager = None

        # Provider 维度变化 → 更新维度、失效 query 缓存、重建索引（BC-2 回滚见 auto_rebuild_on_dim_change）
        if self._vector_manager is not None:
            try:
                from . import vector as _vector

                dim_result = await _vector.apply_provider_dim_change(self)
                if dim_result.get("changed"):
                    logger.warning("[tmemory] embedding dim reconciled: %s", dim_result)
            except Exception as e:
                logger.warning("[tmemory] provider dim reconcile failed: %s", e)

        self._worker_running = True
        self._distill_task = asyncio.create_task(self._distill_worker_loop())

        # 主动记忆 worker（Plan TMEAAA-379 B2）：默认关闭时零调度、零副作用。
        try:
            self._proactive_task = await self._start_proactive_worker()
        except Exception as e:
            logger.warning("[tmemory] proactive worker 启动失败，核心功能不受影响: %s", e)
            self._proactive_task = None

        # 启动独立 WebUI 服务器
        try:
            await self._web_server.start()
        except Exception as e:
            logger.warning("[tmemory] WebUI 启动失败，核心功能不受影响: %s", e)
            self._web_server = _NullWebServer()

        # 注册会话删除钩子（Phase 4a；能力缺失自动跳过）
        try:
            self._register_session_lifecycle_hooks()
        except Exception as e:
            logger.warning("[tmemory] 会话删除钩子注册失败，核心功能不受影响: %s", e)

        logger.info(
            "[tmemory] initialized, db=%s, auto_capture=%s, memory_injection=%s, distill_interval=%ss, memory_mode=%s",
            self.db_path,
            self._cfg.enable_auto_capture,
            self._cfg.enable_memory_injection,
            self._cfg.distill_interval_sec,
            self._cfg.memory_mode,
        )

    async def terminate(self):
        self._worker_running = False
        if self._distill_task and not self._distill_task.done():
            self._distill_task.cancel()
            try:
                await self._distill_task
            except asyncio.CancelledError:
                pass
        # 主动记忆 worker（默认未启动，即 None）
        proactive_task = getattr(self, "_proactive_task", None)
        if proactive_task and not proactive_task.done():
            proactive_task.cancel()
            try:
                await proactive_task
            except asyncio.CancelledError:
                pass
        self._proactive_task = None
        # 关闭 VectorManager
        if self._vector_manager:
            try:
                await self._vector_manager.close()
                logger.info("[tmemory] VectorManager closed")
            except Exception as e:
                logger.warning("[tmemory] VectorManager close exception: %s", e)
        # 关闭 WebUI 服务器
        try:
            await self._web_server.stop()
        except Exception as e:
            logger.warning("[tmemory] WebUI 关闭异常: %s", e)
        # 关闭 aiohttp session（向量检索启用时防止连接泄漏）
        if self._http_session and not self._http_session.closed:
            try:
                await self._http_session.close()
            except Exception as e:
                logger.warning("[tmemory] http_session 关闭异常: %s", e)
        # 关闭持久 DB 连接
        self._close_db()
        logger.info("[tmemory] terminated")

