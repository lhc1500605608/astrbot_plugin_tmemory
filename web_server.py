"""tmemory 独立 WebUI 服务器。

使用 aiohttp 在单独端口运行，支持：
- 管理员账户登录（JWT token）
- IP 白名单
- 信任反向代理（X-Forwarded-For / X-Real-IP）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from aiohttp import web

try:
    from astrbot.api import logger as _astrbot_logger  # available at runtime
except ImportError:
    import logging

    _astrbot_logger = logging.getLogger("tmemory")  # type: ignore[assignment]

if TYPE_CHECKING:
    from main import TMemoryPlugin

# JWT 极简实现（不引入外部依赖）
# ──────────────────────────────────────────────────────────────────────────────


def _b64url_encode(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    import base64

    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def jwt_encode(payload: dict, secret: str, exp_seconds: int = 86400) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        **payload,
        "exp": int(time.time()) + exp_seconds,
        "iat": int(time.time()),
    }
    h = _b64url_encode(json.dumps(header).encode())
    p = _b64url_encode(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url_encode(sig)}"


def jwt_decode(token: str, secret: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        h, p, s = parts
        expected_sig = hmac.new(
            secret.encode(), f"{h}.{p}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64url_decode(s), expected_sig):
            return None
        payload = json.loads(_b64url_decode(p))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# 服务器类
# ──────────────────────────────────────────────────────────────────────────────


class TMemoryWebServer:
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
            from core.admin_service import AdminService
            self._admin = AdminService(self.plugin)
        return self._admin

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
        app.router.add_post("/api/login", self._handle_login)
        app.router.add_get("/api/users", self._handle_get_users)
        app.router.add_get("/api/stats", self._handle_get_stats)
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
        app.router.add_post("/api/user/export", self._handle_export_user)
        app.router.add_post("/api/user/purge", self._handle_purge_user)
        app.router.add_post("/api/memory/pin", self._handle_pin_memory)
        app.router.add_post("/api/memory/refine", self._handle_memory_refine)
        app.router.add_post("/api/memory/merge", self._handle_memory_merge)
        app.router.add_post("/api/memory/split", self._handle_memory_split)

    # ── 中间件：IP 白名单 + JWT 鉴权 ─────────────────────────────────────

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Callable):
        client_ip = self._get_client_ip(request)

        # IP 白名单检查
        if self.ip_whitelist:
            if client_ip not in self.ip_whitelist:
                return web.json_response({"error": "IP not allowed"}, status=403)

        path = request.path

        # 登录接口、首页和静态资源不需要 token
        if path in ("/", "/api/login") or path.startswith("/static/"):
            try:
                return await handler(request)
            except Exception as exc:
                _astrbot_logger.exception("[tmemory-web] handler error: %s %s", request.method, request.path)
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
        except Exception as exc:
            _astrbot_logger.exception("[tmemory-web] handler error: %s %s", request.method, request.path)
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

    # ── Handlers ─────────────────────────────────────────────────────────

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

    async def _handle_login(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        username = str(data.get("username", ""))
        password = str(data.get("password", ""))

        if username == self.username and password == self.password:
            token = jwt_encode({"user": username}, self._jwt_secret, self.token_expire)
            return web.json_response({"ok": True, "token": token})

        return web.json_response({"error": "用户名或密码错误"}, status=401)

    # ── Batch 1.1: 只读查询 ──────────────────────────────────────────────

    async def _handle_get_users(self, request: web.Request):
        admin = self._get_admin()
        users = admin.get_users()
        return web.json_response({"users": users})

    async def _handle_get_stats(self, request: web.Request):
        admin = self._get_admin()
        stats = admin.get_global_stats()
        return web.json_response(stats)

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
        return web.json_response({"history": history})

    # ── Batch 1.2: 低风险写操作 ──────────────────────────────────────────

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

    # ── Batch 1.3: 高风险写操作 ──────────────────────────────────────────

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

    async def _handle_merge_users(self, request: web.Request):
        data = await request.json()
        from_id = str(data.get("from_user", "")).strip()
        to_id = str(data.get("to_user", "")).strip()
        if not from_id or not to_id:
            return web.json_response({"error": "from_user and to_user are required"}, status=400)
        if from_id == to_id:
            return web.json_response({"error": "两个用户 ID 相同，无需合并"}, status=400)
        admin = self._get_admin()
        moved = admin.merge_users(from_id, to_id)
        return web.json_response({"ok": True, "moved": moved, "from_user": from_id, "to_user": to_id})

    async def _handle_rebind_user(self, request: web.Request):
        data = await request.json()
        binding_id = int(data.get("binding_id", 0))
        new_canonical = str(data.get("new_canonical_user_id", "")).strip()
        if not binding_id or not new_canonical:
            return web.json_response({"error": "binding_id and new_canonical_user_id are required"}, status=400)
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
            return web.json_response({"error": "user and ids(>=2) are required"}, status=400)
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
