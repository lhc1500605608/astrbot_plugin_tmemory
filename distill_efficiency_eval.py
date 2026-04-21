import asyncio
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_LIMITATIONS = [
    "这是离线轻量基线，只验证当前门控与去重是否减少无效蒸馏，不代表真实线上吞吐或端到端时延。",
    "样本规模很小，场景由人工构造，主要用于优化前后回归对比，而不是得出普适性能结论。",
    "结果没有接入真实外部模型服务，因此不能说明不同模型、网络抖动或 provider 限速下的真实成本。",
]


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    provider_mod = types.ModuleType("astrbot.api.provider")
    star_mod = types.ModuleType("astrbot.api.star")

    class _DummyFilter:
        class EventMessageType:
            ALL = "all"

        class PermissionType:
            ADMIN = "admin"

        def __getattr__(self, _name):
            def decorator_factory(*_args, **_kwargs):
                def decorator(func):
                    return func

                return decorator

            return decorator_factory

    class AstrMessageEvent:
        pass

    class LLMResponse:
        pass

    class ProviderRequest:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    import logging

    api_mod.logger = logging.getLogger("astrbot")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _DummyFilter()
    provider_mod.LLMResponse = LLMResponse
    provider_mod.ProviderRequest = ProviderRequest
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    astrbot_mod.api = api_mod
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.provider"] = provider_mod
    sys.modules["astrbot.api.star"] = star_mod


