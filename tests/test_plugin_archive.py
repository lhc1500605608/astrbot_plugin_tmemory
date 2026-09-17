"""Regression tests for the plugin packaging path (TMEAAA-378).

Covers the AstrBot archive root-resolution contract that broke when
``__MACOSX`` resource forks were shipped alongside the plugin directory.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import plugin_archive  # noqa: E402


@pytest.fixture(scope="module")
def built_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("archive") / "astrbot_plugin_tmemory.zip"
    return plugin_archive.build_archive(ROOT, output)


def test_resolve_root_single_top_level_dir() -> None:
    entries = [
        "astrbot_plugin_tmemory/",
        "astrbot_plugin_tmemory/main.py",
        "astrbot_plugin_tmemory/core/db.py",
    ]
    assert plugin_archive._resolve_archive_root_dir(entries) == "astrbot_plugin_tmemory"


def test_resolve_root_degenerates_with_macosx() -> None:
    entries = [
        "astrbot_plugin_tmemory/main.py",
        "astrbot_plugin_tmemory/metadata.yaml",
        "__MACOSX/._main.py",
    ]
    assert plugin_archive._resolve_archive_root_dir(entries) == ""
    assert plugin_archive.find_plugin_metadata_entry(entries) is None


def test_built_archive_is_installable(built_archive: Path) -> None:
    report = plugin_archive.verify_archive(built_archive)
    assert report["root"] == plugin_archive.PLUGIN_ROOT_NAME
    assert report["metadata_entry"] == (
        f"{plugin_archive.PLUGIN_ROOT_NAME}/metadata.yaml"
    )


def test_built_archive_excludes_dev_artifacts(built_archive: Path) -> None:
    with zipfile.ZipFile(built_archive) as archive:
        entries = archive.namelist()

    assert plugin_archive.find_junk_entries(entries) == []
    joined = "\n".join(entries)
    assert "__MACOSX" not in joined
    assert "__pycache__" not in joined
    assert ".env\n" not in joined + "\n"
    assert not any(entry.startswith(f"{plugin_archive.PLUGIN_ROOT_NAME}/tests/") for entry in entries)
    assert not any(entry.startswith(f"{plugin_archive.PLUGIN_ROOT_NAME}/data/") for entry in entries)


def test_verify_rejects_macosx_polluted_archive(tmp_path: Path) -> None:
    polluted = tmp_path / "polluted.zip"
    with zipfile.ZipFile(polluted, "w") as archive:
        archive.writestr("astrbot_plugin_tmemory/metadata.yaml", "name: x\n")
        archive.writestr("__MACOSX/._metadata.yaml", "junk")

    with pytest.raises(plugin_archive.ArchiveValidationError):
        plugin_archive.verify_archive(polluted)


def test_verify_rejects_archive_without_metadata(tmp_path: Path) -> None:
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("astrbot_plugin_tmemory/main.py", "print('hi')\n")

    with pytest.raises(plugin_archive.ArchiveValidationError):
        plugin_archive.verify_archive(broken)


def test_verify_rejects_multiple_top_level_dirs(tmp_path: Path) -> None:
    messy = tmp_path / "messy.zip"
    with zipfile.ZipFile(messy, "w") as archive:
        archive.writestr("astrbot_plugin_tmemory/metadata.yaml", "name: x\n")
        archive.writestr("other_dir/readme.md", "hi")

    with pytest.raises(plugin_archive.ArchiveValidationError):
        plugin_archive.verify_archive(messy)
