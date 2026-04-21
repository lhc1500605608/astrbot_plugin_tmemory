import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES_PATH = ROOT / "eval" / "retrieval_samples.json"


def _load_eval_module():
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_tmemory.retrieval_eval", ROOT / "retrieval_eval.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_load_eval_cases_reads_expected_schema():
    retrieval_eval = _load_eval_module()

    cases = retrieval_eval.load_eval_cases(SAMPLES_PATH)

    assert len(cases) >= 4
    assert cases[0]["query"]
    assert cases[0]["memory"]
    assert cases[0]["memory_type"]


def test_evaluate_recall_reports_expected_hits(plugin):
    retrieval_eval = _load_eval_module()

    first_id = plugin._insert_memory(
        canonical_id="user-eval",
        adapter="qq",
        adapter_user="42",
        memory="用户喜欢黑咖啡，不加糖。",
        score=0.8,
        memory_type="preference",
        importance=0.9,
        confidence=0.9,
    )
    second_id = plugin._insert_memory(
        canonical_id="user-eval",
        adapter="qq",
        adapter_user="42",
        memory="用户周三晚上固定去游泳训练。",
        score=0.7,
        memory_type="fact",
        importance=0.7,
        confidence=0.8,
    )
    third_id = plugin._insert_memory(
        canonical_id="user-eval",
        adapter="qq",
        adapter_user="42",
        memory="开会时希望助手先给结论，再展开细节。",
        score=0.6,
        memory_type="style",
        importance=0.8,
        confidence=0.8,
    )

    raw_cases = [
        {
            "query": "黑 咖啡",
            "memory": "用户喜欢黑咖啡，不加糖。",
            "memory_type": "preference",
            "notes": "偏好召回",
        },
        {
            "query": "周三 游泳",
            "memory": "用户周三晚上固定去游泳训练。",
            "memory_type": "fact",
            "notes": "事实召回",
        },
        {
            "query": "结论 细节",
            "memory": "开会时希望助手先给结论，再展开细节。",
            "memory_type": "style",
            "notes": "风格召回",
        },
    ]

    with plugin._db() as conn:
        cases = retrieval_eval.materialize_eval_cases(
            conn=conn,
            canonical_user_id="user-eval",
            cases=raw_cases,
        )
        summary = retrieval_eval.evaluate_recall_at_k(
            conn=conn,
            canonical_user_id="user-eval",
            cases=cases,
            top_k=3,
        )

    assert summary["case_count"] == 3
    assert summary["hit_count"] == 3
    assert summary["recall_at_k"] == 1.0
    assert summary["results"][0]["retrieved_ids"][0] == first_id
    assert summary["results"][1]["retrieved_ids"][0] == second_id
    assert summary["results"][2]["matched_expected_ids"] == [third_id]


def test_render_markdown_report_includes_limitations_section():
    retrieval_eval = _load_eval_module()
    summary = {
        "sample_name": "fts-baseline",
        "top_k": 3,
        "case_count": 4,
        "hit_count": 3,
        "recall_at_k": 0.75,
        "results": [
            {
                "query": "q1",
                "expected_memory_ids": [1],
                "retrieved_ids": [1, 2],
                "matched_expected_ids": [1],
                "hit": True,
                "notes": "ok",
            }
        ],
    }

    report = retrieval_eval.render_markdown_report(summary)

    assert "Recall@3" in report
    assert "局限性" in report
    assert "q1" in report
    assert "0.750" in report


def test_search_fts_supports_multi_token_queries(plugin_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin = plugin_module.TMemoryPlugin(context=None, config={})
    plugin._init_db()
    plugin._migrate_schema()
    plugin._insert_memory(
        canonical_id="user-eval",
        adapter="qq",
        adapter_user="42",
        memory="用户喜欢黑咖啡，不加糖。",
        score=0.8,
        memory_type="preference",
        importance=0.9,
        confidence=0.9,
    )

    retrieval_eval = _load_eval_module()
    HybridMemorySystem = retrieval_eval._load_hybrid_memory_system()

    with plugin._db() as conn:
        fts_results = HybridMemorySystem(conn, 0).fts_db.search_fts("黑 咖啡", "user-eval", limit=3)

    assert [row["id"] for row in fts_results] == [1]
    plugin._close_db()


def test_sample_file_uses_human_maintainable_json():
    payload = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))

    assert payload["sample_name"] == "fts-baseline"
    assert payload["top_k"] == 3
    assert len(payload["cases"]) >= 4
