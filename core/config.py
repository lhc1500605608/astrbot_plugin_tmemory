import re
import logging
from typing import List, Optional
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
    embed_dim: int = 1024
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
    c.embed_dim = max(64, _safe_int(vr_merged.get("vector_dim", 1024), 1024, label="vector_dim"))
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

# =============================================================================
# Facade re-exports (ADR-009 module boundary split)
# =============================================================================
# PluginLifecycleMixin / apply_safe_defaults now live in core.lifecycle.
# Re-exported here so existing ``from .config import ...`` call sites keep working.
from .lifecycle import PluginLifecycleMixin, apply_safe_defaults

__all__ = ["PluginConfig", "PluginLifecycleMixin", "apply_safe_defaults", "parse_config"]