def _load_plugin_module():
    _install_astrbot_stubs()
    root = Path(__file__).resolve().parent
    if "astrbot_plugin_tmemory" not in sys.modules:
        package = types.ModuleType("astrbot_plugin_tmemory")
        package.__path__ = [str(root)]
        sys.modules["astrbot_plugin_tmemory"] = package

    hybrid_name = "astrbot_plugin_tmemory.hybrid_search"
    if hybrid_name not in sys.modules:
        hybrid_spec = importlib.util.spec_from_file_location(hybrid_name, root / "hybrid_search.py")
        hybrid_module = importlib.util.module_from_spec(hybrid_spec)
        assert hybrid_spec is not None and hybrid_spec.loader is not None
        sys.modules[hybrid_name] = hybrid_module
        hybrid_spec.loader.exec_module(hybrid_module)

    module_name = "astrbot_plugin_tmemory.main"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, root / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_eval_payload(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


async def _run_case(case: Dict[str, Any]) -> Dict[str, Any]:
    module = _load_plugin_module()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    plugin = module.TMemoryPlugin(context=None, config={})
    plugin._conn = conn
    plugin._init_db()
    plugin._migrate_schema()
    plugin.capture_dedup_window = int(case.get("capture_dedup_window", 0))
    plugin.capture_min_content_len = int(case.get("capture_min_content_len", 5))
    plugin.distill_min_batch_count = 1
    plugin.distill_batch_limit = 100
    plugin.distill_user_throttle_sec = 0

    llm_response = list(case.get("llm_response", []))
    retained_memories = [str(item.get("memory", "")) for item in llm_response if str(item.get("memory", ""))]

    async def fake_distill_rows_with_llm(rows):
        return llm_response, -1, -1

    plugin._distill_rows_with_llm = fake_distill_rows_with_llm

    canonical_id = str(case.get("scenario", "baseline-user"))
    for item in case.get("messages", []):
        plugin._insert_conversation(
            canonical_id=canonical_id,
            role=str(item.get("role", "user")),
            content=str(item.get("content", "")),
            source_adapter="offline",
            source_user_id="offline-user",
            unified_msg_origin="",
        )

    pending_rows = plugin._fetch_pending_rows(canonical_id, 100)
    filtered_rows = plugin._prefilter_distill_rows(pending_rows)
    skipped_before = plugin._distill_skipped_rows
    history_before = len(plugin._get_distill_history(limit=20))

    processed_users, memories_created = await plugin._run_distill_cycle(
        force=True,
        trigger="distill_efficiency_baseline",
    )

    skipped_after = plugin._distill_skipped_rows
    history_after = plugin._get_distill_history(limit=20)
    conn.close()

    input_rows = len(case.get("messages", []))
    inserted_rows = len(pending_rows)
    deduped_rows = max(0, input_rows - inserted_rows)
    llm_rows = len(pending_rows)
    skipped_rows = skipped_after - skipped_before
    distill_runs = len(history_after) - history_before

    return {
        "scenario": str(case.get("scenario", "unknown")),
        "description": str(case.get("description", "")),
        "input_rows": input_rows,
        "inserted_rows": inserted_rows,
        "deduped_rows": deduped_rows,
        "prefiltered_rows": len(pending_rows) - len(filtered_rows),
        "skipped_rows": skipped_rows,
        "llm_rows": llm_rows,
        "distill_runs": distill_runs,
        "processed_users": processed_users,
        "memories_created": memories_created,
        "retained_memories": retained_memories,
    }


def _summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_input_rows = sum(int(item["input_rows"]) for item in results)
    total_inserted_rows = sum(int(item["inserted_rows"]) for item in results)
    total_skipped_rows = sum(int(item["skipped_rows"]) for item in results)
    total_distill_runs = sum(int(item["distill_runs"]) for item in results)
    total_memories_created = sum(int(item["memories_created"]) for item in results)
    base = float(total_input_rows) if total_input_rows else 1.0
    ratio = round(total_skipped_rows / base, 3)
    return {
        "total_input_rows": total_input_rows,
        "total_inserted_rows": total_inserted_rows,
        "total_skipped_rows": total_skipped_rows,
        "total_distill_runs": total_distill_runs,
        "total_memories_created": total_memories_created,
        "skip_ratio": ratio,
        "distill_reduction_ratio": ratio,
    }


def render_markdown_report(summary: Dict[str, Any]) -> str:
    aggregate = summary["aggregate"]
    lines = [
        "# Distill Efficiency Baseline",
        "",
        f"- Sample: `{summary['sample_name']}`",
        f"- Scenarios: `{summary['scenario_count']}`",
        f"- total_input_rows: `{aggregate['total_input_rows']}`",
        f"- total_inserted_rows: `{aggregate['total_inserted_rows']}`",
        f"- total_skipped_rows: `{aggregate['total_skipped_rows']}`",
        f"- total_distill_runs: `{aggregate['total_distill_runs']}`",
        f"- total_memories_created: `{aggregate['total_memories_created']}`",
        f"- skip_ratio: `{aggregate['skip_ratio']:.3f}`",
        f"- distill_reduction_ratio: `{aggregate['distill_reduction_ratio']:.3f}`",
        "",
        "## Scenarios",
        "",
        "| Scenario | Input Rows | Inserted Rows | Deduped Rows | Prefiltered Rows | Skipped Rows | LLM Rows | Distill Runs | Memories Created | Description |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary["results"]:
        lines.append(
            "| {scenario} | {input_rows} | {inserted_rows} | {deduped_rows} | {prefiltered_rows} | {skipped_rows} | {llm_rows} | {distill_runs} | {memories_created} | {description} |".format(
                **item,
            )
        )
        if item.get("retained_memories"):
            lines.append(
                "| retained_memories | - | - | - | - | - | - | - | - | "
                + "; ".join(str(memory) for memory in item["retained_memories"])
                + " |"
            )
    lines.extend(["", "## 局限性", ""])
    for item in summary.get("limitations", DEFAULT_LIMITATIONS):
        lines.append(f"- {item}")
    return "\n".join(lines)


def run_baseline(sample_path: Path, output_path: Path | None = None) -> Dict[str, Any]:
    payload = load_eval_payload(sample_path)
    results = [asyncio.run(_run_case(case)) for case in payload.get("cases", [])]
    summary = {
        "sample_name": str(payload.get("sample_name", sample_path.stem)),
        "scenario_count": len(results),
        "aggregate": _summarize(results),
        "results": results,
        "limitations": list(payload.get("limitations", DEFAULT_LIMITATIONS)),
    }
    report = render_markdown_report(summary)
    if output_path is not None:
        output_path.write_text(report, encoding="utf-8")
    return summary


if __name__ == "__main__":
    sample_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("eval/distill_efficiency_samples.json")
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("eval/distill_efficiency_baseline.md")
    summary = run_baseline(sample_path, output_path)
    print(render_markdown_report(summary))
