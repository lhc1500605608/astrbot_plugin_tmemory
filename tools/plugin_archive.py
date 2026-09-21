#!/usr/bin/env python3
"""Build and verify AstrBot plugin archives (TMEAAA-378).

Produces a single-top-level-directory zip whose root resolves to
``astrbot_plugin_tmemory`` and whose ``metadata.yaml`` is discoverable by
AstrBot's archive inspector, and rejects archives polluted with macOS
resource forks (``__MACOSX``/``._*``) or development caches.

The verification logic mirrors ``astrbot.core.zip_updater.PluginUpdater``
(``_resolve_archive_root_dir``) and
``astrbot.core.star.updater.PluginUpdater.find_plugin_metadata_entry`` in
AstrBot 4.28.1, reimplemented here so packaging can be validated without
installing AstrBot.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

PLUGIN_ROOT_NAME = "astrbot_plugin_tmemory"
PLUGIN_METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")

INCLUDE_TOP_LEVEL_FILES = (
    "metadata.yaml",
    "main.py",
    "requirements.txt",
    "_conf_schema.json",
    "logo.png",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "hybrid_search.py",
    "vector_manager.py",
)

INCLUDE_TOP_LEVEL_DIRS = (
    "adapters",
    "core",
    "search",
    "web",
    "pages",
    "templates",
    "skills",
    ".astrbot-plugin",
)

EXCLUDE_DIR_NAMES = frozenset(
    {
        "__pycache__",
        "__MACOSX",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        ".git",
        ".worktrees",
        "node_modules",
        ".idea",
        ".vscode",
    }
)

EXCLUDE_FILE_NAMES = frozenset(
    {
        ".DS_Store",
        ".env",
        "Thumbs.db",
        ".git",
    }
)

EXCLUDE_FILE_PREFIXES = ("._",)

EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo", ".zip", ".log")

JUNK_MARKERS = frozenset({"__MACOSX", ".DS_Store", "__pycache__"})


class ArchiveValidationError(RuntimeError):
    """Raised when an archive is not installable as an AstrBot plugin."""


def _is_excluded(rel_parts: tuple[str, ...], name: str) -> bool:
    if any(part in EXCLUDE_DIR_NAMES for part in rel_parts):
        return True
    if name in EXCLUDE_FILE_NAMES:
        return True
    if name.startswith(EXCLUDE_FILE_PREFIXES):
        return True
    if name.endswith(EXCLUDE_FILE_SUFFIXES):
        return True
    return False


def collect_archive_entries(repo_root: Path) -> list[tuple[Path, str]]:
    """Return ``(source_path, archive_name)`` pairs for the plugin payload."""
    entries: list[tuple[Path, str]] = []

    for filename in INCLUDE_TOP_LEVEL_FILES:
        source = repo_root / filename
        if not source.is_file():
            raise ArchiveValidationError(f"缺少必需的插件文件: {filename}")
        entries.append((source, f"{PLUGIN_ROOT_NAME}/{filename}"))

    for dirname in INCLUDE_TOP_LEVEL_DIRS:
        source_dir = repo_root / dirname
        if not source_dir.is_dir():
            raise ArchiveValidationError(f"缺少必需的插件目录: {dirname}")
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(source_dir).parts
            if _is_excluded(rel_parts, path.name):
                continue
            archive_name = "/".join((PLUGIN_ROOT_NAME, dirname, *rel_parts))
            entries.append((path, archive_name))

    return entries


def build_archive(repo_root: Path, output_path: Path) -> Path:
    """Write a clean single-root plugin zip and verify it."""
    entries = collect_archive_entries(repo_root)
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, archive_name in entries:
            archive.write(source, archive_name)

    report = verify_archive(output_path)
    print(
        f"[pack] {output_path} entries={report['entry_count']} "
        f"root={report['root']!r} metadata={report['metadata_entry']!r}"
    )
    return output_path


def _resolve_archive_root_dir(entries: list[str]) -> str:
    """Port of AstrBot's ``_resolve_archive_root_dir`` (4.28.1)."""
    normalized_entries = [os.path.normpath(entry) for entry in entries]
    portable_entries = [entry.replace("\\", "/") for entry in normalized_entries]
    root_candidates: list[str] = []

    for raw_entry, normalized_entry, portable_entry in zip(
        entries, normalized_entries, portable_entries
    ):
        if normalized_entry == ".":
            continue

        has_children = any(
            other_entry != portable_entry
            and other_entry.startswith(f"{portable_entry}/")
            for other_entry in portable_entries
        )
        if raw_entry.endswith(("/", "\\")) or has_children:
            root_candidates.append(normalized_entry)
            continue

        parent_portable, _, _ = portable_entry.rpartition("/")
        if not parent_portable:
            return ""
        root_candidates.append(parent_portable.replace("/", os.sep))

    if not root_candidates:
        return ""
    try:
        return os.path.commonpath(root_candidates)
    except ValueError:
        return ""


