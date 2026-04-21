import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES_PATH = ROOT / "eval" / "distill_efficiency_samples.json"


def _load_eval_module():
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_tmemory.distill_efficiency_eval",
        ROOT / "distill_efficiency_eval.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sample_file_covers_three_distill_efficiency_scenarios():
    payload = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))

    assert payload["sample_name"] == "distill-efficiency-baseline"
    assert {case["scenario"] for case in payload["cases"]} == {
        "high_frequency_short_messages",
        "hot_user_burst_then_high_value_fact",
        "long_session_mixed_signal_preserves_key_memories",
        "repeated_low_value_messages",
        "normal_effective_dialogue",
    }
    assert len(payload["cases"]) == 5


def test_run_baseline_reports_skip_and_distill_metrics():
    distill_efficiency_eval = _load_eval_module()

    summary = distill_efficiency_eval.run_baseline(SAMPLES_PATH)

    assert summary["sample_name"] == "distill-efficiency-baseline"
    assert summary["scenario_count"] == 5
    assert summary["aggregate"]["total_input_rows"] == 41
    assert summary["aggregate"]["total_inserted_rows"] == 35
    assert summary["aggregate"]["total_skipped_rows"] == 20
    assert summary["aggregate"]["total_distill_runs"] == 5
    assert summary["aggregate"]["total_memories_created"] == 5
    assert summary["aggregate"]["distill_reduction_ratio"] == 0.488
    assert summary["aggregate"]["skip_ratio"] == 0.488

    cases = {item["scenario"]: item for item in summary["results"]}
    assert cases["high_frequency_short_messages"]["memories_created"] == 0
    assert cases["high_frequency_short_messages"]["skipped_rows"] == 6
    assert cases["hot_user_burst_then_high_value_fact"]["skipped_rows"] == 4
    assert cases["hot_user_burst_then_high_value_fact"]["retained_memories"] == [
        "用户下周开始需要控制糖分摄入，早餐改成无糖酸奶。"
    ]
    assert cases["long_session_mixed_signal_preserves_key_memories"]["inserted_rows"] == 14
    assert cases["long_session_mixed_signal_preserves_key_memories"]["deduped_rows"] == 0
    assert cases["long_session_mixed_signal_preserves_key_memories"]["skipped_rows"] == 6
    assert cases["long_session_mixed_signal_preserves_key_memories"]["retained_memories"] == [
        "用户每周二晚上会和产品团队做复盘。",
        "用户希望答复先列风险，再给执行建议。",
    ]
    assert cases["repeated_low_value_messages"]["inserted_rows"] == 2
    assert cases["repeated_low_value_messages"]["deduped_rows"] == 4
    assert cases["repeated_low_value_messages"]["skipped_rows"] == 2
    assert cases["normal_effective_dialogue"]["memories_created"] == 2
    assert cases["normal_effective_dialogue"]["llm_rows"] == 6


def test_render_markdown_report_includes_efficiency_limitations():
    distill_efficiency_eval = _load_eval_module()
    summary = {
        "sample_name": "distill-efficiency-baseline",
        "scenario_count": 1,
        "aggregate": {
            "total_input_rows": 6,
            "total_inserted_rows": 4,
            "total_skipped_rows": 2,
            "total_distill_runs": 1,
            "total_memories_created": 1,
            "skip_ratio": 0.333,
            "distill_reduction_ratio": 0.5,
        },
        "results": [
            {
                "scenario": "normal_effective_dialogue",
                "description": "正常有效对话",
                "input_rows": 6,
                "inserted_rows": 4,
                "deduped_rows": 2,
                "prefiltered_rows": 1,
                "skipped_rows": 1,
                "llm_rows": 3,
                "distill_runs": 1,
                "memories_created": 1,
            }
        ],
        "limitations": ["只验证离线基线，不代表线上吞吐。"],
    }

    report = distill_efficiency_eval.render_markdown_report(summary)

    assert "Distill Efficiency Baseline" in report
    assert "skip_ratio" in report
    assert "normal_effective_dialogue" in report
    assert "局限性" in report
