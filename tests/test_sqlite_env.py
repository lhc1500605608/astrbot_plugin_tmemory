"""TMEAAA-475 单测：SQLite 环境能力探测 / pysqlite3 回退 / 具体原因透出。

覆盖验收：
- 探测降级原因码（sqlite_vec_not_installed / load_extension_missing / fts5_missing）
- vec0 回填与 ``vec0_unavailable``
- shim 幂等（缺能力 + pysqlite3 可用时切换；不可用时 no-op）
- db.py 记录 ``load_extension_missing``
- ``/capabilities`` 新字段契约（bridge 与 legacy 共用同一 helper）
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sqlite_env(plugin_module):
    # 依赖 plugin_module 以注册 astrbot stub 与 astrbot_plugin_tmemory 包。
    import astrbot_plugin_tmemory.core.sqlite_env as module

    return module


class FakeQuery:
    def get(self, key, default=None, *args, **kwargs):
        return default


class FakeRequest:
    def __init__(self, json_body=None, query=None, username="admin"):
        self._json = {} if json_body is None else json_body
        self.query = FakeQuery()
        self.username = username

    async def json(self, default=None):
        return self._json


def run(coro):
    return asyncio.run(coro)


# ── 探测：原因码 ────────────────────────────────────────────────────────────


def test_probe_reports_specific_reason_codes(sqlite_env, monkeypatch):
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_sqlite_vec", lambda: False)
    monkeypatch.setattr(sqlite_env, "_platform_machine", lambda: "x86_64")

    report = sqlite_env.probe_sqlite_environment()

    assert report.has_load_extension is False
    assert report.has_fts5 is False
    assert report.sqlite_vec_installed is False
    assert report.reasons == (
        "sqlite_vec_not_installed",
        "load_extension_missing",
        "fts5_missing",
    )
    assert "pip install sqlite-vec" in report.hint
    assert "pip install pysqlite3-binary" in report.hint
    assert report.vec0_available is False


def test_hint_falls_back_to_python_build_when_no_pysqlite3_wheel(
    sqlite_env, monkeypatch
):
    """契约 §10：aarch64/arm64 无 pysqlite3-binary wheel → 指引改用带扩展的 Python 构建。"""
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_sqlite_vec", lambda: True)
    monkeypatch.setattr(sqlite_env, "_platform_machine", lambda: "aarch64")

    report = sqlite_env.probe_sqlite_environment()

    assert report.reasons == ("load_extension_missing",)
    assert "pip install pysqlite3-binary" not in report.hint
    assert "aarch64" in report.hint
    assert "loadable extensions" in report.hint
    assert sqlite_env.pysqlite3_wheel_available("aarch64") is False
    assert sqlite_env.pysqlite3_wheel_available("x86_64") is True



def test_probe_reason_codes_are_stable_and_ordered(sqlite_env, monkeypatch):
    """即使探测顺序不同，原因码顺序必须稳定（面板/单测契约）。"""
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_sqlite_vec", lambda: False)
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: True)

    report = sqlite_env.probe_sqlite_environment()

    assert report.reasons == ("sqlite_vec_not_installed", "fts5_missing")


def test_probe_healthy_env_has_no_reasons_or_hint(sqlite_env, monkeypatch):
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_sqlite_vec", lambda: True)

    report = sqlite_env.probe_sqlite_environment()

    assert report.reasons == ()
    assert report.hint == ""
    assert report.has_load_extension is True
    assert report.has_fts5 is True


def test_vec0_backfill_adds_reason_without_mutating(sqlite_env, monkeypatch):
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_sqlite_vec", lambda: True)

    report = sqlite_env.probe_sqlite_environment()
    unavailable = report.with_vec0_available(False)
    available = report.with_vec0_available(True)

    assert report.vec0_available is False
    assert "vec0_unavailable" not in report.reasons
    assert unavailable.vec0_available is False
    assert unavailable.reasons == ("vec0_unavailable",)
    assert available.vec0_available is True
    assert available.reasons == ()


def test_report_to_dict_matches_payload_contract(sqlite_env, monkeypatch):
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_sqlite_vec", lambda: True)

    payload = sqlite_env.probe_sqlite_environment().to_dict()

    assert set(payload) == {
        "backend",
        "python_version",
        "sqlite_version",
        "has_load_extension",
        "has_fts5",
        "pysqlite3_installed",
        "pysqlite3_version",
        "sqlite_vec_installed",
        "vec0_available",
        "reasons",
        "hint",
        "python_executable",
        "python_prefix",
        "in_venv",
        "platform_machine",
        "pysqlite3_wheel_available",
    }
    assert isinstance(payload["reasons"], list)


# ── 契约 §7：解释器自报 ─────────────────────────────────────────────────────


def test_probe_reports_interpreter_identity(sqlite_env):
    report = sqlite_env.probe_sqlite_environment()

    assert report.python_executable == sys.executable
    assert report.python_prefix == sys.prefix
    assert report.in_venv == (sys.prefix != sys.base_prefix)


def test_self_report_line_is_structured(sqlite_env, monkeypatch):
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_sqlite_vec", lambda: False)

    line = sqlite_env.format_self_report(sqlite_env.probe_sqlite_environment())

    assert line.startswith("[tmemory] sqlite env self-report:")
    for token in (
        "interpreter=",
        "prefix=",
        "venv=",
        "python=",
        "sqlite=",
        "load_extension=False",
        "fts5=True",
        "sqlite_vec_installed=False",
        "vec0=False",
        "reasons=['sqlite_vec_not_installed', 'load_extension_missing']",
    ):
        assert token in line, token


def test_log_self_report_caches_on_plugin_and_is_unconditional(plugin, caplog):
    """即使 enable_vector_search=False，启动自报也必须输出。"""
    import astrbot_plugin_tmemory.core.sqlite_env as se

    plugin._cfg.enable_vector_search = False
    plugin._sqlite_env = None

    with caplog.at_level("INFO", logger="astrbot"):
        report = se.log_sqlite_env_self_report(plugin)

    assert plugin._sqlite_env is report
    assert any("sqlite env self-report" in r.getMessage() for r in caplog.records)


# ── shim：切换与幂等 ────────────────────────────────────────────────────────


def test_install_shim_switches_to_pysqlite3_and_is_idempotent(
    sqlite_env, monkeypatch
):
    fake_pysqlite3 = types.ModuleType("pysqlite3")
    fake_pysqlite3.sqlite_version = "3.99.0"
    fake_pysqlite3.Connection = type(
        "Connection", (), {"load_extension": lambda self, *a, **k: None}
    )
    monkeypatch.setitem(sys.modules, "pysqlite3", fake_pysqlite3)
    monkeypatch.setitem(sys.modules, "sqlite3", sys.modules["sqlite3"])
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: True)
    monkeypatch.setattr(sqlite_env, "_detect_pysqlite3", lambda: (True, "0.0"))

    first = sqlite_env.install_sqlite3_shim()
    assert first.backend == "pysqlite3"
    assert sys.modules["sqlite3"] is fake_pysqlite3

    second = sqlite_env.install_sqlite3_shim()
    assert second.backend == "pysqlite3"
    assert sys.modules["sqlite3"] is fake_pysqlite3


def test_install_shim_noop_when_pysqlite3_absent(sqlite_env, monkeypatch):
    real_sqlite3 = sys.modules["sqlite3"]
    monkeypatch.setattr(sqlite_env, "_detect_load_extension", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_fts5", lambda mod: False)
    monkeypatch.setattr(sqlite_env, "_detect_pysqlite3", lambda: (False, ""))

    report = sqlite_env.install_sqlite3_shim()

    assert report.backend == "sqlite3"
    assert sys.modules["sqlite3"] is real_sqlite3
    assert "load_extension_missing" in report.reasons


# ── db.py 记录具体原因 ──────────────────────────────────────────────────────


def test_db_records_load_extension_missing(plugin_module):
    import sqlite3 as _sqlite3

    from astrbot_plugin_tmemory.core.db import DatabaseManager

    class _ErrorConnection:
        def enable_load_extension(self, _flag):
            raise _sqlite3.OperationalError("not authorized")

    dm = DatabaseManager(":memory:")
    dm.set_vec_extension(object())

    # 完全缺少 enable_load_extension 属性 → AttributeError 分支
    dm._load_vec_extension(types.SimpleNamespace())
    assert "load_extension_missing" in dm.vec_load_reasons

    # enable_load_extension 抛 sqlite3.Error → 同样记录原因
    dm.vec_load_reasons = set()
    dm._load_vec_extension(_ErrorConnection())
    assert "load_extension_missing" in dm.vec_load_reasons


# ── helpers 接线：报告落盘到 plugin._sqlite_env ─────────────────────────────


def test_load_sqlite_vec_records_report_when_vec_missing(plugin, monkeypatch):
    import astrbot_plugin_tmemory.core.sqlite_env as se

    fake = se.SqliteEnvReport(
        backend="sqlite3",
        python_version="3.12.0",
        sqlite_version="3.40.0",
        has_load_extension=False,
        has_fts5=False,
        pysqlite3_installed=False,
        pysqlite3_version="",
        sqlite_vec_installed=False,
        vec0_available=False,
        reasons=("sqlite_vec_not_installed", "load_extension_missing"),
        hint="h",
    )
    monkeypatch.setattr(se, "probe_sqlite_environment", lambda: fake)
    plugin._cfg.enable_vector_search = True

    plugin._load_sqlite_vec()

    assert plugin._sqlite_env is fake
    assert plugin._vec_available is False


# ── /capabilities 新字段契约 ────────────────────────────────────────────────


def test_bridge_capabilities_exposes_sqlite_fields(bridge_module, plugin):
    bridge = bridge_module.PluginPagesBridge(plugin)
    payload, status = run(bridge.capabilities(FakeRequest()))

    assert status == 200
    caps = payload["capabilities"]
    for key in ("vector_search", "sqlite_vec", "fts5", "vector_index_rows"):
        assert key in caps
    assert isinstance(caps["vector_search"], bool)
    assert isinstance(caps["sqlite_vec"], bool)
    assert isinstance(caps["fts5"], bool)
    # vec 不可用时行数为 null（契约：明确「无数据」而非 0）
    assert caps["vector_index_rows"] is None

    env = payload["sqlite_env"]
    assert set(env) == {
        "backend",
        "python_version",
        "sqlite_version",
        "has_load_extension",
        "has_fts5",
        "pysqlite3_installed",
        "pysqlite3_version",
        "sqlite_vec_installed",
        "vec0_available",
        "reasons",
        "hint",
        "python_executable",
        "python_prefix",
        "in_venv",
        "platform_machine",
        "pysqlite3_wheel_available",
    }
    assert env["backend"] in {"sqlite3", "pysqlite3"}
    # 契约 §7：面板可一眼确认 AstrBot 用的是哪个解释器。
    assert env["python_executable"] == sys.executable
    assert isinstance(env["in_venv"], bool)

    # 既有字段不被破坏
    assert "warnings" in payload
    assert payload["runtime"]["extra_user_temp_fallback_count"] == 0
    assert payload["runtime"]["persona_private_fallback_count"] == 0
    assert payload["runtime"]["last_dim_change"] is None


def test_bridge_capabilities_surfaces_last_dim_change(bridge_module, plugin):
    expected = {
        "changed": True,
        "old_dim": 768,
        "new_dim": 1024,
        "skipped_reason": "vec0_unavailable",
        "rebuilt": False,
    }
    plugin._last_dim_change_result = dict(expected)

    bridge = bridge_module.PluginPagesBridge(plugin)
    payload, _ = run(bridge.capabilities(FakeRequest()))

    assert payload["runtime"]["last_dim_change"] == expected


def test_bridge_and_legacy_share_sqlite_capability_helper():
    """bridge 与 legacy 必须走同一 helper，避免两套 payload 漂移。"""
    for rel in ("web/bridge.py", "web_handlers.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "core.sqlite_env" in source
        assert "collect_sqlite_capabilities" in source
        assert "sqlite_env_dict" in source
