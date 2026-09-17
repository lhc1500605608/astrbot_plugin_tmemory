"""Phase 0 元数据/声明回归：锁定「无 @register + support_platforms 合法」契约。

不依赖 PyYAML：metadata.yaml 结构简单，直接按行解析 support_platforms 列表。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LEGAL_SUPPORT_PLATFORMS = {
    "aiocqhttp",
    "qq_official",
    "telegram",
    "weixin_oc",
    "weixin_official_account",
    "wecom",
}


def test_main_has_no_register_decorator():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "@register(" not in source
    assert "register_star" not in source


def test_metadata_declares_version_and_legal_platforms():
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    assert re.search(r"^version:\s*v\d+\.\d+\.\d+\s*$", text, re.MULTILINE), (
        "metadata.yaml version 应为 vX.Y.Z"
    )

    lines = text.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines) if line.startswith("support_platforms:")
        )
    except StopIteration:
        raise AssertionError("metadata.yaml 缺少 support_platforms")

    platforms = []
    for line in lines[start + 1:]:
        match = re.match(r"^\s*-\s*(\S+)\s*$", line)
        if not match:
            break
        platforms.append(match.group(1))

    assert platforms, "support_platforms 为空"
    illegal = [p for p in platforms if p not in LEGAL_SUPPORT_PLATFORMS]
    assert not illegal, f"support_platforms 含非法 ID: {illegal}"
