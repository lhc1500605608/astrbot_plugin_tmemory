"""数据导出 / 导入（Plan TMEAAA-379 B6 / BC-5）。

设计目标（对齐验收）：

- **导出**：把用户记忆与身份绑定序列化为版本化 JSON 信封，可作为
  导入输入与失败备份，实现往返一致（round-trip）。
- **导入 dry-run**：默认只规划不落库，返回 ``to_insert`` / ``conflicts`` /
  ``invalid`` 预览。
- **字段校验**：逐条校验必填字段、枚举与数值范围，非法记录永不写入。
- **幂等**：以 ``(canonical_user_id, memory_hash, persona_id, scope)`` 为
  去重键；重复导入同一份数据在第二次全部命中 ``conflicts``，不产生新行。
- **冲突策略**：``skip``（默认，只增不覆盖）或 ``error``（命中即中止，
  不写任何数据）；显式拒绝 ``overwrite``，保证「导入只增不覆盖」。
- **备份 + 事务**：真正落库前先对受影响用户写 JSON 备份；写入包裹在单个
  SQLite 事务中，任何异常自动回滚，备份文件保留用于恢复。

约束：复用既有数据访问层（``plugin._db()`` / ``_now`` / 现有表结构），
不新增表、不改表结构。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .utils_shared import (
    _VALID_MEMORY_TYPES,
    _clamp01,
    _normalize_text,
)

logger = logging.getLogger("astrbot")

SCHEMA_VERSION = 1
GENERATOR = "astrbot_plugin_tmemory"
DEFAULT_SOURCE_ADAPTER = "import"
DEFAULT_SOURCE_USER = "import"

_ALLOWED_SCOPES = frozenset({"user", "session", "private"})
_ALLOWED_POLICIES = frozenset({"skip", "error"})
_MAX_MEMORY_LEN = 4000
_MAX_ID_LEN = 256
_PREVIEW_LIMIT = 10


# ── 通用工具 ────────────────────────────────────────────────────────────────


def _memory_hash(persona_id: str, scope: str, memory: str) -> str:
    normalized = _normalize_text(memory)
    payload = f"{persona_id}:{scope}:{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dedup_key(record: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(record["canonical_user_id"]),
        _memory_hash(
            str(record.get("persona_id", "")),
            str(record.get("scope", "user")),
            str(record["memory"]),
        ),
        str(record.get("persona_id", "")),
        str(record.get("scope", "user")),
    )


def _unit_float(value: Any, field: str, default: float) -> Tuple[Optional[float], Optional[str]]:
    if value is None:
        return default, None
    if isinstance(value, bool):
        return None, f"{field} must be a number in [0, 1]"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None, f"{field} must be a number in [0, 1]"
    if num != num or num in (float("inf"), float("-inf")):
        return None, f"{field} must be a number in [0, 1]"
    if not 0.0 <= num <= 1.0:
        return None, f"{field} must be in [0, 1]"
    return num, None


def _clean_str(value: Any, field: str, *, max_len: int, required: bool) -> Tuple[Optional[str], Optional[str]]:
    if value is None:
        value = ""
    if not isinstance(value, str):
        return None, f"{field} must be a string"
    cleaned = value.strip()
    if required and not cleaned:
        return None, f"{field} is required"
    if len(cleaned) > max_len:
        return None, f"{field} exceeds max length {max_len}"
    return cleaned, None


# ── 导出 ────────────────────────────────────────────────────────────────────


def _list_export_users(plugin) -> List[str]:
    with plugin._db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT canonical_user_id FROM memories "
            "UNION SELECT DISTINCT canonical_user_id FROM identity_bindings "
            "ORDER BY canonical_user_id"
        ).fetchall()
    return [str(r[0]) for r in rows]


def _export_user(plugin, canonical_id: str) -> Dict[str, Any]:
    with plugin._db() as conn:
        memory_rows = conn.execute(
            """
            SELECT canonical_user_id, memory, memory_type, score, importance,
                   confidence, source_adapter, source_user_id, persona_id,
                   scope, is_pinned
            FROM memories
            WHERE canonical_user_id = ? AND is_active = 1
            ORDER BY id
            """,
            (canonical_id,),
        ).fetchall()
        bindings = conn.execute(
            "SELECT adapter, adapter_user_id FROM identity_bindings "
            "WHERE canonical_user_id = ? ORDER BY adapter, adapter_user_id",
            (canonical_id,),
        ).fetchall()

    memories = [
        {
            "canonical_user_id": str(r["canonical_user_id"]),
            "memory": str(r["memory"]),
            "memory_type": str(r["memory_type"]),
            "score": float(r["score"]),
            "importance": float(r["importance"]),
            "confidence": float(r["confidence"]),
            "source_adapter": str(r["source_adapter"]),
            "source_user_id": str(r["source_user_id"]),
            "persona_id": str(r["persona_id"]),
            "scope": str(r["scope"]),
            "is_pinned": int(r["is_pinned"] or 0),
        }
        for r in memory_rows
    ]
    return {
        "canonical_user_id": canonical_id,
        "memories": memories,
        "bindings": [
            {"adapter": str(b["adapter"]), "adapter_user_id": str(b["adapter_user_id"])}
            for b in bindings
        ],
    }


def build_export(plugin, users: Optional[List[str]] = None) -> Dict[str, Any]:
    """构造版本化导出信封。``users=None`` 时导出全部用户。"""
    if users:
        targets = []
        seen = set()
        for raw in users:
            uid = str(raw or "").strip()
            if uid and uid not in seen:
                seen.add(uid)
                targets.append(uid)
    else:
        targets = _list_export_users(plugin)

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "exported_at": plugin._now() if hasattr(plugin, "_now") else _now(),
        "users": [_export_user(plugin, uid) for uid in targets],
    }


# ── 解析与校验 ──────────────────────────────────────────────────────────────


def _schema_version_error(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict) or "schema_version" not in payload:
        return None
    raw = payload.get("schema_version")
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return "schema_version must be an integer"
    if version != SCHEMA_VERSION:
        return f"unsupported schema_version {version} (expected {SCHEMA_VERSION})"
    return None


def _extract_user_groups(payload: Any) -> Tuple[List[Tuple[str, Any]], Optional[str]]:
    """归一化三种可接受的输入形状，返回 ``[(canonical_user_id, memories), ...]``。"""
    if isinstance(payload, dict) and isinstance(payload.get("users"), list):
        groups = []
        for entry in payload["users"]:
            if not isinstance(entry, dict):
                continue
            groups.append((entry.get("canonical_user_id"), entry.get("memories")))
        return groups, None

    if isinstance(payload, dict) and isinstance(payload.get("memories"), list):
        # 单用户 legacy 导出形状（maintenance.export_user_data）
        groups = [(payload.get("canonical_user_id"), payload["memories"])]
        if isinstance(payload.get("bindings"), list):
            groups.append((payload.get("canonical_user_id"), []))
        return groups, None

    if isinstance(payload, list):
        return [(None, payload)], None

    return [], "payload must be an export envelope, a user export object, or a memory list"


def _validate_memory(entry: Any, fallback_user: Any, index: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    def _err(reason: str) -> Tuple[None, Dict[str, Any]]:
        return None, {"index": index, "reason": reason}

    if not isinstance(entry, dict):
        return _err("record must be a json object")

    user_raw = entry.get("canonical_user_id", fallback_user)
    user, reason = _clean_str(
        user_raw, "canonical_user_id", max_len=_MAX_ID_LEN, required=True
    )
    if reason:
        return _err(reason)

    memory, reason = _clean_str(
        entry.get("memory"), "memory", max_len=_MAX_MEMORY_LEN, required=True
    )
    if reason:
        return _err(reason)

    memory_type_raw = str(entry.get("memory_type", "fact") or "fact").strip().lower()
    if memory_type_raw not in _VALID_MEMORY_TYPES:
        return _err("memory_type must be one of: " + ", ".join(sorted(_VALID_MEMORY_TYPES)))

    score, reason = _unit_float(entry.get("score"), "score", 0.6)
    if reason:
        return _err(reason)
    importance, reason = _unit_float(entry.get("importance"), "importance", 0.6)
    if reason:
        return _err(reason)
    confidence, reason = _unit_float(entry.get("confidence"), "confidence", 0.7)
    if reason:
        return _err(reason)

    scope = str(entry.get("scope", "user") or "user").strip().lower()
    if scope not in _ALLOWED_SCOPES:
        return _err("scope must be one of: " + ", ".join(sorted(_ALLOWED_SCOPES)))

    persona_id, reason = _clean_str(
        entry.get("persona_id", ""), "persona_id", max_len=_MAX_ID_LEN, required=False
    )
    if reason:
        return _err(reason)

    source_adapter, reason = _clean_str(
        entry.get("source_adapter", DEFAULT_SOURCE_ADAPTER),
        "source_adapter",
        max_len=_MAX_ID_LEN,
        required=False,
    )
    if reason:
        return _err(reason)
    source_user_id, reason = _clean_str(
        entry.get("source_user_id", DEFAULT_SOURCE_USER),
        "source_user_id",
        max_len=_MAX_ID_LEN,
        required=False,
    )
    if reason:
        return _err(reason)

    return {
        "canonical_user_id": user,
        "memory": memory,
        "memory_type": memory_type_raw,
        "score": score,
        "importance": importance,
        "confidence": confidence,
        "scope": scope,
        "persona_id": persona_id,
        "source_adapter": source_adapter or DEFAULT_SOURCE_ADAPTER,
        "source_user_id": source_user_id or DEFAULT_SOURCE_USER,
        "is_pinned": 1 if entry.get("is_pinned") else 0,
    }, None


def parse_records(payload: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """解析并逐条校验，返回 ``(valid_records, errors)``；errors 不影响 valid。"""
    groups, error = _extract_user_groups(payload)
    if error:
        return [], [{"index": -1, "reason": error}]

    valid: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    index = 0
    for fallback_user, memories in groups:
        if not isinstance(memories, list):
            errors.append(
                {"index": index, "reason": "memories must be a list"}
            )
            index += 1
            continue
        for entry in memories:
            record, reason = _validate_memory(entry, fallback_user, index)
            if record is None:
                errors.append(reason)
            else:
                valid.append(record)
            index += 1
    return valid, errors


# ── 规划（dry-run） ─────────────────────────────────────────────────────────


def plan_import(plugin, payload: Any) -> Dict[str, Any]:
    """解析 + 校验 + 去重 + 冲突检测，**不写库**。"""
    valid, errors = parse_records(payload)
    schema_error = _schema_version_error(payload)
    if schema_error:
        errors.insert(0, {"index": -1, "reason": schema_error})
        valid = []

    seen: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    duplicates: List[Dict[str, Any]] = []
    unique: List[Dict[str, Any]] = []
    for record in valid:
        key = _dedup_key(record)
        if key in seen:
            duplicates.append(record)
            continue
        seen[key] = record
        unique.append(record)

    existing_keys = _existing_keys(plugin, unique)
    to_insert: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for record in unique:
        if _dedup_key(record) in existing_keys:
            conflicts.append(record)
        else:
            to_insert.append(record)

    return {
        "schema_version": SCHEMA_VERSION,
        "valid": len(valid),
        "invalid": len(errors),
        "to_insert": len(to_insert),
        "conflicts": len(conflicts),
        "duplicates_in_payload": len(duplicates),
        "errors": errors[:_PREVIEW_LIMIT],
        "error_count": len(errors),
        "preview": {
            "new": [r["memory"][:120] for r in to_insert[:_PREVIEW_LIMIT]],
            "conflicts": [r["memory"][:120] for r in conflicts[:_PREVIEW_LIMIT]],
        },
        "_records": to_insert,
        "_insert_users": sorted({r["canonical_user_id"] for r in to_insert}),
    }


def _existing_keys(plugin, records: List[Dict[str, Any]]) -> set:
    keys: set = set()
    by_user: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        by_user.setdefault(str(record["canonical_user_id"]), []).append(record)

    with plugin._db() as conn:
        for user in by_user:
            rows = conn.execute(
                "SELECT canonical_user_id, memory, persona_id, scope FROM memories "
                "WHERE canonical_user_id = ?",
                (user,),
            ).fetchall()
            for r in rows:
                keys.add(
                    (
                        str(r["canonical_user_id"]),
                        _memory_hash(
                            str(r["persona_id"]), str(r["scope"]), str(r["memory"])
                        ),
                        str(r["persona_id"]),
                        str(r["scope"]),
                    )
                )
    return keys


# ── 备份 ────────────────────────────────────────────────────────────────────


def _backup_dir(plugin) -> str:
    db_path = str(getattr(getattr(plugin, "_db_mgr", None), "db_path", "") or "")
    base = os.path.dirname(db_path)
    if not base:
        base = os.path.join(os.getcwd(), "data")
    return os.path.join(base, "backups")


def write_backup(plugin, users: List[str]) -> str:
    """落库前把受影响用户的完整记忆快照写入 JSON 备份文件，返回路径。"""
    snapshot: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "kind": "tmemory_import_backup",
        "created_at": plugin._now(),
        "users": [],
    }
    with plugin._db() as conn:
        for user in users:
            rows = conn.execute(
                "SELECT canonical_user_id, source_adapter, source_user_id, memory, "
                "memory_type, score, importance, confidence, is_pinned, is_active, "
                "persona_id, scope FROM memories WHERE canonical_user_id = ?",
                (user,),
            ).fetchall()
            bindings = conn.execute(
                "SELECT adapter, adapter_user_id FROM identity_bindings "
                "WHERE canonical_user_id = ?",
                (user,),
            ).fetchall()
            snapshot["users"].append(
                {
                    "canonical_user_id": user,
                    "memories": [
                        {
                            "canonical_user_id": str(r["canonical_user_id"]),
                            "memory": str(r["memory"]),
                            "memory_type": str(r["memory_type"]),
                            "score": float(r["score"]),
                            "importance": float(r["importance"]),
                            "confidence": float(r["confidence"]),
                            "source_adapter": str(r["source_adapter"]),
                            "source_user_id": str(r["source_user_id"]),
                            "persona_id": str(r["persona_id"]),
                            "scope": str(r["scope"]),
                            "is_pinned": int(r["is_pinned"] or 0),
                            "is_active": int(r["is_active"] or 0),
                        }
                        for r in rows
                    ],
                    "bindings": [
                        {
                            "adapter": str(b["adapter"]),
                            "adapter_user_id": str(b["adapter_user_id"]),
                        }
                        for b in bindings
                    ],
                }
            )

    backup_dir = _backup_dir(plugin)
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"tm_import_{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
    logger.info("[tmemory] import backup written: %s", path)
    return path


# ── 落库 ────────────────────────────────────────────────────────────────────


def _insert_record(conn, record: Dict[str, Any], now: str) -> None:
    user = record["canonical_user_id"]
    persona_id = record["persona_id"]
    scope = record["scope"]
    mhash = _memory_hash(persona_id, scope, record["memory"])
    import jieba

    tokenized = " ".join(jieba.cut_for_search(record["memory"]))
    summary_channel = "persona" if record["memory_type"] == "style" else "canonical"
    conn.execute(
        """
        INSERT INTO memories(
            canonical_user_id, source_adapter, source_user_id, source_channel,
            memory_type, summary_channel, memory, tokenized_memory, memory_hash,
            score, importance, confidence, reinforce_count, attention_score,
            is_active, is_pinned, last_seen_at, created_at, updated_at, persona_id, scope
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user,
            record["source_adapter"],
            record["source_user_id"],
            "default",
            record["memory_type"],
            summary_channel,
            record["memory"],
            tokenized,
            mhash,
            _clamp01(record["score"]),
            _clamp01(record["importance"]),
            _clamp01(record["confidence"]),
            1,
            _clamp01(record["importance"]),
            1,
            1 if record["is_pinned"] else 0,
            now,
            now,
            now,
            persona_id,
            scope,
        ),
    )


