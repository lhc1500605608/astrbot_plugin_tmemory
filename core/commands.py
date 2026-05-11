"""CommandHandlersMixin — /tm_* slash command handlers.

Extracted from handlers.py to keep each module under 500 lines (ADR-009 / TMEAAA-350).
Methods use ``self._normalize_text()``, ``self._db()``, ``self._cfg`` etc. from TMemoryPlugin.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class CommandHandlersMixin:
    """Slash-command handlers for /tm_memory, /tm_context, /tm_stats, etc."""

    async def _handle_tm_distill_now(self, event: AstrMessageEvent):
        processed_users, total_memories, errors = await self._run_distill_cycle(
            force=True, trigger="manual_cmd"
        )
        yield event.plain_result(
            f"\u6279\u91cf\u84b8\u998f\u5b8c\u6210:\u5904\u7406\u7528\u6237 {processed_users} \u4e2a\uff0c\u65b0\u589e/\u66f4\u65b0\u8bb0\u5fc6 {total_memories} \u6761\u3002"
        )

    async def _handle_tm_worker(self, event: AstrMessageEvent):
        pending_users = self._count_pending_users()
        pending_rows = self._count_pending_rows()
        lines = [
            f"worker_running={self._worker_running}",
            f"distill_interval_sec={self._cfg.distill_interval_sec}",
            f"distill_min_batch_count={self._cfg.distill_min_batch_count}",
            f"distill_batch_limit={self._cfg.distill_batch_limit}",
            "--- \u8bb0\u5fc6\u84b8\u998f (memory distill) ---",
            f"enable_auto_capture={self._cfg.enable_auto_capture}",
            f"distill_pause={self._cfg.distill_pause}",
            f"memory_mode={self._cfg.memory_mode}",
            f"pending_users={pending_users}",
            f"pending_rows={pending_rows}",
            "--- gate stats ---",
            f"capture_min_content_len={self._cfg.capture_min_content_len}",
            f"capture_dedup_window={self._cfg.capture_dedup_window}",
            f"distill_user_throttle_sec={self._cfg.distill_user_throttle_sec}",
            f"distill_skipped_rows(lifetime)={self._distill_skipped_rows}",
            f"throttled_users={sum(1 for ts in self._user_last_distilled_ts.values() if time.time() - ts < self._cfg.distill_user_throttle_sec) if self._cfg.distill_user_throttle_sec > 0 else 'N/A'}",
        ]
        yield event.plain_result("\n".join(lines))

    async def _handle_tm_memory(self, event: AstrMessageEvent):
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        memories = self._list_memories(canonical_id, limit=8)
        if not memories:
            yield event.plain_result("\u5f53\u524d\u8fd8\u6ca1\u6709\u5df2\u4fdd\u5b58\u8bb0\u5fc6\u3002")
            return

        lines = [f"canonical_id={canonical_id}"]
        for row in memories:
            pin = "\U0001f4cc " if row.get("is_pinned") else ""
            attn = row.get("attention_score", 0.0)
            lines.append(
                f"[{row['id']}] {pin}[{row['memory_type']}] s={row['score']:.2f} i={row['importance']:.2f} c={row['confidence']:.2f} r={row['reinforce_count']} a={attn:.2f} | {row['memory']}"
            )
        yield event.plain_result("\n".join(lines))

    async def _handle_tm_context(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        query = re.sub(r"^/tm_context\s*", "", raw, flags=re.IGNORECASE).strip()
        if not query:
            yield event.plain_result("\u7528\u6cd5: /tm_context <\u5f53\u524d\u95ee\u9898>")
            return

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        context_block = await self.build_memory_context(canonical_id, query, limit=6)
        yield event.plain_result(context_block)

    async def _handle_tm_bind(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        canonical_id = re.sub(r"^/tm_bind\s*", "", raw, flags=re.IGNORECASE).strip()
        if not canonical_id:
            yield event.plain_result("\u7528\u6cd5: /tm_bind <\u7edf\u4e00\u7528\u6237ID>")
            return

        adapter = self._get_adapter_name(event)
        adapter_user = self._get_adapter_user_id(event)
        self._identity_mgr.bind_identity(adapter, adapter_user, canonical_id)
        yield event.plain_result(f"\u7ed1\u5b9a\u6210\u529f:{adapter}:{adapter_user} -> {canonical_id}")

    async def _handle_tm_merge(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        args = re.sub(r"^/tm_merge\s*", "", raw, flags=re.IGNORECASE).strip().split()
        if len(args) != 2:
            yield event.plain_result(
                "\u7528\u6cd5: /tm_merge <from_canonical_id> <to_canonical_id>"
            )
            return

        from_id, to_id = args[0], args[1]
        if from_id == to_id:
            yield event.plain_result("\u4e24\u4e2a ID \u76f8\u540c\uff0c\u65e0\u9700\u5408\u5e76\u3002")
            return

        moved = self._identity_mgr.merge_identity(from_id, to_id)
        self._delete_vectors_for_user(from_id)
        self._merge_needs_vector_rebuild = True
        yield event.plain_result(
            f"\u5408\u5e76\u5b8c\u6210:{from_id} -> {to_id}\uff0c\u8fc1\u79fb\u8bb0\u5fc6 {moved} \u6761\u3002"
        )

    async def _handle_tm_forget(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_forget\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("\u7528\u6cd5: /tm_forget <\u8bb0\u5fc6ID>")
            return

        deleted = self._delete_memory(int(arg))
        if deleted:
            yield event.plain_result(f"\u5df2\u5220\u9664\u8bb0\u5fc6 {arg}")
            return
        yield event.plain_result(f"\u672a\u627e\u5230\u8bb0\u5fc6 {arg}")

    async def _handle_tm_stats(self, event: AstrMessageEvent):
        stats = self._get_global_stats()
        lines = [
            f"total_users: {stats['total_users']}",
            f"total_active_memories: {stats['total_active_memories']}",
            f"total_deactivated_memories: {stats['total_deactivated_memories']}",
            f"pending_cached_rows: {stats['pending_cached_rows']}",
            f"total_events: {stats['total_events']}",
        ]
        if self._vec_available:
            lines.append(f"vector_index_rows: {stats.get('vector_index_rows', 0)}")
            lines.append(
                f"embed_ok/fail: {self._embed_ok_count}/{self._embed_fail_count}"
            )
            hit_rate = (
                f"{self._vec_hit_count}/{self._vec_query_count}"
                f" ({self._vec_hit_count * 100 // max(1, self._vec_query_count)}%)"
                if self._vec_query_count > 0
                else "N/A"
            )
            lines.append(f"vector_hit_rate: {hit_rate}")
            embed_cache_total = self._embed_cache_hit_count + self._embed_cache_miss_count
            embed_cache_pct = (
                self._embed_cache_hit_count * 100 // max(1, embed_cache_total)
                if embed_cache_total > 0
                else 0
            )
            lines.append(
                f"embed_cache: {self._embed_cache_hit_count}h "
                f"/ {embed_cache_total}t ({embed_cache_pct}%)"
            )
            if self._embed_last_error:
                lines.append(f"embed_last_error: {self._embed_last_error[:80]}")
        elif self._cfg.enable_vector_search:
            lines.append("vector_search: enabled but sqlite-vec not installed")

        distill_cost = self._get_distill_cost_summary(last_n=10)
        lines.append("--- distill cost (last 10 runs) ---")
        if distill_cost["has_usage"]:
            lines.append(f"distill_runs: {distill_cost['runs']}")
            lines.append(f"distill_tokens_input: {distill_cost['tokens_input']}")
            lines.append(f"distill_tokens_output: {distill_cost['tokens_output']}")
            lines.append(f"distill_tokens_total: {distill_cost['tokens_total']}")
        else:
            lines.append(
                f"distill_runs: {distill_cost['runs']} (no usage data from provider)"
            )

        yield event.plain_result("\n".join(lines))

    async def _handle_tm_distill_history(self, event: AstrMessageEvent):
        rows = self._get_distill_history(limit=10)
        if not rows:
            yield event.plain_result("\u6682\u65e0\u84b8\u998f\u5386\u53f2\u8bb0\u5f55\u3002")
            return

        from .distill_validator import get_budget_consumption_pct, get_daily_token_usage

        budget = max(0, getattr(self._cfg, "daily_token_budget", 0))
        used = get_daily_token_usage(self)
        pct = get_budget_consumption_pct(self)

        lines = [f"\u6700\u8fd1 {len(rows)} \u8f6e\u84b8\u998f\u5386\u53f2\uff08\u6700\u65b0\u4f18\u5148\uff09:"]
        if budget > 0:
            lines.append(
                f"--- \u65e5\u9884\u7b97: {budget} token, \u5df2\u7528: {used} ({pct}%) ---"
            )
        else:
            lines.append(f"--- \u65e5\u9884\u7b97: \u65e0\u9650\u5236, \u4eca\u65e5\u5df2\u7528: {used} token ---")

        for r in rows:
            tok_in = r.get("tokens_input", -1)
            tok_out = r.get("tokens_output", -1)
            tok_total = r.get("tokens_total", -1)
            tok_str = (
                f"in={tok_in} out={tok_out} total={tok_total}"
                if tok_total >= 0
                else "tokens=N/A"
            )
            lines.append(
                f"[{r['id']}] {r['started_at'][:16]} trigger={r['trigger_type']}"
                f" users={r['users_processed']} mems={r['memories_created']}"
                f" failed={r['users_failed']} dur={r['duration_sec']:.1f}s"
                f" {tok_str}"
            )
        yield event.plain_result("\n".join(lines))

    async def _handle_tm_purify(self, event: AstrMessageEvent):
        yield event.plain_result("\u5f00\u59cb\u8bb0\u5fc6\u63d0\u7eaf\uff0c\u8bf7\u7a0d\u5019\u2026")
        pruned, kept = await self._run_memory_purify()
        yield event.plain_result(
            f"\u8bb0\u5fc6\u63d0\u7eaf\u5b8c\u6210:\u5931\u6d3b\u4f4e\u8d28\u91cf\u8bb0\u5fc6 {pruned} \u6761\uff0c\u4fdd\u7559 {kept} \u6761\u3002"
        )

    async def _handle_tm_quality_refine(self, event: AstrMessageEvent):
        async for msg in self._handle_tm_purify(event):
            yield msg

    async def _handle_tm_vec_rebuild(self, event: AstrMessageEvent):
        if not self._vec_available:
            yield event.plain_result(
                "\u5411\u91cf\u68c0\u7d22\u672a\u542f\u7528\u6216 sqlite-vec \u672a\u5b89\u88c5\u3002\n"
                "\u8bf7\u5148\u5b89\u88c5:pip install sqlite-vec\uff0c\u5e76\u5728\u914d\u7f6e\u4e2d\u5f00\u542f enable_vector_search\u3002"
            )
            return
        if not self._cfg.embed_provider_id:
            yield event.plain_result("\u672a\u914d\u7f6e embed_provider_id\uff0c\u65e0\u6cd5\u751f\u6210\u5411\u91cf\u3002")
            return

        raw = (event.message_str or "").strip()
        force = "force=true" in raw.lower() or "force" in raw.lower()

        if force:
            yield event.plain_result("\u5168\u91cf\u91cd\u5efa\u6a21\u5f0f:\u6e05\u7a7a\u73b0\u6709\u5411\u91cf\u540e\u91cd\u5efa\uff0c\u8bf7\u7a0d\u5019...")
            with self._db() as conn:
                try:
                    conn.execute("DELETE FROM memory_vectors")
                except Exception:
                    pass
        else:
            yield event.plain_result("\u589e\u91cf\u8865\u5168\u6a21\u5f0f:\u53ea\u8865\u7f3a\u5931\u5411\u91cf\uff0c\u8bf7\u7a0d\u5019...")

        ok, fail = await self._rebuild_vector_index()
        yield event.plain_result(
            f"\u5411\u91cf\u7d22\u5f15\u91cd\u5efa\u5b8c\u6210:\u6210\u529f {ok} \u6761\uff0c\u8df3\u8fc7/\u5931\u8d25 {fail} \u6761\u3002"
        )

    async def _handle_tm_refine(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_refine\s*", "", raw, flags=re.IGNORECASE).strip()

        opts = {
            "mode": self._cfg.manual_purify_default_mode,
            "limit": str(self._cfg.manual_purify_default_limit),
            "dry_run": "false",
            "include_pinned": "false",
        }
        for m in re.finditer(
            r"(mode|limit|dry_run|include_pinned)=([^\s]+)",
            body,
            flags=re.IGNORECASE,
        ):
            opts[m.group(1).lower()] = m.group(2)
        extra = re.sub(
            r"(mode|limit|dry_run|include_pinned)=([^\s]+)",
            "",
            body,
            flags=re.IGNORECASE,
        ).strip()

        mode = str(opts["mode"]).lower()
        if mode not in {"merge", "split", "both"}:
            yield event.plain_result("mode \u4ec5\u652f\u6301 merge|split|both")
            return

        try:
            limit = max(1, min(200, int(opts["limit"])))
        except Exception:
            limit = 20
        dry_run = str(opts["dry_run"]).lower() in {"1", "true", "yes", "y", "on"}
        include_pinned = str(opts["include_pinned"]).lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        result = await self._manual_purify_memories(
            event=event,
            canonical_id=canonical_id,
            mode=mode,
            limit=limit,
            dry_run=dry_run,
            include_pinned=include_pinned,
            extra_instruction=extra,
        )

        yield event.plain_result(
            "\n".join(
                [
                    f"manual_purify done (dry_run={dry_run})",
                    f"user={canonical_id}",
                    f"mode={mode}, limit={limit}, include_pinned={include_pinned}",
                    f"updates={result['updates']}, adds={result['adds']}, deletes={result['deletes']}",
                    f"note={result.get('note', '')}",
                ]
            )
        )

    async def _handle_tm_mem_merge(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_mem_merge\s*", "", raw, flags=re.IGNORECASE).strip()
        if not body:
            yield event.plain_result(
                "\u7528\u6cd5: /tm_mem_merge <id1,id2,...> <\u5408\u5e76\u540e\u7684\u8bb0\u5fc6\u6587\u672c>"
            )
            return

        parts = body.split(None, 1)
        ids_part = parts[0]
        merged_text = parts[1].strip() if len(parts) > 1 else ""
        ids = [int(x) for x in re.split(r"[,,\uff0c]", ids_part) if x.strip().isdigit()]
        if len(ids) < 2:
            yield event.plain_result(
                "\u8bf7\u81f3\u5c11\u63d0\u4f9b\u4e24\u4e2a\u8bb0\u5fc6ID\uff0c\u4f8b\u5982 /tm_mem_merge 12,18 \u65b0\u8bb0\u5fc6\u5185\u5bb9"
            )
            return

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        rs = self._fetch_memories_by_ids(canonical_id, ids)
        if len(rs) < 2:
            yield event.plain_result("\u8fd9\u4e9bID\u4e2d\u53ef\u7528\u8bb0\u5fc6\u4e0d\u8db3\u4e24\u6761(\u53ef\u80fd\u4e0d\u5c5e\u4e8e\u5f53\u524d\u7528\u6237)")
            return

        if not merged_text:
            merged_text = self._auto_merge_memory_text([str(r["memory"]) for r in rs])

        keep_id = int(rs[0]["id"])
        self._update_memory_text(keep_id, merged_text)
        if self._vec_available:
            await self._upsert_vector(keep_id, merged_text)

        for r in rs[1:]:
            self._delete_memory(int(r["id"]))

        yield event.plain_result(f"\u5408\u5e76\u5b8c\u6210:\u4fdd\u7559 #{keep_id}\uff0c\u5220\u9664 {len(rs) - 1} \u6761")

    async def _handle_tm_mem_split(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        body = re.sub(r"^/tm_mem_split\s*", "", raw, flags=re.IGNORECASE).strip()
        if not body:
            yield event.plain_result("\u7528\u6cd5: /tm_mem_split <id> [\u7247\u6bb51|\u7247\u6bb52|...]")
            return

        parts = body.split(None, 1)
        if not parts[0].isdigit():
            yield event.plain_result("\u7b2c\u4e00\u4e2a\u53c2\u6570\u5fc5\u987b\u662f\u8bb0\u5fc6ID")
            return
        mem_id = int(parts[0])
        custom = parts[1].strip() if len(parts) > 1 else ""

        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        row = self._fetch_memory_by_id(canonical_id, mem_id)
        if not row:
            yield event.plain_result(f"\u672a\u627e\u5230\u8bb0\u5fc6 {mem_id}")
            return

        if custom:
            segments = [
                self._normalize_text(x)
                for x in custom.split("|")
                if self._normalize_text(x)
            ]
        else:
            segments = await self._llm_split_memory(event, str(row["memory"]))

        if len(segments) < 2:
            yield event.plain_result("\u62c6\u5206\u7ed3\u679c\u4e0d\u8db3\u4e24\u6bb5\uff0c\u672a\u6267\u884c\u5199\u5165")
            return

        self._update_memory_text(mem_id, segments[0])
        if self._vec_available:
            await self._upsert_vector(mem_id, segments[0])

        added = 0
        for seg in segments[1:]:
            new_id = self._insert_memory(
                canonical_id=canonical_id,
                adapter=str(row["source_adapter"]),
                adapter_user=str(row["source_user_id"]),
                memory=seg,
                score=float(row["score"]),
                memory_type=str(row["memory_type"]),
                importance=float(row["importance"]),
                confidence=float(row["confidence"]),
                source_channel="manual_split",
            )
            if self._vec_available and new_id:
                await self._upsert_vector(new_id, seg)
            added += 1

        yield event.plain_result(f"\u62c6\u5206\u5b8c\u6210:\u539f\u8bb0\u5fc6#{mem_id} + \u65b0\u589e {added} \u6761")

    async def _handle_tm_pin(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_pin\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("\u7528\u6cd5: /tm_pin <\u8bb0\u5fc6ID>")
            return
        ok = self._set_pinned(int(arg), True)
        yield event.plain_result(
            f"\u8bb0\u5fc6 {arg} \u5df2\u8bbe\u4e3a\u5e38\u9a7b" if ok else f"\u672a\u627e\u5230\u8bb0\u5fc6 {arg}"
        )

    async def _handle_tm_unpin(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        arg = re.sub(r"^/tm_unpin\s*", "", raw, flags=re.IGNORECASE).strip()
        if not arg.isdigit():
            yield event.plain_result("\u7528\u6cd5: /tm_unpin <\u8bb0\u5fc6ID>")
            return
        ok = self._set_pinned(int(arg), False)
        yield event.plain_result(
            f"\u8bb0\u5fc6 {arg} \u5df2\u53d6\u6d88\u5e38\u9a7b" if ok else f"\u672a\u627e\u5230\u8bb0\u5fc6 {arg}"
        )

    async def _handle_tm_export(self, event: AstrMessageEvent):
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        data = self._export_user_data(canonical_id)
        yield event.plain_result(json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    async def _handle_tm_purge(self, event: AstrMessageEvent):
        canonical_id, _, _ = self._identity_mgr.resolve_current_identity(event)
        deleted = self._purge_user_data(canonical_id)
        yield event.plain_result(
            f"\u5df2\u6e05\u9664 {canonical_id} \u7684\u6240\u6709\u6570\u636e:{deleted['memories']} \u6761\u8bb0\u5fc6\uff0c{deleted['cache']} \u6761\u7f13\u5b58\u3002"
        )