def find_plugin_metadata_entry(entries: list[str]) -> str | None:
    """Port of AstrBot's ``find_plugin_metadata_entry`` (4.28.1)."""
    update_dir = _resolve_archive_root_dir(entries)
    portable_update_dir = os.path.normpath(update_dir).replace("\\", "/")
    if portable_update_dir == ".":
        portable_update_dir = ""

    entries_by_portable_path: dict[str, str] = {}
    for entry in entries:
        portable_entry = os.path.normpath(entry).replace("\\", "/")
        if portable_entry in ("", "."):
            continue
        entries_by_portable_path[portable_entry] = entry

    metadata_candidates = (
        [f"{portable_update_dir}/{filename}" for filename in PLUGIN_METADATA_FILENAMES]
        if portable_update_dir
        else list(PLUGIN_METADATA_FILENAMES)
    )
    for candidate in metadata_candidates:
        if candidate in entries_by_portable_path:
            return entries_by_portable_path[candidate]
    return None


def find_junk_entries(entries: list[str]) -> list[str]:
    """Return archive entries that should never ship in a plugin zip."""
    junk: list[str] = []
    for entry in entries:
        parts = entry.replace("\\", "/").split("/")
        name = parts[-1] if parts else entry
        if entry.endswith("/"):
            name = parts[-2] if len(parts) >= 2 else name
        if (
            any(part in JUNK_MARKERS for part in parts)
            or name in JUNK_MARKERS
            or name.startswith(EXCLUDE_FILE_PREFIXES)
            or name in EXCLUDE_FILE_NAMES
        ):
            junk.append(entry)
    return junk


def verify_archive(zip_path: Path) -> dict[str, object]:
    """Validate an archive the way AstrBot does; raise on failure."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise ArchiveValidationError(f"压缩包不存在: {zip_path}")

    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            entries = archive.namelist()
    except zipfile.BadZipFile as exc:
        raise ArchiveValidationError(f"压缩包格式错误: {zip_path}") from exc

    if not entries:
        raise ArchiveValidationError("压缩包为空。")

    junk = find_junk_entries(entries)
    if junk:
        preview = ", ".join(junk[:5])
        raise ArchiveValidationError(
            f"压缩包含 macOS/开发垃圾条目 ({len(junk)} 条): {preview}；"
            "AstrBot 的 commonpath 根目录推导会因此退化，导致 metadata.yaml 找不到。"
        )

    root = _resolve_archive_root_dir(entries)
    metadata_entry = find_plugin_metadata_entry(entries)
    if metadata_entry is None:
        raise ArchiveValidationError(
            "压缩包不是合法的 AstrBot 插件：未找到 metadata.yaml 或 metadata.yml。"
            f"（推导根目录 root={root!r}）"
        )

    top_level = {
        entry.split("/", 1)[0]
        for entry in entries
        if entry.strip("/")
    }
    if top_level != {PLUGIN_ROOT_NAME}:
        raise ArchiveValidationError(
            f"压缩包顶层目录必须唯一为 {PLUGIN_ROOT_NAME}/，实际为 {sorted(top_level)}"
        )

    portable_root = os.path.normpath(root).replace("\\", "/")
    if portable_root != PLUGIN_ROOT_NAME:
        raise ArchiveValidationError(
            f"压缩包根目录解析结果应为 {PLUGIN_ROOT_NAME}，实际为 {portable_root!r}"
        )

    return {
        "entry_count": len(entries),
        "root": portable_root,
        "metadata_entry": metadata_entry,
        "path": str(zip_path),
    }


def _default_output(repo_root: Path) -> Path:
    return repo_root / "dist" / f"{PLUGIN_ROOT_NAME}.zip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack_parser = subparsers.add_parser("pack", help="build and verify a plugin zip")
    pack_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    pack_parser.add_argument("--output", type=Path, default=None)

    verify_parser = subparsers.add_parser("verify", help="verify an existing zip")
    verify_parser.add_argument("zip_path", type=Path)

    args = parser.parse_args(argv)

    try:
        if args.command == "pack":
            repo_root = args.repo_root.resolve()
            output = args.output or _default_output(repo_root)
            build_archive(repo_root, output)
        else:
            report = verify_archive(args.zip_path)
            print(
                f"[verify] OK {report['path']} entries={report['entry_count']} "
                f"root={report['root']!r} metadata={report['metadata_entry']!r}"
            )
    except ArchiveValidationError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
