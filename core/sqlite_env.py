"""SQLite 运行环境能力探测与 pysqlite3 回退（TMEAAA-474 / TMEAAA-475）。

生产 AstrBot 的 Python ``sqlite3`` 可能编译期未启用 loadable extensions / FTS5，
导致 ``sqlite_vec.load(conn)`` 失败、vec0 不可用。本模块是**唯一权威探测入口**：

- ``probe_sqlite_environment()``：纯探测，不抛异常，返回不可变 ``SqliteEnvReport``。
- ``install_sqlite3_shim()``：stdlib 缺 load_extension/FTS5 且 ``pysqlite3`` 可导入时
  把 ``sys.modules["sqlite3"]`` 切换到 pysqlite3。**必须在其它 ``.core.*`` import 之前
  调用**（main.py / web/legacy_server.py 入口），否则 db.py 等模块已绑定旧 sqlite3。

只依赖标准库，不 import astrbot / 其它插件模块（R1 边界约束）。
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("astrbot")

# ── 稳定原因码（供面板映射中文，禁止随意改名）─────────────────────────────
REASON_SQLITE_VEC_NOT_INSTALLED = "sqlite_vec_not_installed"
REASON_LOAD_EXTENSION_MISSING = "load_extension_missing"
REASON_FTS5_MISSING = "fts5_missing"
REASON_VEC0_UNAVAILABLE = "vec0_unavailable"

#: 原因码的规范顺序（保证 payload / 单测稳定）。
_REASON_ORDER: Tuple[str, ...] = (
    REASON_SQLITE_VEC_NOT_INSTALLED,
    REASON_LOAD_EXTENSION_MISSING,
    REASON_FTS5_MISSING,
    REASON_VEC0_UNAVAILABLE,
)

_FIX_PYSQLITE3 = "pip install pysqlite3-binary"
_FIX_SQLITE_VEC = "pip install sqlite-vec"


def _fix_cmd(pkg: str) -> str:
    """修复命令必须作用于**运行解释器**（契约 §8）：优先用 sys.executable 限定。"""
    exe = ""
    try:
        exe = str(sys.executable or "")
    except Exception:
        exe = ""
    if exe:
        return f"{exe} -m pip install {pkg}"
    return f"pip install {pkg}"



def _ordered_reasons(codes: Iterable[str]) -> Tuple[str, ...]:
    unique = {str(code) for code in codes if code}
    return tuple(code for code in _REASON_ORDER if code in unique)


def build_hint(reasons: Iterable[str]) -> str:
    """把原因码翻译成包含可执行修复命令的人可读指引；无问题时返回空串。"""
    codes = {str(code) for code in reasons if code}
    if not codes:
        return ""
    parts = []
    if REASON_LOAD_EXTENSION_MISSING in codes or REASON_FTS5_MISSING in codes:
        machine = _platform_machine()
        if pysqlite3_wheel_available(machine):
            parts.append(
                "当前 Python 的 sqlite3 未启用 loadable extensions 或 FTS5："
                "请安装带扩展支持的 SQLite，或执行 `"
                f"{_fix_cmd('pysqlite3-binary')}` 后重启插件（会自动切换到 pysqlite3 后端）"
            )
        else:
            parts.append(
                f"当前 Python 的 sqlite3 未启用 loadable extensions 或 FTS5，"
                f"且本机架构 {machine or 'unknown'} 无 pysqlite3-binary 预编译包："
                "请改用带 loadable extensions/FTS5 的 Python 构建"
                "（如 python.org 官方构建或发行版 python3-sqlite 源），或自行编译 pysqlite3"
            )
    if REASON_SQLITE_VEC_NOT_INSTALLED in codes:
        parts.append(f"向量检索扩展缺失：请执行 `{_fix_cmd('sqlite-vec')}`")
    if (
        REASON_VEC0_UNAVAILABLE in codes
        and REASON_SQLITE_VEC_NOT_INSTALLED not in codes
    ):
        parts.append(
            "sqlite-vec 已安装但 vec0 未能在连接上加载："
            "请检查 load_extension 是否被禁用及 sqlite-vec 版本"
        )
    return "；".join(parts) + "。"


@dataclass(frozen=True)
class SqliteEnvReport:
    """一次 SQLite 环境探测的不可变快照（``vec0_available`` 由调用方回填）。"""

    backend: str
    python_version: str
    sqlite_version: str
    has_load_extension: bool
    has_fts5: bool
    pysqlite3_installed: bool
    pysqlite3_version: str
    sqlite_vec_installed: bool
    vec0_available: bool = False
    reasons: Tuple[str, ...] = ()
    hint: str = ""
    # 解释器身份（契约 §7）：让面板/日志自报运行解释器，不靠外部命令猜。
    python_executable: str = ""
    python_prefix: str = ""
    in_venv: bool = False
    # 宿主 CPU 架构（契约 §10）：决定 pysqlite3-binary 是否有可用 wheel。
    platform_machine: str = ""

    def with_reason(self, code: str) -> "SqliteEnvReport":
        """追加一个原因码（幂等），并重算 hint。"""
        reasons = _ordered_reasons((*self.reasons, code))
        return replace(self, reasons=reasons, hint=build_hint(reasons))

    def with_vec0_available(self, available: bool) -> "SqliteEnvReport":
        """回填连接级 vec0 探测结果；不可用时并入 ``vec0_unavailable``。"""
        report = replace(self, vec0_available=bool(available))
        if not available:
            report = report.with_reason(REASON_VEC0_UNAVAILABLE)
        return report

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "python_version": self.python_version,
            "sqlite_version": self.sqlite_version,
            "has_load_extension": self.has_load_extension,
            "has_fts5": self.has_fts5,
            "pysqlite3_installed": self.pysqlite3_installed,
            "pysqlite3_version": self.pysqlite3_version,
            "sqlite_vec_installed": self.sqlite_vec_installed,
            "vec0_available": self.vec0_available,
            "reasons": list(self.reasons),
            "hint": self.hint,
            "python_executable": self.python_executable,
            "python_prefix": self.python_prefix,
            "in_venv": self.in_venv,
            "platform_machine": self.platform_machine,
            "pysqlite3_wheel_available": pysqlite3_wheel_available(
                self.platform_machine
            ),
        }


# ── 细粒度探测（单测可 monkeypatch）────────────────────────────────────────


def _active_sqlite3_module() -> Any:
    """返回当前生效的 ``sqlite3`` 模块（可能是 shim 后的 pysqlite3）。"""
    return importlib.import_module("sqlite3")


def _detect_load_extension(mod: Any) -> bool:
    connection_cls = getattr(mod, "Connection", None)
    return callable(getattr(connection_cls, "load_extension", None))


def _detect_fts5(mod: Any) -> bool:
    conn = None
    try:
        conn = mod.connect(":memory:")
    except Exception:
        return False
    try:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE temp.__tmemory_fts5_probe USING fts5(x)"
            )
            conn.execute("DROP TABLE temp.__tmemory_fts5_probe")
            return True
        except Exception:
            pass
        try:
            options = {
                str(row[0]) for row in conn.execute("PRAGMA compile_options").fetchall()
            }
            return "ENABLE_FTS5" in options
        except Exception:
            return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _detect_pysqlite3() -> Tuple[bool, str]:
    mod = sys.modules.get("pysqlite3")
    if mod is None:
        try:
            mod = importlib.import_module("pysqlite3")
        except Exception:
            return False, ""
    for attr in ("__version__", "version", "sqlite_version"):
        value = getattr(mod, attr, "")
        if value:
            return True, str(value)
    return True, ""


def _detect_sqlite_vec() -> bool:
    if "sqlite_vec" in sys.modules:
        return True
    try:
        importlib.import_module("sqlite_vec")
        return True
    except Exception:
        return False


def probe_sqlite_environment() -> SqliteEnvReport:
    """探测当前 sqlite3 后端能力；任何异常都降级为「能力缺失」，绝不抛出。"""
    backend = "sqlite3"
    sqlite_version = ""
    has_load_extension = False
    has_fts5 = False
    try:
        mod = _active_sqlite3_module()
    except Exception:
        mod = None
    if mod is not None:
        if str(getattr(mod, "__name__", "sqlite3")) == "pysqlite3":
            backend = "pysqlite3"
        sqlite_version = str(getattr(mod, "sqlite_version", "") or "")
        has_load_extension = _detect_load_extension(mod)
        has_fts5 = _detect_fts5(mod)

    pysqlite3_installed, pysqlite3_version = _detect_pysqlite3()
    sqlite_vec_installed = _detect_sqlite_vec()

    reasons = []
    if not sqlite_vec_installed:
        reasons.append(REASON_SQLITE_VEC_NOT_INSTALLED)
    if not has_load_extension:
        reasons.append(REASON_LOAD_EXTENSION_MISSING)
    if not has_fts5:
        reasons.append(REASON_FTS5_MISSING)
    ordered = _ordered_reasons(reasons)

    return SqliteEnvReport(
        backend=backend,
        python_version=sys.version.split()[0],
        sqlite_version=sqlite_version,
        has_load_extension=has_load_extension,
        has_fts5=has_fts5,
        pysqlite3_installed=pysqlite3_installed,
        pysqlite3_version=pysqlite3_version,
        sqlite_vec_installed=sqlite_vec_installed,
        vec0_available=False,
        reasons=ordered,
        hint=build_hint(ordered),
        python_executable=_python_executable(),
        python_prefix=_python_prefix(),
        in_venv=_in_venv(),
        platform_machine=_platform_machine(),
    )


def _python_executable() -> str:
    try:
        return str(sys.executable or "")
    except Exception:  # noqa: BLE001 - 自报不应阻断探测
        return ""


def _python_prefix() -> str:
    try:
        return str(sys.prefix or "")
    except Exception:  # noqa: BLE001
        return ""


def _in_venv() -> bool:
    try:
        return bool(
            getattr(sys, "prefix", None) != getattr(sys, "base_prefix", sys.prefix)
        )
    except Exception:  # noqa: BLE001
        return False


#: pysqlite3-binary 仅发布 x86_64 manylinux wheel（PyPI 实测：无 aarch64/arm64）。
_PYSQLITE3_WHEEL_MACHINES = ("x86_64", "amd64", "x86-64")


def pysqlite3_wheel_available(machine: str) -> bool:
    """该 CPU 架构是否可安装 ``pysqlite3-binary``（决定回退是否可行）。"""
    return str(machine or "").strip().lower() in _PYSQLITE3_WHEEL_MACHINES


def _platform_machine() -> str:
    try:
        import platform as _platform

        return str(_platform.machine() or "")
    except Exception:  # noqa: BLE001
        return ""


def format_self_report(report: "SqliteEnvReport") -> str:
    """契约 §7 的结构化自报行（启动时无条件输出一行）。"""
    return (
        "[tmemory] sqlite env self-report: "
        f"interpreter={report.python_executable} "
        f"prefix={report.python_prefix} "
        f"venv={report.in_venv} "
        f"machine={report.platform_machine} "
        f"python={report.python_version} "
        f"sqlite={report.sqlite_version} "
        f"load_extension={report.has_load_extension} "
        f"fts5={report.has_fts5} "
        f"sqlite_vec_installed={report.sqlite_vec_installed} "
        f"vec0={report.vec0_available} "
        f"reasons={list(report.reasons)}"
    )


def log_sqlite_env_self_report(plugin: Any = None) -> "SqliteEnvReport":
    """幂等探测并输出自报日志；传 plugin 时把报告缓存到 ``plugin._sqlite_env``。

    与 ``enable_vector_search`` 无关：启动时必须无条件调用。
    """
    report = probe_sqlite_environment()
    if plugin is not None:
        try:
            plugin._sqlite_env = report
        except Exception:  # noqa: BLE001
            pass
    logger.info(format_self_report(report))
    return report


_shim_lock = threading.Lock()
_shim_installed = False


def install_sqlite3_shim() -> SqliteEnvReport:
    """必要且可行时把 ``sys.modules["sqlite3"]`` 切换到 pysqlite3。

    幂等：重复调用安全；已是 pysqlite3 后端时直接返回当前探测结果。
    """
    global _shim_installed
    with _shim_lock:
        report = probe_sqlite_environment()
        if report.backend == "pysqlite3":
            _shim_installed = True
            return report
        needs_shim = (not report.has_load_extension) or (not report.has_fts5)
        if not needs_shim or not report.pysqlite3_installed:
            return report
        try:
            import pysqlite3  # type: ignore[import-not-found]
        except Exception as e:  # noqa: BLE001 - 回退失败保持 stdlib
            logger.warning("[tmemory] pysqlite3 导入失败，保持 stdlib sqlite3: %s", e)
            return report
        sys.modules["sqlite3"] = pysqlite3
        _shim_installed = True
        logger.warning(
            "[tmemory] 系统 sqlite3 缺少 load_extension 或 FTS5；"
            "已切换到 pysqlite3 后端（backend=pysqlite3, sqlite=%s）。",
            getattr(pysqlite3, "sqlite_version", "unknown"),
        )
        return probe_sqlite_environment()


def get_env_report(plugin: Any) -> SqliteEnvReport:
    """返回插件缓存的报告；缺失时惰性探测（保证面板字段永远可用）。"""
    report = getattr(plugin, "_sqlite_env", None)
    if isinstance(report, SqliteEnvReport):
        return report
    return probe_sqlite_environment()


def sqlite_env_dict(plugin: Any) -> Dict[str, Any]:
    return get_env_report(plugin).to_dict()


def last_dim_change_dict(plugin: Any) -> Optional[Dict[str, Any]]:
    """返回最近一次 provider 维度变更结果（供面板展示「未重建原因」）。"""
    result = getattr(plugin, "_last_dim_change_result", None)
    return result if isinstance(result, dict) else None


def collect_sqlite_capabilities(
    plugin: Any, stats: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """构造 ``capabilities.{vector_search,sqlite_vec,fts5,vector_index_rows}``。"""
    report = get_env_report(plugin)
    vec_available = bool(getattr(plugin, "_vec_available", False))
    sqlite_vec_loaded = bool(
        getattr(plugin, "_sqlite_vec", None) is not None and vec_available
    )
    vector_index_rows: Optional[int] = None
    if vec_available and isinstance(stats, dict):
        try:
            vector_index_rows = int(stats.get("vector_index_rows", 0) or 0)
        except (TypeError, ValueError):
            vector_index_rows = 0
    return {
        "vector_search": vec_available,
        "sqlite_vec": sqlite_vec_loaded,
        "fts5": bool(report.has_fts5),
        "vector_index_rows": vector_index_rows,
    }


__all__ = [
    "REASON_SQLITE_VEC_NOT_INSTALLED",
    "REASON_LOAD_EXTENSION_MISSING",
    "REASON_FTS5_MISSING",
    "REASON_VEC0_UNAVAILABLE",
    "SqliteEnvReport",
    "build_hint",
    "collect_sqlite_capabilities",
    "format_self_report",
    "get_env_report",
    "install_sqlite3_shim",
    "last_dim_change_dict",
    "log_sqlite_env_self_report",
    "probe_sqlite_environment",
    "sqlite_env_dict",
]
