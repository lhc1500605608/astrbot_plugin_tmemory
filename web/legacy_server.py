"""tmemory 独立 WebUI 服务器。

使用 aiohttp 在单独端口运行，支持：
- 管理员账户登录（JWT token）
- IP 白名单
- 信任反向代理（X-Forwarded-For / X-Real-IP）
"""

from __future__ import annotations

import os
import secrets
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from aiohttp import web

try:
    from astrbot.api import logger as _astrbot_logger  # available at runtime
except ImportError:
    import logging

    _astrbot_logger = logging.getLogger("tmemory")  # type: ignore[assignment]

if TYPE_CHECKING:
    from main import TMemoryPlugin

from .web_handlers import WebHandlersMixin
from .core.utils_shared import jwt_decode, jwt_encode

# 服务器类
# ──────────────────────────────────────────────────────────────────────────────


class TMemoryWebServer(WebHandlersMixin):
    """tmemory 独立 Web 面板服务器。"""

    def __init__(self, plugin: TMemoryPlugin, config: Dict[str, Any]):
        self.plugin = plugin
        self.enabled: bool = bool(config.get("webui_enabled", False))
        self.host: str = str(config.get("webui_host", "0.0.0.0"))
        self.port: int = int(config.get("webui_port", 9966))
        self.username: str = str(config.get("webui_username", "admin"))
        self.password: str = str(config.get("webui_password", ""))
        self.trust_proxy: bool = bool(config.get("webui_trust_proxy", False))
        self.token_expire: int = int(config.get("webui_token_expire_hours", 24)) * 3600

        whitelist_raw = config.get("webui_ip_whitelist", "")
        if isinstance(whitelist_raw, list):
            self.ip_whitelist: List[str] = [
                s.strip() for s in whitelist_raw if s.strip()
            ]
        else:
            self.ip_whitelist = [
                s.strip() for s in str(whitelist_raw).split(",") if s.strip()
            ]

        # JWT secret：每次启动随机生成，重启后旧 token 自动失效
        self._jwt_secret = secrets.token_hex(32)

        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None

        # AdminService — 延迟初始化（插件可能还未完成 DB 初始化）
        self._admin = None

    def _get_admin(self):
        """延迟构造 AdminService，确保插件 DB 已就绪。"""
        if self._admin is None:
            from .core.admin_service import AdminService
            self._admin = AdminService(self.plugin)
        return self._admin

    async def _require_json_object(self, request: web.Request) -> Dict[str, Any]:
        try:
            data = await request.json()
        except Exception as exc:
            raise ValueError("invalid json") from exc
        if not isinstance(data, dict):
            raise ValueError("json object required")
        return data

    def _require_positive_int(self, value: Any, *, field: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field} must be a positive integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{field} must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a positive integer") from exc
        if parsed <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return parsed

    def _require_distinct_positive_ints(
        self, value: Any, *, field: str, min_count: int = 2
    ) -> List[int]:
        if not isinstance(value, list):
            raise ValueError(
                f"{field} must contain at least {min_count} positive integers"
            )
        parsed = [self._require_positive_int(item, field=field) for item in value]
        if len(parsed) < min_count:
            raise ValueError(
                f"{field} must contain at least {min_count} positive integers"
            )
        if len(set(parsed)) != len(parsed):
            raise ValueError(f"{field} must be unique")
        return parsed

    def _validate_config_patch(self, patch: Dict[str, Any]) -> None:
        if not patch:
            raise ValueError("config patch is empty")

        from dataclasses import fields

        allowed_keys = {field.name for field in fields(type(self.plugin._cfg))}
        allowed_keys.update(
            {
                "webui_settings",
                "vector_retrieval",
                "profile_storage",
                "consolidation_pipeline",
                "distill_model_settings",
                "consolidation_model_settings",
            }
        )
        if isinstance(self.plugin.config, dict):
            allowed_keys.update(self.plugin.config.keys())

        unknown = sorted(key for key in patch if key not in allowed_keys)
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(unknown)}")

        for nested_key in (
            "webui_settings",
            "vector_retrieval",
            "profile_storage",
            "consolidation_pipeline",
            "distill_model_settings",
            "consolidation_model_settings",
        ):
            if nested_key in patch and not isinstance(patch[nested_key], dict):
                raise ValueError(f"{nested_key} must be a json object")

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def start(self):
        if not self.enabled:
            return
        if not self.password:
            _astrbot_logger.warning(
                "[tmemory-web] webui_password 未设置，WebUI 面板不会启动。请在插件配置中设置密码。"
            )
            return

        self._app = web.Application(middlewares=[self._middleware])
        self._setup_routes()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        _astrbot_logger.info(
            "[tmemory-web] WebUI 面板已启动: http://%s:%s", self.host, self.port
        )

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._app = None

    # ── 路由 ──────────────────────────────────────────────────────────────

    def _setup_routes(self):
        app = self._app
        assert app is not None
        # 静态资源路由（CSS / JS / icons / vendor）
        static_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "templates", "static"
        )
        app.router.add_static("/static", static_dir, name="static")
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/favicon.ico", self._handle_favicon)
        app.router.add_post("/api/login", self._handle_login)
        app.router.add_get("/api/users", self._handle_get_users)
        app.router.add_get("/api/stats", self._handle_get_stats)
        app.router.add_get("/api/mindmap", self._handle_get_mindmap)
        app.router.add_get("/api/memories", self._handle_get_memories)
        app.router.add_get("/api/events", self._handle_get_events)
        app.router.add_post("/api/memory/add", self._handle_add_memory)
        app.router.add_post("/api/memory/update", self._handle_update_memory)
        app.router.add_post("/api/memory/delete", self._handle_delete_memory)
        app.router.add_post("/api/distill", self._handle_trigger_distill)
        app.router.add_get("/api/pending", self._handle_get_pending)
        app.router.add_get("/api/identities", self._handle_get_identities)
        app.router.add_post("/api/identity/merge", self._handle_merge_users)
        app.router.add_post("/api/identity/rebind", self._handle_rebind_user)
        app.router.add_get("/api/distill/history", self._handle_distill_history)
        app.router.add_post("/api/distill/pause", self._handle_distill_pause)
        # ── 用户画像 API ──────────────────────────────────────────────
        app.router.add_get("/api/profile/summary", self._handle_profile_summary)
        app.router.add_get("/api/profile/items", self._handle_profile_items)
        app.router.add_get("/api/profile/items/{id}/evidence", self._handle_profile_item_evidence)
        app.router.add_post("/api/profile/item/update", self._handle_update_profile_item)
        app.router.add_post("/api/profile/item/archive", self._handle_archive_profile_item)
        app.router.add_post("/api/profile/items/merge", self._handle_merge_profile_items)
        app.router.add_post("/api/user/export", self._handle_export_user)
        app.router.add_post("/api/user/purge", self._handle_purge_user)
        app.router.add_post("/api/memory/pin", self._handle_pin_memory)
        app.router.add_post("/api/memory/refine", self._handle_memory_refine)
        app.router.add_post("/api/memory/merge", self._handle_memory_merge)
        app.router.add_post("/api/memory/split", self._handle_memory_split)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_patch("/api/config", self._handle_update_config)
        # 测试对话模拟（受 JWT 保护）
        app.router.add_post("/api/test/conversation", self._handle_test_conversation)

    # ── 中间件：IP 白名单 + JWT 鉴权 ─────────────────────────────────────

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Callable):
        client_ip = self._get_client_ip(request)

        # IP 白名单检查
        if self.ip_whitelist:
            if client_ip not in self.ip_whitelist:
                return web.json_response({"error": "IP not allowed"}, status=403)

        path = request.path

        # 登录接口、首页和浏览器自动资源请求不需要 token
        if path in ("/", "/favicon.ico", "/api/login") or path.startswith("/static/"):
            try:
                return await handler(request)
            except web.HTTPException as exc:
                if exc.status == 404:
                    _astrbot_logger.warning(
                        "[tmemory-web] route not found: %s %s",
                        request.method,
                        request.path,
                    )
                    if path.startswith("/api/"):
                        return web.json_response(
                            {"error": f"未找到: {request.path}"}, status=404
                        )
                raise
            except Exception as exc:
                _astrbot_logger.exception(
                    "[tmemory-web] handler error: %s %s",
                    request.method,
                    request.path,
                )
                return web.json_response(
                    {"error": f"内部错误: {type(exc).__name__}: {exc}"},
                    status=500,
                )

        # 其余 API 需要 JWT
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            token = request.query.get("token", "")

        if not token:
            return web.json_response({"error": "未登录"}, status=401)

        payload = jwt_decode(token, self._jwt_secret)
        if not payload:
            return web.json_response({"error": "登录已过期，请重新登录"}, status=401)

        request["user"] = payload.get("user", "")
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if exc.status == 404:
                _astrbot_logger.warning(
                    "[tmemory-web] route not found after auth: %s %s",
                    request.method,
                    request.path,
                )
                return web.json_response(
                    {"error": f"未找到: {request.path}"}, status=404
                )
            raise
        except Exception as exc:
            _astrbot_logger.exception(
                "[tmemory-web] handler error: %s %s",
                request.method,
                request.path,
            )
            return web.json_response(
                {"error": f"内部错误: {type(exc).__name__}: {exc}"},
                status=500,
            )

    def _get_client_ip(self, request: web.Request) -> str:
        if self.trust_proxy:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
            real_ip = request.headers.get("X-Real-IP", "")
            if real_ip:
                return real_ip.strip()
        peername = (
            request.transport.get_extra_info("peername") if request.transport else None
        )
        return peername[0] if peername else "unknown"

    # Handler 方法由 WebHandlersMixin 提供（定义在 web_handlers.py），
    # 以保持路由注册与业务逻辑的模块边界清晰。
