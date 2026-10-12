"""Small offline retrieval smoke benchmark, not the official LongMemEval dataset."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def run_bench(samples_path):
    from astrbot_plugin_tmemory.main import TMemoryPlugin
    from astrbot_plugin_tmemory.core.profile_ops import ProfileItemOps
    samples = json.loads(Path(samples_path).read_text(encoding="utf-8"))
    results = []
    original = Path.cwd()
    with TemporaryDirectory() as temp:
        try:
            os.chdir(temp)
            plugin = TMemoryPlugin(context=None, config={})
            plugin._init_db()
            for sample in samples:
                uid = sample["user"]
                for text in sample.get("facts", []):
                    ProfileItemOps(plugin).upsert_profile_item(uid, "fact", "", text, .9, .8)
                hits = plugin._retrieval_mgr.retrieve_profile_items(
                    sample.get("query_user", uid), sample["query"], 3,
                )
                expected = sample.get("expected")
                ok = not hits if expected is None else any(expected in h["content"] for h in hits)
                results.append({"category": sample["category"], "correct": ok, "abstention": expected is None})
            plugin._close_db()
        finally:
            os.chdir(original)
    abstentions = [r for r in results if r["abstention"]]
    return {"aggregate": {
        "questions": len(results),
        "accuracy": sum(r["correct"] for r in results) / len(results),
        "abstention_accuracy": sum(r["correct"] for r in abstentions) / max(1, len(abstentions)),
    }, "results": results}


def render_report(summary):
    lines = ["# LongMemEval-lite", "Local smoke samples; not official benchmark scores.", "## Per-category"]
    lines.extend(f"- {r['category']}: {'pass' if r['correct'] else 'fail'}" for r in summary["results"])
    return "\n".join(lines)
