"""tmemory WebUI 请求处理器（mixin）。

所有 _handle_* 方法作为 mixin 混入 TMemoryWebServer，保持 self._get_admin() 等
调用自然可用，同时将路由注册与业务逻辑分离到不同模块。
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import TYPE_CHECKING

from aiohttp import web

try:
    from astrbot.api import logger as _astrbot_logger
except ImportError:
    import logging

    _astrbot_logger = logging.getLogger("tmemory")

if TYPE_CHECKING:
    from main import TMemoryPlugin


class WebHandlersMixin:
    """Handler 方法集合，混入 TMemoryWebServer 使用。"""

    # ── 页面 & 登录 ────────────────────────────────────────────────────────

    async def _handle_page(self, request: web.Request):
        html_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "templates", "dashboard.html"
        )
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                return web.Response(
                    text=f.read(), content_type="text/html", charset="utf-8"
                )
        except FileNotFoundError:
            return web.Response(text="dashboard.html not found", status=500)

    async def _handle_favicon(self, request: web.Request):
        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "templates",
            "static",
            "icons",
            "favicon.ico",
        )
        if os.path.exists(icon_path):
            return web.FileResponse(icon_path)
        return web.Response(status=204)

    async def _handle_login(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        username = str(data.get("username", ""))
        password = str(data.get("password", ""))

        if username == self.username and password == self.password:
            from .core.utils_shared import jwt_encode
            token = jwt_encode({"user": username}, self._jwt_secret, self.token_expire)
            return web.json_response({"ok": True, "token": token})

        return web.json_response({"error": "用户名或密码错误"}, status=401)

    # ── 只读查询 ──────────────────────────────────────────────────────────

    async def _handle_get_users(self, request: web.Request):
        admin = self._get_admin()
        users = admin.get_users()
        return web.json_response({"users": users})

    async def _handle_get_stats(self, request: web.Request):
        admin = self._get_admin()
        stats = admin.get_global_stats()
        return web.json_response(stats)

    async def _handle_get_mindmap(self, request: web.Request):
        admin = self._get_admin()
        user = request.query.get("user", "")
        data = admin.get_mindmap_data(user)
        return web.json_response(data)

    async def _handle_get_memories(self, request: web.Request):
        admin = self._get_admin()
        user = request.query.get("user", "")
        memories = admin.get_memories(user)
        return web.json_response({"memories": memories})

    async def _handle_get_events(self, request: web.Request):
        admin = self._get_admin()
        user = request.query.get("user", "")
        events = admin.get_events(user)
        return web.json_response({"events": events})

    async def _handle_get_pending(self, request: web.Request):
        admin = self._get_admin()
        pending = admin.get_pending()
        return web.json_response({"pending": pending})

    async def _handle_get_identities(self, request: web.Request):
        admin = self._get_admin()
        bindings = admin.get_identities()
        return web.json_response({"bindings": bindings})

    async def _handle_distill_history(self, request: web.Request):
        admin = self._get_admin()
        history = admin.get_distill_history(limit=30)
        budget_info = admin.get_distill_budget_info()
        return web.json_response({"history": history, "budget": budget_info})

    # ── 低风险写操作 ──────────────────────────────────────────────────────

    async def _handle_add_memory(self, request: web.Request):
        data = await request.json()
        user = str(data.get("user", ""))
        memory = str(data.get("memory", "")).strip()
        if not user or not memory:
            return web.json_response(
                {"error": "user and memory are required"}, status=400
            )
        admin = self._get_admin()
        mem_id = admin.add_memory(
            user=user,
            memory=memory,
            score=float(data.get("score", 0.7)),
            memory_type=str(data.get("memory_type", "fact")),
            importance=float(data.get("importance", 0.6)),
            confidence=float(data.get("confidence", 0.7)),
        )
        return web.json_response({"ok": True, "memory_id": mem_id})

    async def _handle_update_memory(self, request: web.Request):
        data = await request.json()
        mem_id = int(data.get("id", 0))
        if not mem_id:
            return web.json_response({"error": "id is required"}, status=400)
        admin = self._get_admin()
        try:
            admin.update_memory(mem_id, data)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def _handle_delete_memory(self, request: web.Request):
        data = await request.json()
        mem_id = int(data.get("id", 0))
        if not mem_id:
            return web.json_response({"error": "id is required"}, status=400)
        admin = self._get_admin()
        deleted = admin.delete_memory(mem_id)
        return web.json_response({"ok": deleted})

    async def _handle_pin_memory(self, request: web.Request):
        data = await request.json()
        mem_id = int(data.get("id", 0))
        pinned = bool(data.get("pinned", True))
        if not mem_id:
            return web.json_response({"error": "id is required"}, status=400)
        admin = self._get_admin()
        ok = admin.set_pinned(mem_id, pinned)
        return web.json_response({"ok": ok, "pinned": pinned})

    # ── 高风险写操作 ──────────────────────────────────────────────────────

    async def _handle_trigger_distill(self, request: web.Request):
        admin = self._get_admin()
        result = await admin.trigger_distill()
        return web.json_response({"ok": True, **result})

    async def _handle_distill_pause(self, request: web.Request):
        data = await request.json()
        pause = bool(data.get("pause", True))
        admin = self._get_admin()
        admin.set_distill_pause(pause)
        return web.json_response({"ok": True, "distill_pause": pause})

    # ── 用户画像 handlers ──────────────────────────────────────────────────

    async def _handle_profile_summary(self, request: web.Request):
        admin = self._get_admin()
        user = request.query.get("user", "")
        summary = admin.get_profile_summary(user)
        return web.json_response(summary)

    async def _handle_profile_items(self, request: web.Request):
        admin = self._get_admin()
        user = request.query.get("user", "")
        facet_type = request.query.get("facet_type", "")
        status = request.query.get("status", "active")
        items = admin.get_profile_items(user, facet_type, status)
        return web.json_response({"items": items})

    async def _handle_profile_item_evidence(self, request: web.Request):
        admin = self._get_admin()
        item_id = int(request.match_info.get("id", 0))
        if not item_id:
            return web.json_response({"error": "id is required"}, status=400)
        evidence = admin.get_profile_item_evidence(item_id)
        return web.json_response({"evidence": evidence})

    async def _handle_update_profile_item(self, request: web.Request):
        try:
            data = await self._require_json_object(request)
            item_id = self._require_positive_int(data.get("id"), field="id")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        admin = self._get_admin()
        try:
            admin.update_profile_item(item_id, data)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    async def _handle_archive_profile_item(self, request: web.Request):
        try:
            data = await self._require_json_object(request)
            item_id = self._require_positive_int(data.get("id"), field="id")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        admin = self._get_admin()
        ok = admin.archive_profile_item(item_id)
        return web.json_response({"ok": ok})

    async def _handle_merge_profile_items(self, request: web.Request):
        try:
            data = await self._require_json_object(request)
            user = str(data.get("user", "")).strip()
            if not user:
                raise ValueError("user is required")
            ids = self._require_distinct_positive_ints(data.get("ids"), field="ids")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        admin = self._get_admin()
        try:
            result = admin.merge_profile_items(user, ids)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, **result})

    async def _handle_merge_users(self, request: web.Request):
        try:
            data = await self._require_json_object(request)
            from_id = str(data.get("from_user", "")).strip()
            to_id = str(data.get("to_user", "")).strip()
            if not from_id or not to_id:
                raise ValueError("from_user and to_user are required")
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        if from_id == to_id:
            return web.json_response({"error": "两个用户 ID 相同，无需合并"}, status=400)
        admin = self._get_admin()
        moved = admin.merge_users(from_id, to_id)
        return web.json_response(
            {"ok": True, "moved": moved, "from_user": from_id, "to_user": to_id}
        )

    async def _handle_rebind_user(self, request: web.Request):
        data = await request.json()
        binding_id = int(data.get("binding_id", 0))
        new_canonical = str(data.get("new_canonical_user_id", "")).strip()
        if not binding_id or not new_canonical:
            return web.json_response(
                {"error": "binding_id and new_canonical_user_id are required"},
                status=400,
            )
        admin = self._get_admin()
        try:
            admin.rebind_user(binding_id, new_canonical)
        except LookupError:
            return web.json_response({"error": "binding not found"}, status=404)
        return web.json_response({"ok": True})

    async def _handle_export_user(self, request: web.Request):
        data = await request.json()
        user = str(data.get("user", "")).strip()
        if not user:
            return web.json_response({"error": "user is required"}, status=400)
        admin = self._get_admin()
        export = admin.export_user(user)
        return web.json_response(export)

    async def _handle_purge_user(self, request: web.Request):
        data = await request.json()
        user = str(data.get("user", "")).strip()
        if not user:
            return web.json_response({"error": "user is required"}, status=400)
        admin = self._get_admin()
        result = admin.purge_user(user)
        return web.json_response({"ok": True, **result})

    async def _handle_memory_refine(self, request: web.Request):
        data = await request.json()
        user = str(data.get("user", "")).strip()
        if not user:
            return web.json_response({"error": "user is required"}, status=400)
        admin = self._get_admin()
        result = await admin.refine_memories(
            user=user,
            mode=str(data.get("mode", "")).lower(),
            limit=int(data.get("limit", 0)),
            dry_run=bool(data.get("dry_run", False)),
            include_pinned=bool(data.get("include_pinned", False)),
            extra_instruction=str(data.get("extra_instruction", "")).strip(),
            unified_msg_origin=str(data.get("unified_msg_origin", "")),
        )
        return web.json_response({"ok": True, **result})

    async def _handle_memory_merge(self, request: web.Request):
        data = await request.json()
        user = str(data.get("user", "")).strip()
        ids = data.get("ids", [])
        merged_text = str(data.get("memory", "")).strip()
        if not user or not isinstance(ids, list) or len(ids) < 2:
            return web.json_response(
                {"error": "user and ids(>=2) are required"}, status=400
            )
        admin = self._get_admin()
        try:
            result = await admin.merge_memories(user, ids, merged_text)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, **result})

    async def _handle_memory_split(self, request: web.Request):
        data = await request.json()
        user = str(data.get("user", "")).strip()
        memory_id = int(data.get("id", 0))
        segments = data.get("segments", None)
        if not user or not memory_id:
            return web.json_response({"error": "user and id are required"}, status=400)
        admin = self._get_admin()
        try:
            result = await admin.split_memory(
                user=user,
                memory_id=memory_id,
                segments=segments if isinstance(segments, list) else None,
                unified_msg_origin=str(data.get("unified_msg_origin", "")),
            )
        except LookupError:
            return web.json_response({"error": "memory not found"}, status=404)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, **result})

    async def _handle_get_config(self, request: web.Request):
        self._get_admin()  # ensure auth
        keys = request.query.get("keys", "").split(",")
        keys = [k.strip() for k in keys if k.strip()]

        config_dict = asdict(self.plugin._cfg)
        # capture_skip_regex is a compiled re.Pattern, not JSON-serializable
        config_dict.pop("capture_skip_regex", None)

        if not keys:
            return web.json_response(config_dict)

        return web.json_response(
            {k: config_dict[k] for k in keys if k in config_dict}
        )

    async def _handle_update_config(self, request: web.Request):
        self._get_admin()  # ensure auth
        try:
            data = await self._require_json_object(request)
            self._validate_config_patch(data)

            current_config = self.plugin.config

            for k, v in data.items():
                current_config[k] = v

            if hasattr(current_config, "save_config"):
                current_config.save_config()
            from .core.config import parse_config

            self.plugin._cfg = parse_config(current_config)

            return web.json_response({"status": "ok"})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception:
            _astrbot_logger.exception("[tmemory-web] config update failed")
            return web.json_response({"error": "failed to update config"}, status=400)

    # ── 测试对话模拟 ──────────────────────────────────────────────────

    async def _handle_test_conversation(self, request: web.Request):
        """插入一条测试对话到 conversation_cache。

        POST /api/test/conversation
        Body: {user_id, role, content, source_adapter?, source_user_id?,
               unified_msg_origin?, scope?, persona_id?}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        admin = self._get_admin()
        result = await admin.insert_test_conversation(
            user_id=str(data.get("user_id", "")).strip(),
            role=str(data.get("role", "user")).strip().lower(),
            content=str(data.get("content", "")).strip(),
            source_adapter=str(data.get("source_adapter", "")).strip(),
            source_user_id=str(data.get("source_user_id", "")).strip(),
            unified_msg_origin=str(data.get("unified_msg_origin", "")).strip(),
            scope=str(data.get("scope", "user")).strip(),
            persona_id=str(data.get("persona_id", "")).strip(),
        )
        if result.get("ok"):
            return web.json_response(result)
        return web.json_response(result, status=400)
