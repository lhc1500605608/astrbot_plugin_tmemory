import asyncio
import json
import re
import struct
from pathlib import Path

import pytest
from aiohttp import web


class JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def test_web_login_returns_token_and_rejects_wrong_password(web_module, plugin):
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_username": "admin",
            "webui_password": "secret",
        },
    )

    ok_resp = asyncio.run(server._handle_login(JsonRequest({"username": "admin", "password": "secret"})))
    bad_resp = asyncio.run(server._handle_login(JsonRequest({"username": "admin", "password": "wrong"})))

    assert ok_resp.status == 200
    assert ok_resp.text
    assert "token" in ok_resp.text
    assert bad_resp.status == 401


def test_web_middleware_enforces_ip_whitelist_and_authentication(web_module, plugin):
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_password": "secret",
            "webui_ip_whitelist": "127.0.0.1",
        },
    )

    token = web_module.jwt_encode({"user": "admin"}, server._jwt_secret, 3600)

    async def protected_handler(_request):
        return web.json_response({"ok": True})

    class Request:
        def __init__(self, path, headers=None, ip="127.0.0.1"):
            self.path = path
            self.headers = headers or {}
            self.query = {}
            self.method = "GET"
            self.transport = self
            self._storage = {}
            self._ip = ip

        def get_extra_info(self, name):
            if name == "peername":
                return (self._ip, 12345)
            return None

        def __setitem__(self, key, value):
            self._storage[key] = value

        def __getitem__(self, key):
            return self._storage[key]

    blocked = asyncio.run(server._middleware(Request("/api/users", ip="10.0.0.1"), protected_handler))
    unauthorized = asyncio.run(server._middleware(Request("/api/users"), protected_handler))
    authorized = asyncio.run(
        server._middleware(
            Request("/api/users", headers={"Authorization": f"Bearer {token}"}),
            protected_handler,
        )
    )

    assert blocked.status == 403
    assert unauthorized.status == 401
    assert authorized.status == 200


def test_web_middleware_allows_favicon_without_auth_and_route_is_registered(
    web_module, plugin
):
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_password": "secret",
        },
    )
    server._app = web.Application()
    server._setup_routes()

    routes = {
        route.resource.canonical
        for route in server._app.router.routes()
        if hasattr(route.resource, "canonical")
    }
    assert "/favicon.ico" in routes

    async def favicon_handler(_request):
        return web.Response(status=204)

    class Request:
        def __init__(self, path):
            self.path = path
            self.headers = {}
            self.query = {}
            self.method = "GET"
            self.transport = self
            self._storage = {}

        def get_extra_info(self, name):
            if name == "peername":
                return ("127.0.0.1", 12345)
            return None

        def __setitem__(self, key, value):
            self._storage[key] = value

        def __getitem__(self, key):
            return self._storage[key]

    response = asyncio.run(server._middleware(Request("/favicon.ico"), favicon_handler))
    assert response.status == 204


def test_web_middleware_preserves_http_not_found_from_handler(web_module, plugin):
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_password": "secret",
        },
    )

    async def missing_handler(_request):
        raise web.HTTPNotFound(text="missing route")

    class Request:
        def __init__(self):
            self.path = "/static/missing.js"
            self.headers = {}
            self.query = {}
            self.method = "GET"
            self.transport = self
            self._storage = {}

        def get_extra_info(self, name):
            if name == "peername":
                return ("127.0.0.1", 12345)
            return None

        def __setitem__(self, key, value):
            self._storage[key] = value

        def __getitem__(self, key):
            return self._storage[key]

    with pytest.raises(web.HTTPNotFound):
        asyncio.run(server._middleware(Request(), missing_handler))


def test_web_middleware_converts_not_found_to_json_for_api_routes(web_module, plugin):
    """Non-static 404s are returned as JSON so the frontend can parse the error."""
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_password": "secret",
        },
    )
    token = web_module.jwt_encode({"user": "admin"}, server._jwt_secret, 3600)

    async def missing_handler(_request):
        raise web.HTTPNotFound(text="missing route")

    class Request:
        def __init__(self):
            self.path = "/api/missing"
            self.headers = {"Authorization": f"Bearer {token}"}
            self.query = {}
            self.method = "GET"
            self.transport = self
            self._storage = {}

        def get_extra_info(self, name):
            if name == "peername":
                return ("127.0.0.1", 12345)
            return None

        def __setitem__(self, key, value):
            self._storage[key] = value

        def __getitem__(self, key):
            return self._storage[key]

    resp = asyncio.run(server._middleware(Request(), missing_handler))
    assert resp.status == 404
    body = json.loads(resp.text) if hasattr(resp, "text") else json.loads(resp.body)
    assert "未找到" in body.get("error", "")


def test_web_middleware_still_re_raises_404_for_static_routes(web_module, plugin):
    """Static resource 404s are re-raised so aiohttp can serve its default response."""
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_password": "secret",
        },
    )

    async def missing_handler(_request):
        raise web.HTTPNotFound(text="missing file")

    class Request:
        def __init__(self):
            self.path = "/static/js/nonexistent.js"
            self.headers = {}
            self.query = {}
            self.method = "GET"
            self.transport = self
            self._storage = {}

        def get_extra_info(self, name):
            if name == "peername":
                return ("127.0.0.1", 12345)
            return None

        def __setitem__(self, key, value):
            self._storage[key] = value

        def __getitem__(self, key):
            return self._storage[key]

    with pytest.raises(web.HTTPNotFound):
        asyncio.run(server._middleware(Request(), missing_handler))


