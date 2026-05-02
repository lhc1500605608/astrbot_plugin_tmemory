import asyncio
import json
import re
import struct
from pathlib import Path

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


def _png_size(path):
    with path.open("rb") as file:
        header = file.read(24)
    assert header.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", header[16:24])
