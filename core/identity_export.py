"""共享身份映射导出（TMEAAA-569，plan TMEAAA-568 §2）。

tmemory 是身份权威，**全量**将当前全库身份绑定写出到共享文件
``<AstrBot data>/plugin_data/_shared/identity_map.json``（单一写者）。
tcompanion 只读该文件的 ``index`` 做 ``(adapter, adapter_user_id) → canonical``
的 O(1) 解析。

设计约束（plan §2.1）：
- 原子写：同目录临时文件 → ``os.replace()``；目录缺失先 ``makedirs``。
- fail-closed：任何异常一律捕获 + debug 日志，**绝不抛出、不影响主流程**。
- 群聊不入 index；``display_name`` 可为空；只写 id 级映射。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..adapters.config import get_astrbot_data_path

logger = logging.getLogger("astrbot")

_IDENTITY_MAP_VERSION = 1
_IDENTITY_MAP_AUTHORITY = "tmemory"
_RELATIVE_PARTS = ("plugin_data", "_shared", "identity_map.json")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def resolve_identity_map_path(data_root: str | None = None) -> Path | None:
    """解析共享映射文件路径；AstrBot 数据目录不可用时返回 ``None``。"""
    root = data_root if data_root is not None else get_astrbot_data_path()
    if not root:
        return None
    return Path(root).joinpath(*_RELATIVE_PARTS)


def _is_group_key(adapter: str, adapter_user_id: str) -> bool:
    """群聊键不入 index（映射只用于私聊 person；群聊仍 ``group:<session>``）。"""
    return (
        str(adapter_user_id or "").startswith("group:")
        or f"{adapter}:{adapter_user_id}".startswith("group:")
    )


def build_identity_map(
    binding_rows: Iterable[Any],
    profile_rows: Iterable[Any],
    *,
    updated_at: str,
) -> dict[str, Any]:
    """由绑定行 + 画像行重建 ``persons`` + ``index``（纯函数，便于单测）。"""
    display_names: dict[str, str] = {}
    profile_updated: dict[str, str] = {}
    for row in profile_rows:
        pid = str(row["canonical_user_id"])
        display_names[pid] = str(row["display_name"] or "")
        profile_updated[pid] = str(row["updated_at"] or "")

    persons: dict[str, dict[str, Any]] = {}
    index: dict[str, str] = {}

    def _person(pid: str) -> dict[str, Any]:
        return persons.setdefault(
            pid,
            {
                "display_name": display_names.get(pid, ""),
                "bindings": [],
                "updated_at": "",
            },
        )

    # 有绑定的人（群聊键跳过）
    for row in binding_rows:
        adapter = str(row["adapter"] or "")
        adapter_user_id = str(row["adapter_user_id"] or "")
        if _is_group_key(adapter, adapter_user_id):
            continue
        canonical = str(row["canonical_user_id"] or "")
        if not canonical:
            continue
        person = _person(canonical)
        person["bindings"].append(
            {"adapter": adapter, "adapter_user_id": adapter_user_id}
        )
        stamp = str(row["updated_at"] or "")
        if stamp > str(person["updated_at"]):
            person["updated_at"] = stamp
        index[f"{adapter}:{adapter_user_id}"] = canonical

    # 仅画像、无绑定的人（保持与 get_person_identities 一致）
    for pid in display_names:
        if pid not in persons:
            person = _person(pid)
            person["updated_at"] = profile_updated.get(pid, "")

    for person in persons.values():
        person["bindings"].sort(
            key=lambda b: (b["adapter"], b["adapter_user_id"])
        )
        if not person["updated_at"]:
            person["updated_at"] = updated_at

    return {
        "version": _IDENTITY_MAP_VERSION,
        "authority": _IDENTITY_MAP_AUTHORITY,
        "updated_at": updated_at,
        "persons": {pid: persons[pid] for pid in sorted(persons)},
        "index": {key: index[key] for key in sorted(index)},
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 → ``os.replace()``，避免半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".identity_map.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_identity_map(
    db_manager: Any,
    *,
    data_root: str | None = None,
    now: str | None = None,
) -> str | None:
    """全量重建并原子写出共享身份映射；失败一律 fail-closed 返回 ``None``。

    Parameters
    ----------
    db_manager
        拥有 ``db()`` 上下文管理器（返回 ``sqlite3`` 连接）的数据库管理器。
    data_root
        AstrBot 数据根目录覆盖（测试注入用）；缺省走
        ``adapters.config.get_astrbot_data_path()``。
    now
        覆盖 ``updated_at`` 时间戳（测试注入用）。

    Returns
    -------
    str | None
        成功时返回写出路径；数据目录不可用或任何异常时返回 ``None``。
    """
    try:
        path = resolve_identity_map_path(data_root)
        if path is None:
            logger.debug(
                "[tmemory] identity_map export skipped: AstrBot data path unavailable"
            )
            return None

        with db_manager.db() as conn:
            binding_rows = conn.execute(
                "SELECT adapter, adapter_user_id, canonical_user_id, updated_at "
                "FROM identity_bindings"
            ).fetchall()
            profile_rows = conn.execute(
                "SELECT canonical_user_id, display_name, updated_at FROM user_profiles"
            ).fetchall()

        payload = build_identity_map(
            binding_rows, profile_rows, updated_at=now or _now()
        )
        _atomic_write(path, payload)
        logger.debug(
            "[tmemory] identity_map exported: %s (%d persons, %d index)",
            path,
            len(payload["persons"]),
            len(payload["index"]),
        )
        return str(path)
    except Exception as e:  # noqa: BLE001 - 导出绝不影响主流程
        logger.debug("[tmemory] identity_map export failed (ignored): %s", e)
        return None
