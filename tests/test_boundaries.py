"""Boundary tests (R1/R2) — TMEAAA-357 / Plan TMEAAA-354 §3.1.

R1: ``core/**`` 禁止 import ``astrbot*``（仅允许 ``if TYPE_CHECKING:`` 下的类型标注）。
R2: AstrBot 运行时依赖仅允许出现在边界层（``adapters/**``、``main.py``、旧 ``web_*.py``）。
R3: 事件私有字段 ``event._extras["conversation"]`` 只能在 ``adapters/`` 内访问。

纯静态 AST 扫描，不依赖 AstrBot 运行时 stub。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
ADAPTERS = ROOT / "adapters"

# R2 边界白名单：允许出现 AstrBot 依赖的目录 / 文件（相对仓库根）。
# ``docker/`` 下的脚本在 AstrBot 容器内运行（如 seed_openapi_key.py 复用
# astrbot.dashboard 的 ApiKeyService），属于容器边界层，不是插件运行时。
BOUNDARY_DIRS = {"adapters", "web", "tests", "docker", ".worktrees", ".git"}
BOUNDARY_FILES = {"main.py", "web_handlers.py"}
# 扫描时跳过的非源码目录（虚拟环境 / 运行数据 / 缓存）。
SKIP_DIRS = {
    ".git",
    ".worktrees",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "data",
    "node_modules",
    "site-packages",
    "dist",
}

ADAPTER_MODULES = ("event", "llm", "conversation", "config")
ADAPTER_PORT_FUNCTIONS = {
    "event": {
        "get_unified_msg_origin",
        "get_platform_str",
        "get_adapter_name",
        "get_adapter_user_id",
        "get_identity_adapter_name",
        "get_memory_scope",
        "is_group_event",
        "get_current_persona",
    },
    "llm": {"inject_extra_user_temp"},
    "conversation": {"get_current_persona_id"},
    "config": {"get_astrbot_data_path"},
}


def _python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _is_type_checking_test(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _astrbot_imports(tree: ast.AST):
    """Yield (lineno, module, inside_type_checking) for every astrbot import."""
    findings: list[tuple[int, str, bool]] = []

    def visit(node: ast.AST, in_tc: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_in_tc = in_tc
            if isinstance(child, ast.If) and _is_type_checking_test(child.test):
                child_in_tc = True
            if isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name == "astrbot" or alias.name.startswith("astrbot."):
                        findings.append((child.lineno, alias.name, in_tc))
            elif isinstance(child, ast.ImportFrom):
                module = child.module or ""
                if module == "astrbot" or module.startswith("astrbot."):
                    findings.append((child.lineno, module, in_tc))
            visit(child, child_in_tc)

    visit(tree, False)
    return findings


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_r1_core_has_no_runtime_astrbot_imports():
    violations = []
    for path in _python_files(CORE):
        for lineno, module, in_tc in _astrbot_imports(_parse(path)):
            if not in_tc:
                rel = path.relative_to(ROOT)
                violations.append(f"{rel}:{lineno} imports {module}")
    assert not violations, (
        "R1 violated — core/ must not import astrbot* outside TYPE_CHECKING:\n  "
        + "\n  ".join(violations)
    )


def test_r2_astrbot_imports_confined_to_boundary_layers():
    violations = []
    for path in _python_files(ROOT):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in BOUNDARY_DIRS or str(rel) in BOUNDARY_FILES:
            continue
        for lineno, module, in_tc in _astrbot_imports(_parse(path)):
            if not in_tc:
                violations.append(f"{rel}:{lineno} imports {module}")
    assert not violations, (
        "R2 violated — runtime AstrBot imports must stay in boundary layers "
        "(adapters/, main.py, web_*.py):\n  " + "\n  ".join(violations)
    )


def test_r2_adapters_expose_port_surface():
    missing = []
    for module in ADAPTER_MODULES:
        path = ADAPTERS / f"{module}.py"
        if not path.exists():
            missing.append(f"adapters/{module}.py (missing)")
            continue
        tree = _parse(path)
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ADAPTER_PORT_FUNCTIONS[module]:
            if name not in defined:
                missing.append(f"adapters/{module}.py::{name} (missing)")
    assert not missing, "adapters port surface incomplete:\n  " + "\n  ".join(missing)


def test_r3_extras_conversation_access_is_adapter_only():
    offenders = []
    for path in _python_files(CORE):
        source = path.read_text(encoding="utf-8")
        if "_extras" in source:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "R3 violated — event._extras must only be read inside adapters/:\n  "
        + "\n  ".join(offenders)
    )

    adapter_source = (ADAPTERS / "event.py").read_text(encoding="utf-8")
    assert "_extras" in adapter_source, (
        "adapters/event.py must own the event._extras fallback read"
    )
