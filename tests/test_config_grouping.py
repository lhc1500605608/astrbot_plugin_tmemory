"""TMEAAA-460: 配置分组重排 + Embedding Provider 选择 + 向后兼容迁移。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _schema():
    return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def test_schema_top_level_sections_ordered():
    """配置页分组顺序：基础 / 向量检索 / 蒸馏 / 注入 / 会话与身份 / WebUI。"""
    schema = _schema()
    expected = [
        "basic",
        "vector_retrieval",
        "distill",
        "distill_model_settings",
        "injection",
        "session_identity",
        "profile_storage",
        "proactive",
        "webui_settings",
    ]
    assert list(schema.keys())[: len(expected)] == expected


def test_embedding_provider_id_is_plain_text_id_input():
    """TMEAAA-463: AstrBot 无 Embedding Provider 下拉，改为手填 Provider ID。"""
    item = _schema()["vector_retrieval"]["items"]["embedding_provider_id"]
    assert item["type"] == "string"
    # select_provider 下拉被前端硬编码为 chat_completion，会误导用户选错 Provider
    assert "_special" not in item
    assert "Embedding" in item["description"]
    assert "ID" in item["hint"] and "Embedding" in item["hint"]


def test_standalone_embedding_grouped_and_documented():
    """standalone 相关项归入「独立 / 高级模式」分组并加说明。"""
    vr = _schema()["vector_retrieval"]["items"]
    group = vr["standalone_embedding"]
    assert group["type"] == "object"
    assert "高级" in group["description"] or "独立" in group["description"]
    for key in ("embedding_provider", "embedding_api_key", "embedding_model", "embedding_base_url"):
        assert key in group["items"]
    local = vr["local_embedding"]
    for key in ("local_embedding_path", "local_embedding_model_file", "local_embedding_max_length"):
        assert key in local["items"]


def test_deprecated_items_are_invisible():
    """弃用项保留键（不丢值）但不再在配置页展示。"""
    schema = _schema()
    for key in (
        "enable_layered_injection",
        "inject_working_turns",
        "inject_episode_limit",
        "inject_episode_max_chars",
        "inject_style_max_chars",
    ):
        assert schema[key].get("invisible") is True, key
    assert schema["consolidation_pipeline"].get("invisible") is True
    assert schema["consolidation_model_settings"].get("invisible") is True


def test_moved_keys_kept_invisible_at_legacy_path():
    """被移动的旧平铺键必须保留（invisible）以免 AstrBot 完整性检查删除值。"""
    schema = _schema()
    for key in (
        "enable_auto_capture",
        "memory_mode",
        "session_reset_policy",
        "inject_memory_limit",
        "distill_pause",
        "daily_token_budget",
        "cache_max_rows",
    ):
        assert schema[key].get("invisible") is True, key
    vr_items = schema["vector_retrieval"]["items"]
    for key in ("embedding_api_key", "embedding_provider", "local_embedding_path"):
        assert vr_items[key].get("invisible") is True, key


def test_no_visible_deprecated_labels():
    """配置页不再残留任何 [已废弃] 可见项。"""

    def visible_deprecated(items):
        found = []
        for key, value in items.items():
            if value.get("type") == "object" and not value.get("invisible"):
                found.extend(visible_deprecated(value.get("items", {})))
            if not value.get("invisible") and "[已废弃]" in value.get("description", ""):
                found.append(key)
        return found

    assert visible_deprecated(_schema()) == []


def test_migration_preserves_values_and_reads_grouped(plugin_module):
    from astrbot_plugin_tmemory.core.config import parse_config

    raw = {
        "enable_auto_capture": False,
        "inject_memory_limit": 9,
        "session_reset_policy": "archive",
        "distill_pause": True,
        "vector_retrieval": {
            "embedding_api_key": "k1",
            "embedding_model": "m1",
            "local_embedding_max_length": 256,
        },
        "enable_layered_injection": True,
        "consolidation_pipeline": {"enable_consolidation_pipeline": True},
    }
    cfg = parse_config(raw)

    # 行为/值不变
    assert cfg.enable_auto_capture is False
    assert cfg.inject_memory_limit == 9
    assert cfg.session_reset_policy == "archive"
    assert cfg.distill_pause is True
    assert cfg.embed_api_key == "k1"
    assert cfg.embed_model_id == "m1"
    assert cfg.local_embedding_max_length == 256
    assert cfg.enable_layered_injection is True
    assert cfg.enable_consolidation_pipeline is True

    # 原地迁移到新分组路径并保留旧值
    assert raw["basic"]["enable_auto_capture"] is False
    assert raw["injection"]["inject_memory_limit"] == 9
    assert raw["session_identity"]["session_reset_policy"] == "archive"
    assert raw["distill"]["distill_pause"] is True
    assert raw["vector_retrieval"]["standalone_embedding"]["embedding_api_key"] == "k1"
    assert raw["vector_retrieval"]["local_embedding"]["local_embedding_max_length"] == 256
    # 旧路径重置为默认，之后以新路径为准
    assert raw["enable_auto_capture"] is True
    assert raw["inject_memory_limit"] == 5


def test_migration_prefers_customized_new_value(plugin_module):
    from astrbot_plugin_tmemory.core.config import parse_config

    raw = {"basic": {"cache_max_rows": 50}, "cache_max_rows": 7}
    cfg = parse_config(raw)
    assert cfg.cache_max_rows == 50
    assert raw["cache_max_rows"] == 20


def test_grouped_config_reads_new_format(plugin_module):
    from astrbot_plugin_tmemory.core.config import parse_config

    cfg = parse_config(
        {
            "basic": {"cache_max_rows": 33},
            "injection": {"inject_position": "slot"},
            "session_identity": {"memory_scope": "session"},
            "distill": {"daily_token_budget": 123},
        }
    )
    assert cfg.cache_max_rows == 33
    assert cfg.inject_position == "slot"
    assert cfg.memory_scope == "session"
    assert cfg.daily_token_budget == 123
