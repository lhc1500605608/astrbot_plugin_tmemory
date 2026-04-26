import re
import asyncio
import logging
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
    
    # Purify / Refine
    purify_interval_days: int = 0
    purify_provider_id: str = ""
    purify_model_id: str = ""
    purify_min_score: float = 0.0
    manual_purify_default_mode: str = "both"
    manual_purify_default_limit: int = 20
    
    # Vector
    enable_vector_search: bool = False
    embed_provider_id: str = ""
    embed_model_id: str = ""
    embed_dim: int = 1536
    vector_weight: float = 0.4
    min_vector_sim: float = 0.15
    embed_base_url: str = ""
    embed_api_key: str = ""
    
    # Rerank
    enable_reranker: bool = False
    rerank_provider_id: str = ""
    rerank_model_id: str = ""
    rerank_top_n: int = 5
    rerank_base_url: str = ""
    
    # Style Distill
    enable_style_distill: bool = True
    style_min_confidence: float = 0.55
    style_min_importance: float = 0.4

    # Active Tool Mode
    memory_mode: str = "hybrid"  # distill_only | active_only | hybrid

    # Injection & Scope
    enable_memory_injection: bool = True
    memory_scope: str = "user"
    private_memory_in_group: bool = False
    inject_position: str = "system_prompt"
    inject_slot_marker: str = "{{tmemory}}"
    inject_memory_limit: int = 5
    inject_max_chars: int = 0


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

    # ── 聊天风格蒸馏 ──
    style_cfg = raw_config.get("style_distill_settings", {})
    if not isinstance(style_cfg, dict):
        style_cfg = {}
    c.enable_style_distill = _safe_bool(style_cfg.get("enable_style_distill", True), True, label="enable_style_distill")
    c.style_min_confidence = max(0.0, min(1.0, _safe_float(style_cfg.get("style_min_confidence", 0.55), 0.55, label="style_min_confidence")))
    c.style_min_importance = max(0.0, min(1.0, _safe_float(style_cfg.get("style_min_importance", 0.4), 0.4, label="style_min_importance")))

    distill_cfg = raw_config.get("distill_model_settings", {})
    c.use_independent_distill_model = _safe_bool(distill_cfg.get("use_independent_distill_model", False), False, label="use_independent_distill_model")
    c.distill_provider_id = str(distill_cfg.get("distill_provider_id", raw_config.get("distill_provider_id", ""))).strip()
    c.distill_model_id = str(distill_cfg.get("distill_model_id", raw_config.get("distill_model_id", ""))).strip()

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
    for key in ("enable_vector_search", "embedding_provider", "embedding_api_key", "embedding_model", "embedding_base_url", "vector_dim"):
        if key not in vr_merged and key in raw_config:
            vr_merged[key] = raw_config.get(key)
            
    c.enable_vector_search = _safe_bool(vr_merged.get("enable_vector_search", False), False, label="enable_vector_search")
    c.embed_provider_id = str(vr_merged.get("embedding_provider", "")).strip()
    c.embed_model_id = str(vr_merged.get("embedding_model", "")).strip()
    c.embed_dim = max(64, _safe_int(vr_merged.get("vector_dim", 2048), 2048, label="vector_dim"))
    c.embed_base_url = str(vr_merged.get("embedding_base_url", "")).strip()
    c.embed_api_key = str(vr_merged.get("embedding_api_key", "")).strip()

    # ── Rerank ──
    c.enable_reranker = _safe_bool(raw_config.get("enable_reranker", False), False, label="enable_reranker")
    c.rerank_provider_id = str(raw_config.get("rerank_provider_id", "")).strip()
    c.rerank_model_id = str(raw_config.get("rerank_model_id", raw_config.get("rerank_model", ""))).strip()
    c.rerank_top_n = max(1, _safe_int(raw_config.get("rerank_top_n", 5), 5, label="rerank_top_n"))
    c.rerank_base_url = str(raw_config.get("rerank_base_url", "")).strip()

    # ── 主动工具模式 ──
    c.memory_mode = str(raw_config.get("memory_mode", "hybrid")).strip().lower()
    if c.memory_mode not in {"distill_only", "active_only", "hybrid"}:
        c.memory_mode = "hybrid"

    # ── 注入与隔离 ──
    c.enable_memory_injection = _safe_bool(raw_config.get("enable_memory_injection", True), True, label="enable_memory_injection")
    c.memory_scope = str(raw_config.get("memory_scope", "user")).strip().lower()
    if c.memory_scope not in {"user", "session"}:
        c.memory_scope = "user"
    c.private_memory_in_group = _safe_bool(raw_config.get("private_memory_in_group", False), False, label="private_memory_in_group")
    
    c.inject_position = str(raw_config.get("inject_position", "system_prompt")).strip().lower()
    if c.inject_position not in {"system_prompt", "user_message_before", "user_message_after", "slot"}:
        c.inject_position = "system_prompt"
    c.inject_slot_marker = str(raw_config.get("inject_slot_marker", "{{tmemory}}")).strip()
    c.inject_memory_limit = _safe_int(raw_config.get("inject_memory_limit", 5), 5, label="inject_memory_limit")
    c.inject_max_chars = _safe_int(raw_config.get("inject_max_chars", 0), 0, label="inject_max_chars")
    
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
    c.manual_purify_default_mode = "both"
    c.manual_purify_default_limit = 20
    plugin.manual_refine_default_mode = "both"
    plugin.manual_refine_default_limit = 20
    c.distill_pause = False
    c.purify_interval_days = 0
    c.enable_style_distill = True
    c.style_min_confidence = 0.55
    c.style_min_importance = 0.4
    c.purify_model_id = ""
    c.purify_min_score = 0.0
    # ── 向量检索管理器 ──────────────────────────────────────────
    plugin._vector_manager = None
    c.embed_provider_id = ""
    c.embed_model_id = ""
    plugin.embed_model = ""
    c.embed_dim = 1536
    plugin.vector_weight = 0.4
    plugin.min_vector_sim = 0.15
    plugin._sqlite_vec = None
    plugin._vec_available = False
    c.embed_base_url = ""
    c.embed_api_key = ""
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
    plugin._sanitize_patterns = []
    plugin._distill_task = None
    plugin._worker_running = False
    plugin._merge_needs_vector_rebuild = False
    plugin._fts5_needs_rebuild = False
    plugin._last_purify_ts = 0.0
    plugin._embed_ok_count = 0
    plugin._embed_fail_count = 0
    plugin._embed_last_error = ""
    plugin._vec_query_count = 0
    plugin._vec_hit_count = 0
    plugin._embed_semaphore = asyncio.Semaphore(4)
    plugin._http_session = None
    # ── 触发门控与批处理效率 ──────────────────────────────────────────────
    c.capture_min_content_len = 5
    c.capture_dedup_window = 10
    c.distill_user_throttle_sec = 0
    # 运行时统计：每次蒸馏周期中因门控被跳过的行数
    plugin._distill_skipped_rows = 0
    # 内存缓存：per-user 最近蒸馏完成时间戳（用于节流）
    plugin._user_last_distilled_ts = {}
