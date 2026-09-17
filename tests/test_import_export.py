"""B6 数据导出 / 导入测试（Plan TMEAAA-379 T4）。

覆盖验收：
- 导出信封与 round-trip 幂等
- dry-run 只预览不落库
- 字段校验：非法记录不写入
- 冲突策略：skip 幂等 / error 中止，overwrite 被拒
- 失败自动回滚 + 保留备份
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


class FakeEvent:
    def __init__(self, message_str: str):
        self.message_str = message_str

    def plain_result(self, text: str):
        return {"type": "plain", "text": text}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def portability_mod(plugin):
    from astrbot_plugin_tmemory.core import portability

    return portability


def _count(plugin, user: str) -> int:
    with plugin._db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE canonical_user_id=? AND is_active=1",
            (user,),
        ).fetchone()
    return int(row["n"])


def _payload(user: str = "u-import", n: int = 3):
    return {
        "schema_version": 1,
        "generator": "astrbot_plugin_tmemory",
        "users": [
            {
                "canonical_user_id": user,
                "memories": [
                    {
                        "canonical_user_id": user,
                        "memory": f"导入事实 {i}",
                        "memory_type": "fact",
                        "score": 0.6,
                        "importance": 0.6,
                        "confidence": 0.7,
                    }
                    for i in range(n)
                ],
            }
        ],
    }


# ── dry-run ────────────────────────────────────────────────────────────────


def test_dry_run_previews_without_writing(plugin, admin_svc):
    result = admin_svc.import_dataset(_payload("u-dry", 3), dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["to_insert"] == 3
    assert result["inserted"] == 0
    assert len(result["preview"]["new"]) == 3
    assert _count(plugin, "u-dry") == 0


# ── 幂等 / 冲突 ─────────────────────────────────────────────────────────────


def test_apply_is_idempotent(plugin, admin_svc):
    first = admin_svc.import_dataset(_payload("u-idem", 3), dry_run=False)
    assert first["ok"] is True and first["inserted"] == 3
    assert _count(plugin, "u-idem") == 3

    second = admin_svc.import_dataset(_payload("u-idem", 3), dry_run=False)
    assert second["ok"] is True
    assert second["inserted"] == 0
    assert second["conflicts"] == 3
    assert _count(plugin, "u-idem") == 3


def test_conflict_policy_error_aborts_without_writes(plugin, admin_svc):
    admin_svc.import_dataset(_payload("u-strict", 1), dry_run=False)
    mixed = _payload("u-strict", 2)
    mixed["users"][0]["memories"].append(
        {
            "canonical_user_id": "u-strict-other",
            "memory": "全新事实",
        }
    )

    result = admin_svc.import_dataset(
        mixed, dry_run=False, on_conflict="error"
    )

    assert result["ok"] is False
    assert result["inserted"] == 0
    assert _count(plugin, "u-strict") == 1
    assert _count(plugin, "u-strict-other") == 0


def test_overwrite_policy_rejected(plugin, admin_svc):
    with pytest.raises(ValueError):
        admin_svc.import_dataset(_payload(), dry_run=False, on_conflict="overwrite")


def test_unsupported_schema_version_rejected(plugin, admin_svc):
    payload = _payload("u-schema", 1)
    payload["schema_version"] = 99
    result = admin_svc.import_dataset(payload, dry_run=True)

    assert result["valid"] == 0
    assert result["error_count"] == 1
    assert "schema_version" in result["errors"][0]["reason"]


# ── 字段校验 ───────────────────────────────────────────────────────────────


def test_field_validation_skips_invalid_records(plugin, admin_svc):
    payload = {
        "users": [
            {
                "canonical_user_id": "u-valid",
                "memories": [
                    {"memory": "合法记忆"},
                    {"memory": ""},  # missing memory
                    {"memory": "坏类型", "memory_type": "gossip"},
                    {"memory": "坏分数", "score": 1.5},
                    {"memory": "坏 scope", "scope": "root"},
                    "not-a-dict",
                ],
            }
        ]
    }
    result = admin_svc.import_dataset(payload, dry_run=False)

    assert result["inserted"] == 1
    assert result["error_count"] == 5
    assert _count(plugin, "u-valid") == 1


def test_export_envelope_roundtrip_is_idempotent(plugin, admin_svc):
    admin_svc.add_memory(user="u-rt", memory="喜欢喝乌龙茶", score=0.7)
    admin_svc.add_memory(user="u-rt", memory="常住上海", memory_type="fact")

    envelope = admin_svc.export_dataset(["u-rt"])
    assert envelope["schema_version"] == 1
    assert envelope["users"][0]["canonical_user_id"] == "u-rt"
    assert len(envelope["users"][0]["memories"]) == 2

    # 回灌同一份导出：全部命中冲突，不新增行
    result = admin_svc.import_dataset(envelope, dry_run=False)
    assert result["inserted"] == 0
    assert result["conflicts"] == 2
    assert _count(plugin, "u-rt") == 2


# ── 备份 / 回滚 ────────────────────────────────────────────────────────────


def test_backup_written_and_contains_prior_rows(plugin, admin_svc):
    admin_svc.add_memory(user="u-bak", memory="已有记忆")
    result = admin_svc.import_dataset(_payload("u-bak", 1), dry_run=False)

    assert result["backup_path"]
    backup = Path(result["backup_path"])
    assert backup.exists()
    snapshot = json.loads(backup.read_text(encoding="utf-8"))
    assert snapshot["kind"] == "tmemory_import_backup"
    texts = [m["memory"] for m in snapshot["users"][0]["memories"]]
    assert "已有记忆" in texts


def test_failure_rolls_back_and_keeps_backup(plugin, admin_svc, monkeypatch, portability_mod):
    payload = _payload("u-rb", 3)
    calls = {"n": 0}
    original = portability_mod._insert_record

    def flaky(conn, record, now):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return original(conn, record, now)

    monkeypatch.setattr(portability_mod, "_insert_record", flaky)

    result = admin_svc.import_dataset(payload, dry_run=False)

    assert result["ok"] is False
    assert result["rolled_back"] is True
    assert result["inserted"] == 0
    assert _count(plugin, "u-rb") == 0
    assert result["backup_path"] and Path(result["backup_path"]).exists()


# ── bridge 契约 ────────────────────────────────────────────────────────────


def test_bridge_export_import_contract(bridge_module, plugin):
    class FakeQuery:
        def get(self, key, default=None, *a, **k):
            return default

    class FakeRequest:
        def __init__(self, body):
            self._body = body
            self.query = FakeQuery()

        async def json(self, default=None):
            return self._body

    bridge = bridge_module.PluginPagesBridge(plugin)

    payload, status = run(bridge.import_data(FakeRequest({"payload": _payload("u-bridge", 2)})))
    assert status == 200 and payload["dry_run"] is True and payload["to_insert"] == 2

    applied, status = run(
        bridge.import_data(
            FakeRequest({"payload": _payload("u-bridge", 2), "dry_run": False})
        )
    )
    assert status == 200 and applied["inserted"] == 2

    exported, status = run(bridge.export_data(FakeRequest({"users": ["u-bridge"]})))
    assert status == 200
    assert exported["users"][0]["canonical_user_id"] == "u-bridge"

    missing, status = run(bridge.import_data(FakeRequest({})))
    assert status == 400 and "payload" in missing["error"]


# ── /tm_import 命令 ────────────────────────────────────────────────────────


def test_tm_import_command_dry_run_then_apply(plugin):
    event = FakeEvent("/tm_import " + json.dumps(_payload("u-cmd", 2)))

    preview = list(run_iter(plugin._handle_tm_import(event)))
    assert "dry-run" in preview[0]["text"]
    assert _count(plugin, "u-cmd") == 0

    apply_event = FakeEvent("/tm_import apply " + json.dumps(_payload("u-cmd", 2)))
    applied = list(run_iter(plugin._handle_tm_import(apply_event)))
    assert "导入完成" in applied[0]["text"]
    assert _count(plugin, "u-cmd") == 2


def run_iter(agen):
    async def _collect():
        return [item async for item in agen]

    return asyncio.run(_collect())
