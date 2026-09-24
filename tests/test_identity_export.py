"""TMEAAA-569: 导出共享身份映射 identity_map.json（写者）。

覆盖（plan TMEAAA-568 §2.2/§5）：
- 绑定 / 合并 / 重绑 / 解绑后文件内容与 ``index`` 正确（canonical 迁移跟随）。
- ``display_name`` 来自 user_profiles；缺失可为空。
- 群聊键不入 index。
- 目录缺失自动创建；JSON 损坏可覆盖；原子写无残留临时文件。
- AstrBot 数据目录不可用 / DB 异常时 fail-closed 不抛。
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest


def _ie():
    from astrbot_plugin_tmemory.core import identity_export

    return identity_export


def _map_path(data_root) -> Path:
    return Path(data_root) / "plugin_data" / "_shared" / "identity_map.json"


def _read(data_root) -> dict:
    return json.loads(_map_path(data_root).read_text(encoding="utf-8"))


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    """把导出路径固定到 tmp 目录（等价 AstrBot data path 可用）。"""
    ie = _ie()
    monkeypatch.setattr(ie, "get_astrbot_data_path", lambda: str(tmp_path))
    return tmp_path


def _seed_display_name(plugin, canonical, name):
    from astrbot_plugin_tmemory.core.memory_ops import ProfileItemOps

    ProfileItemOps(plugin).upsert_profile_item(canonical, "fact", "t", "c", 0.8, 0.8)
    with plugin._db() as conn:
        conn.execute(
            "UPDATE user_profiles SET display_name=? WHERE canonical_user_id=?",
            (name, canonical),
        )


# ── 触发点：绑定 / 合并 / 重绑 / 解绑 ─────────────────────────────────────────


def test_bind_writes_file_and_index(admin_svc, plugin, data_root):
    _seed_display_name(plugin, "person-zhang", "张三")
    admin_svc.bind_identity("qq", "42", "person-zhang")

    payload = _read(data_root)
    assert payload["version"] == 1
    assert payload["authority"] == "tmemory"
    assert payload["index"]["qq:42"] == "person-zhang"
    person = payload["persons"]["person-zhang"]
    assert person["display_name"] == "张三"
    assert person["bindings"] == [{"adapter": "qq", "adapter_user_id": "42"}]


def test_merge_follows_canonical(admin_svc, data_root):
    admin_svc.bind_identity("qq", "42", "person-a")
    admin_svc.merge_users("person-a", "person-b")

    payload = _read(data_root)
    assert payload["index"]["qq:42"] == "person-b"
    assert "person-a" not in payload["persons"]


def test_rebind_updates_index(admin_svc, plugin, data_root):
    bound = admin_svc.bind_identity("qq", "42", "person-a")
    admin_svc.rebind_user(bound["binding_id"], "person-c")

    assert _read(data_root)["index"]["qq:42"] == "person-c"


def test_unbind_reverts_index(admin_svc, data_root):
    bound = admin_svc.bind_identity("qq", "42", "person-a")
    admin_svc.unbind_identity(bound["binding_id"])

    assert _read(data_root)["index"]["qq:42"] == "qq:42"


def test_index_matches_db(admin_svc, plugin, data_root):
    admin_svc.bind_identity("qq", "42", "person-a")
    admin_svc.bind_identity("wx", "7", "person-a")
    admin_svc.bind_identity("tg", "9", "")

    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT adapter, adapter_user_id, canonical_user_id FROM identity_bindings"
        ).fetchall()
    expected = {
        f"{r['adapter']}:{r['adapter_user_id']}": r["canonical_user_id"] for r in rows
    }
    assert _read(data_root)["index"] == expected


def test_group_binding_excluded_from_index(admin_svc, plugin, data_root):
    admin_svc.bind_identity("qq", "group:1000", "person-z")

    payload = _read(data_root)
    assert "qq:group:1000" not in payload["index"]
    assert payload["index"] == {}


# ── 隐式自动绑定（IdentityManager.resolve_current_identity）──────────────────


def test_implicit_bind_exports(plugin, data_root):
    from astrbot_plugin_tmemory.core.identity import IdentityManager

    mgr = IdentityManager(plugin._db_mgr, plugin._cfg, plugin._memory_logger)

    class _Event:
        def get_sender_id(self):
            return "42"

    mgr.get_adapter_name = lambda _event: "qq"  # type: ignore[assignment]
    canonical, adapter, user = mgr.resolve_current_identity(_Event())

    assert (canonical, adapter, user) == ("qq:42", "qq", "42")
    assert _read(data_root)["index"]["qq:42"] == "qq:42"


def test_manager_bind_and_merge_export(plugin, data_root):
    """命令路径（IdentityManager.bind_identity / merge_identity）也触发导出。"""
    mgr = plugin._identity_mgr

    mgr.bind_identity("qq", "42", "person-a")
    assert _read(data_root)["index"]["qq:42"] == "person-a"

    mgr.merge_identity("person-a", "person-b")
    assert _read(data_root)["index"]["qq:42"] == "person-b"


# ── 边界：目录缺失 / JSON 损坏 / 数据目录不可用 / DB 异常 ─────────────────────


def test_missing_directory_is_created(admin_svc, data_root):
    assert not _map_path(data_root).exists()
    admin_svc.bind_identity("qq", "42", "person-a")
    assert _map_path(data_root).exists()


def test_corrupt_file_is_overwritten(admin_svc, data_root):
    path = _map_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    admin_svc.bind_identity("qq", "42", "person-a")
    assert _read(data_root)["index"] == {"qq:42": "person-a"}


def test_no_tmp_file_leftover(admin_svc, data_root):
    admin_svc.bind_identity("qq", "42", "person-a")
    leftovers = glob.glob(str(_map_path(data_root).parent / ".identity_map.*.tmp"))
    assert leftovers == []


def test_data_root_unavailable_is_skipped(plugin, monkeypatch):
    ie = _ie()
    monkeypatch.setattr(ie, "get_astrbot_data_path", lambda: None)
    assert ie.export_identity_map(plugin._db_mgr) is None


def test_db_error_is_fail_closed(tmp_path):
    ie = _ie()

    class _Boom:
        def db(self):
            raise RuntimeError("boom")

    assert ie.export_identity_map(_Boom(), data_root=str(tmp_path)) is None


def test_build_identity_map_pure(data_root):
    ie = _ie()
    bindings = [
        {
            "adapter": "qq",
            "adapter_user_id": "42",
            "canonical_user_id": "person-a",
            "updated_at": "2026-09-24 10:00:00",
        },
        {
            "adapter": "qq",
            "adapter_user_id": "group:1000",
            "canonical_user_id": "person-b",
            "updated_at": "2026-09-24 11:00:00",
        },
    ]
    profiles = [
        {"canonical_user_id": "person-a", "display_name": "张三", "updated_at": "x"},
        {"canonical_user_id": "lonely", "display_name": "", "updated_at": "y"},
    ]
    payload = ie.build_identity_map(bindings, profiles, updated_at="2026-09-24 12:00:00")
    assert payload["index"] == {"qq:42": "person-a"}
    assert payload["persons"]["person-a"]["display_name"] == "张三"
    assert payload["persons"]["lonely"]["bindings"] == []
    assert payload["updated_at"] == "2026-09-24 12:00:00"
