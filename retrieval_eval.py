import json
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any, Dict, List


def _load_hybrid_memory_system():
    try:
        from .hybrid_search import HybridMemorySystem

        return HybridMemorySystem
    except ImportError:
        if "astrbot_plugin_tmemory.hybrid_search" in sys.modules:
            return sys.modules["astrbot_plugin_tmemory.hybrid_search"].HybridMemorySystem
        if "hybrid_search" in sys.modules:
            return sys.modules["hybrid_search"].HybridMemorySystem
        module_path = Path(__file__).with_name("hybrid_search.py")
        spec = importlib.util.spec_from_file_location("tmemory_hybrid_search", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        return module.HybridMemorySystem


DEFAULT_LIMITATIONS = [
    "样本量很小，只适合作为回归对照，不代表真实线上流量。",
    "当前样本偏中文短句和单跳查询，不能说明复杂多意图查询的效果。",
    "第一版基线不依赖外部 embedding 服务，因此不能代表真实向量召回质量。",
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


def load_eval_payload(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_eval_cases(path: Path) -> List[Dict[str, Any]]:
    payload = load_eval_payload(path)
    cases = payload.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError("cases must be a list")
    return cases


def materialize_eval_cases(
    conn: sqlite3.Connection,
    canonical_user_id: str,
    cases: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    materialized = []
    for case in cases:
        memory = str(case.get("memory", "")).strip()
        row = conn.execute(
            "SELECT id FROM memories WHERE canonical_user_id=? AND memory=? AND is_active=1 LIMIT 1",
            (canonical_user_id, memory),
        ).fetchone()
        if row is None:
            raise ValueError(f"memory not found for eval case: {memory}")
        materialized.append(
            {
                "query": str(case.get("query", "")),
                "expected_memory_ids": [int(row["id"])],
                "notes": str(case.get("notes", "")),
                "memory": memory,
                "memory_type": str(case.get("memory_type", "")),
            }
        )
    return materialized


def evaluate_recall_at_k(
    conn: sqlite3.Connection,
    canonical_user_id: str,
    cases: List[Dict[str, Any]],
    top_k: int,
    sample_name: str = "ad-hoc",
    limitations: List[str] | None = None,
) -> Dict[str, Any]:
    HybridMemorySystem = _load_hybrid_memory_system()
    hybrid_system = HybridMemorySystem(conn, vector_dim=0)
    results: List[Dict[str, Any]] = []
    hit_count = 0

    for case in cases:
        query = str(case.get("query", ""))
        expected_ids = [int(item) for item in case.get("expected_memory_ids", [])]
        raw_results = hybrid_system.hybrid_search(
            query=query,
            query_vector=None,
            canonical_user_id=canonical_user_id,
            top_k=top_k,
        )
        retrieved_ids = [int(item["id"]) for item in raw_results]
        matched_expected_ids = [item for item in expected_ids if item in retrieved_ids]
        hit = bool(matched_expected_ids)
        if hit:
            hit_count += 1
        results.append(
            {
                "query": query,
                "expected_memory_ids": expected_ids,
                "retrieved_ids": retrieved_ids,
                "matched_expected_ids": matched_expected_ids,
                "hit": hit,
                "notes": str(case.get("notes", "")),
            }
        )

    case_count = len(cases)
    recall_at_k = float(hit_count / case_count) if case_count else 0.0
    return {
        "sample_name": sample_name,
        "top_k": top_k,
        "case_count": case_count,
        "hit_count": hit_count,
        "recall_at_k": recall_at_k,
        "results": results,
        "limitations": limitations or DEFAULT_LIMITATIONS,
    }


def render_markdown_report(summary: Dict[str, Any]) -> str:
    top_k = int(summary["top_k"])
    lines = [
        "# Retrieval Offline Eval Baseline",
        "",
        f"- Sample: `{summary['sample_name']}`",
        f"- Recall@{top_k}: `{summary['recall_at_k']:.3f}` ({summary['hit_count']}/{summary['case_count']})",
        "",
        "## Cases",
        "",
        "| Query | Expected IDs | Retrieved IDs | Matched IDs | Hit | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary["results"]:
        lines.append(
            "| {query} | {expected} | {retrieved} | {matched} | {hit} | {notes} |".format(
                query=item["query"],
                expected=", ".join(str(v) for v in item["expected_memory_ids"]),
                retrieved=", ".join(str(v) for v in item["retrieved_ids"]),
                matched=", ".join(str(v) for v in item["matched_expected_ids"]),
                hit="yes" if item["hit"] else "no",
                notes=item["notes"],
            )
        )
    lines.extend([
        "",
        "## 局限性",
        "",
    ])
    for item in summary.get("limitations", DEFAULT_LIMITATIONS):
        lines.append(f"- {item}")
    return "\n".join(lines)


def run_baseline(sample_path: Path, output_path: Path | None = None) -> Dict[str, Any]:
    payload = load_eval_payload(sample_path)
    db_path = Path(":memory:")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _install_astrbot_stubs()
        if "astrbot_plugin_tmemory" not in sys.modules:
            package = types.ModuleType("astrbot_plugin_tmemory")
            package.__path__ = [str(Path(__file__).resolve().parent)]
            sys.modules["astrbot_plugin_tmemory"] = package
        module_path = Path(__file__).with_name("main.py")
        spec = importlib.util.spec_from_file_location("astrbot_plugin_tmemory.main", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        sys.modules["astrbot_plugin_tmemory.main"] = module
        spec.loader.exec_module(module)
        plugin = module.TMemoryPlugin(context=None, config={})
        plugin._conn = conn
        plugin._init_db()
        plugin._migrate_schema()

        canonical_user_id = "offline-eval-user"
        for case in payload["cases"]:
            plugin._insert_memory(
                canonical_id=canonical_user_id,
                adapter="offline",
                adapter_user="offline-user",
                memory=str(case["memory"]),
                score=0.8,
                memory_type=str(case.get("memory_type", "fact")),
                importance=0.8,
                confidence=0.8,
            )

        cases = materialize_eval_cases(conn, canonical_user_id, payload["cases"])
        summary = evaluate_recall_at_k(
            conn=conn,
            canonical_user_id=canonical_user_id,
            cases=cases,
            top_k=int(payload.get("top_k", 3)),
            sample_name=str(payload.get("sample_name", sample_path.stem)),
            limitations=list(payload.get("limitations", DEFAULT_LIMITATIONS)),
        )
    finally:
        conn.close()

    report = render_markdown_report(summary)
    if output_path is not None:
        output_path.write_text(report, encoding="utf-8")
    return summary


if __name__ == "__main__":
    sample_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("eval/retrieval_samples.json")
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("eval/retrieval_baseline.md")
    summary = run_baseline(sample_path, output_path)
    print(render_markdown_report(summary))