def test_dashboard_memoryforge_brand_assets_are_consistent_and_lightweight():
    """MemoryForge WebUI should not ship mismatched or oversized icon assets."""
    dashboard_path = Path("templates/dashboard.html")
    icons_dir = Path("templates/static/icons")

    html = dashboard_path.read_text(encoding="utf-8")

    assert "<title>MemoryForge - 记忆管理面板</title>" in html
    assert 'alt="MemoryForge"' in html
    assert "tmemory - 记忆管理面板" not in html

    icon_links = re.findall(
        r'<link rel="icon" type="image/png" sizes="(\d+)x(\d+)" href="/static/icons/([^"]+)">',
        html,
    )
    assert icon_links

    for declared_width, declared_height, filename in icon_links:
        icon_path = icons_dir / filename
        assert icon_path.exists(), f"missing icon asset: {filename}"
        assert _png_size(icon_path) == (int(declared_width), int(declared_height))

    hero_icon = icons_dir / "tmemory-icon.png"
    assert hero_icon.stat().st_size <= 200_000

    expected_assets = {
        Path("logo.png"): (192, 192),
        icons_dir / "tmemory-icon.png": (192, 192),
        icons_dir / "favicon-16.png": (16, 16),
        icons_dir / "favicon-32.png": (32, 32),
        icons_dir / "favicon.ico": (32, 32),
    }
    for asset_path, expected_size in expected_assets.items():
        assert asset_path.exists(), f"missing icon asset: {asset_path}"
        assert _png_size(asset_path) == expected_size


def test_dashboard_memoryforge_css_and_export_contract():
    css = Path("templates/static/css/dashboard.css").read_text(encoding="utf-8")
    main_js = Path("templates/static/js/main.js").read_text(encoding="utf-8")

    assert "MemoryForge WebUI Dashboard Styles" in css
    assert "--bg: #111827;" in css
    assert "--surface: #1f2937;" in css
    assert "--surface2: #374151;" in css
    assert "--text: #f3f4f6;" in css
    assert "--text2: #9ca3af;" in css
    assert "font-family: var(--font);" in css

    assert "MemoryForge_export_${currentUser}.json" in main_js
    assert "tmemory_export_${currentUser}.json" not in main_js


def test_dashboard_switch_tab_only_references_rendered_panels():
    html = Path("templates/dashboard.html").read_text(encoding="utf-8")
    ui_js = Path("templates/static/js/ui.js").read_text(encoding="utf-8")

    rendered_panel_ids = set(re.findall(r'id="(panel[A-Za-z]+)"', html))
    referenced_panel_ids = set(re.findall(r"'panel[A-Za-z]+'", ui_js))
    referenced_panel_ids = {panel_id.strip("'") for panel_id in referenced_panel_ids}

    assert referenced_panel_ids <= rendered_panel_ids


def test_dashboard_default_entry_is_profile_workbench_without_mindmap_assets():
    html = Path("templates/dashboard.html").read_text(encoding="utf-8")
    ui_js = Path("templates/static/js/ui.js").read_text(encoding="utf-8")
    main_js = Path("templates/static/js/main.js").read_text(encoding="utf-8")

    script_sources = re.findall(r'<script src="([^"]+)"></script>', html)

    assert '<button class="active" onclick="switchTab(\'profile\',this)">👤 画像工作台</button>' in html
    assert 'id="panelProfile"' in html
    assert '<!-- Profile Workbench -->' in html
    assert 'style="display:flex;flex-direction:column"' in html
    assert "profile:'panelProfile'" in ui_js
    assert 'loadProfileSummary(currentUser)' in ui_js
    assert 'loadProfileItems(currentUser)' in ui_js
    assert 'loadProfileSummary(userId)' in main_js
    assert 'loadProfileItems(userId)' in main_js
    assert '/static/js/profile.js' in script_sources

    assert '/static/js/mindmap.js' not in script_sources
    assert '/static/vendor/d3.v7.min.js' not in script_sources
    assert "switchTab('mindmap'" not in html
    assert 'id="panelMindmap"' not in html


def test_dashboard_does_not_reference_removed_profile_creation_ui():
    html = Path("templates/dashboard.html").read_text(encoding="utf-8")

    assert "createProfile()" not in html
    assert 'id="profileModal"' not in html


def test_get_config_returns_plugin_config_not_global(web_module, plugin):
    """GET /api/config 应返回 tmemory 插件配置，而非 AstrBot 全局配置。"""
    server = web_module.TMemoryWebServer(
        plugin,
        {
            "webui_enabled": True,
            "webui_password": "secret",
        },
    )

    class _Req:
        def __init__(self, query_keys=""):
            self.query = {"keys": query_keys}

    # ── 无 keys 参数：返回完整插件配置 ──
    resp_full = asyncio.run(server._handle_get_config(_Req()))
    assert resp_full.status == 200
    body_full = json.loads(resp_full.text)
    assert "memory_mode" in body_full
    assert "distill_pause" in body_full
    assert "platform" not in body_full, "不应包含 AstrBot 全局配置键"

    # ── 带 keys 参数：仅返回请求的键 ──
    resp_keys = asyncio.run(
        server._handle_get_config(_Req("memory_mode,distill_pause"))
    )
    assert resp_keys.status == 200
    body_keys = json.loads(resp_keys.text)
    assert "memory_mode" in body_keys
    assert "distill_pause" in body_keys
    assert body_keys["memory_mode"] == "hybrid"
    assert body_keys["distill_pause"] is False


def _png_size(path):
    with path.open("rb") as file:
        header = file.read(24)
    assert header.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", header[16:24])
