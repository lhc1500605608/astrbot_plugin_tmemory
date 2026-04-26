import asyncio
import json

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


def test_update_config_preserves_readonly_style_distill_switch(web_module, plugin):
    saved = {}

    class Context:
        def get_config(self):
            return {
                "style_distill_settings": {
                    "enable_style_distill": True,
                    "enable_style_injection": False,
                }
            }

        def save_config(self, config):
            saved.update(config)

    plugin.context = Context()
    server = web_module.TMemoryWebServer(plugin, {"webui_enabled": True})

    resp = asyncio.run(
        server._handle_update_config(
            JsonRequest({
                "style_distill_settings": {
                    "enable_style_distill": False,
                    "enable_style_injection": True,
                }
            })
        )
    )

    assert resp.status == 200
    assert json.loads(resp.text)["status"] == "ok"
    assert saved["style_distill_settings"]["enable_style_distill"] is True
    assert saved["style_distill_settings"]["enable_style_injection"] is True


def test_style_binding_api_rejects_default_null_profile(web_module, plugin):
    server = web_module.TMemoryWebServer(plugin, {"webui_enabled": True})

    resp = asyncio.run(
        server._handle_set_style_binding(
            JsonRequest({
                "adapter_name": "qq",
                "conversation_id": "conv-default",
                "profile_id": 0,
            })
        )
    )

    assert resp.status == 400
    assert "profile_id" in resp.text
