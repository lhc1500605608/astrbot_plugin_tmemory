"""LongMemEval 类基准的可复现性回归（Plan TMEAAA-379 B6）。

直接加载 ``eval/longmemeval_bench.py`` 跑样本，锁定离线基准可运行且
达到基线阈值；防止检索路径回归导致基准失真。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_bench():
    spec = importlib.util.spec_from_file_location(
        "longmemeval_bench", ROOT / "eval" / "longmemeval_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_longmemeval_bench_is_reproducible(plugin_module):
    bench = _load_bench()
    summary = bench.run_bench(ROOT / "eval" / "longmemeval_samples.json")
    agg = summary["aggregate"]

    assert agg["questions"] >= 5
    assert agg["accuracy"] >= 0.6
    assert agg["abstention_accuracy"] >= 0.6

    report = bench.render_report(summary)
    assert "LongMemEval-lite" in report
    assert "Per-category" in report
