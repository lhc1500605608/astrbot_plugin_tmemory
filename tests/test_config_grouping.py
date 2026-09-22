"""TMEAAA-460/467: 配置分组重排 + Embedding Provider 选择 + 向后兼容迁移。

TMEAAA-467：embedding 收敛为 provider-only——`embedding_source` 与
`standalone_embedding` / `local_embedding` 分组仅保留（invisible，不丢值），
旧值加载时不报错、不丢值。
"""
import json
import logging
from pathlib import Path

import pytest

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
    # TMEAAA-467: hint 指向记忆面板下拉选择 AstrBot Embedding Provider。
    assert "Embedding Provider" in item["hint"]
    assert "下拉" in item["hint"]


def test_embedding_source_invisible_provider_only():
    """TMEAAA-467: embedding_source 收敛为 provider，仅保留 default 且不可见。"""
    item = _schema()["vector_retrieval"]["items"]["embedding_source"]
    assert item.get("invisible") is True
    assert item["default"] == "provider"
    assert "options" not in item
    assert "hint" not in item


def test_standalone_and_local_groups_hidden_but_items_kept():
    """TMEAAA-467: standalone/local 分组 invisible，但 items 必须保留（不丢值）。"""
    vr = _schema()["vector_retrieval"]["items"]
    standalone = vr["standalone_embedding"]
    assert standalone.get("invisible") is True
    assert standalone["type"] == "object"
    for key in ("embedding_provider", "embedding_api_key", "embedding_model", "embedding_base_url"):
        assert key in standalone["items"]
    local = vr["local_embedding"]
    assert local.get("invisible") is True
    for key in ("local_embedding_path", "local_embedding_model_file", "local_embedding_max_length"):
        assert key in local["items"]


@pytest.mark.parametrize("legacy_source", ["standalone", "local"])
def test_legacy_embedding_source_reads_as_provider_and_preserves_values(
    plugin_module, caplog, legacy_source
):
    """旧 embedding_source=standalone/local：归一化为 provider，告警且不丢值。"""
    from astrbot_plugin_tmemory.core.config import parse_config

    raw = {
        "enable_vector_search": True,
        "vector_retrieval": {
            "embedding_source": legacy_source,
            "standalone_embedding": {
                "embedding_api_key": "legacy-key",
                "embedding_model": "legacy-model",
            },
            "local_embedding": {"local_embedding_path": "data/custom-bge"},
        },
    }
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        cfg = parse_config(raw)

    assert cfg.embedding_source == "provider"
    # 旧值仍完整保留在 raw config（不丢值、不报错）
    assert raw["vector_retrieval"]["embedding_source"] == legacy_source
    assert raw["vector_retrieval"]["standalone_embedding"]["embedding_api_key"] == "legacy-key"
    assert raw["vector_retrieval"]["local_embedding"]["local_embedding_path"] == "data/custom-bge"
    assert cfg.embed_api_key == "legacy-key"
    assert cfg.embed_model_id == "legacy-model"
    assert cfg.local_embedding_path == "data/custom-bge"
    assert any(
        "embedding_source" in record.getMessage() and legacy_source in record.getMessage()
        for record in caplog.records
    )


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


def test_distill_fallback_to_rules_defaults_false_and_reads_grouped(plugin_module):
    """TMEAAA-513: 蒸馏失败回退规则记忆默认关闭，可从 distill 分组读取。"""
    from astrbot_plugin_tmemory.core.config import parse_config

    assert parse_config({}).distill_fallback_to_rules is False
    assert parse_config({"distill": {"distill_fallback_to_rules": True}}).distill_fallback_to_rules is True


def test_distill_fallback_to_rules_schema_visible_in_distill_group():
    schema = _schema()
    item = schema["distill"]["items"]["distill_fallback_to_rules"]
    assert item["type"] == "bool"
    assert item["default"] is False
    assert item["description"] == "蒸馏失败时回退规则记忆"
    assert "默认关闭" in item["hint"]
    # 旧平铺键保留（invisible）以免完整性检查删值
    assert schema["distill_fallback_to_rules"].get("invisible") is True