def import_dataset(
    plugin,
    payload: Any,
    *,
    dry_run: bool = True,
    on_conflict: str = "skip",
    backup: bool = True,
) -> Dict[str, Any]:
    """导入用户记忆数据。

    Parameters
    ----------
    dry_run:
        ``True``（默认）只返回预览，不写库、不写备份。
    on_conflict:
        ``skip`` 幂等跳过已存在记录；``error`` 命中冲突即中止且不写任何数据。
    backup:
        落库前是否写备份文件（默认开启）。
    """
    policy = str(on_conflict or "skip").strip().lower()
    if policy not in _ALLOWED_POLICIES:
        raise ValueError(
            "on_conflict must be one of: " + ", ".join(sorted(_ALLOWED_POLICIES))
        )

    plan = plan_import(plugin, payload)
    report: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "on_conflict": policy,
        "valid": plan["valid"],
        "invalid": plan["invalid"],
        "to_insert": plan["to_insert"],
        "conflicts": plan["conflicts"],
        "duplicates_in_payload": plan["duplicates_in_payload"],
        "errors": plan["errors"],
        "error_count": plan["error_count"],
        "preview": plan["preview"],
        "backup_path": None,
        "inserted": 0,
        "rolled_back": False,
    }

    if dry_run:
        report["ok"] = True
        report["applied"] = False
        return report

    if policy == "error" and (plan["conflicts"] or plan["duplicates_in_payload"]):
        report["ok"] = False
        report["applied"] = False
        report["error"] = (
            f"conflict detected: {plan['conflicts']} existing, "
            f"{plan['duplicates_in_payload']} duplicate in payload"
        )
        return report

    records: List[Dict[str, Any]] = plan["_records"]
    if not records:
        report["ok"] = True
        report["applied"] = True
        return report

    if backup:
        report["backup_path"] = write_backup(plugin, plan["_insert_users"])

    now = plugin._now()
    try:
        with plugin._db() as conn:
            for record in records:
                _insert_record(conn, record, now)
            for user in sorted({r["canonical_user_id"] for r in records}):
                conn.execute(
                    "INSERT INTO memory_events(canonical_user_id, event_type, payload_json, created_at)"
                    " VALUES(?, ?, ?, ?)",
                    (
                        user,
                        "import",
                        json.dumps(
                            {
                                "source": GENERATOR,
                                "inserted": sum(
                                    1 for r in records if r["canonical_user_id"] == user
                                ),
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
    except Exception as exc:  # noqa: BLE001 - 事务已回滚，保留备份用于恢复
        logger.exception("[tmemory] import failed, transaction rolled back")
        report["ok"] = False
        report["applied"] = False
        report["rolled_back"] = True
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report

    report["ok"] = True
    report["applied"] = True
    report["inserted"] = len(records)
    return report


__all__ = [
    "SCHEMA_VERSION",
    "build_export",
    "parse_records",
    "plan_import",
    "import_dataset",
    "write_backup",
]
