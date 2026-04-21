import json
from types import SimpleNamespace

from aiohttp import web


class FakeRequest(dict):
    def __init__(self, *, path, method="GET", headers=None, query=None, json_data=None, peer_ip="127.0.0.1"):
        super().__init__()
        self.path = path
        self.method = method
        self.headers = headers or {}
        self.query = query or {}
        self._json_data = json_data
        self.transport = SimpleNamespace(get_extra_info=lambda _name: (peer_ip, 12345))

    async def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


def test_login_returns_token_on_valid_credentials(web_module):
    server = web_module.TMemoryWebServer(
        plugin=SimpleNamespace(),
        config={
            "webui_enabled": True,
            "webui_username": "admin",
            "webui_password": "secret",
        },
    )
    request = FakeRequest(
        path="/api/login",
        method="POST",
        json_data={"username": "admin", "password": "secret"},
    )

    response = __import__("asyncio").run(server._handle_login(request))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["token"]


def test_middleware_rejects_missing_token(web_module):
    server = web_module.TMemoryWebServer(
        plugin=SimpleNamespace(),
        config={"webui_enabled": True, "webui_password": "secret"},
    )
    request = FakeRequest(path="/api/users")

    async def handler(_request):
        return web.json_response({"ok": True})

    response = __import__("asyncio").run(server._middleware(request, handler))
    payload = json.loads(response.text)

    assert response.status == 401
    assert payload["error"] == "未登录"


def test_middleware_allows_valid_bearer_token(web_module):
    server = web_module.TMemoryWebServer(
        plugin=SimpleNamespace(),
        config={
            "webui_enabled": True,
            "webui_username": "admin",
            "webui_password": "secret",
        },
    )
    token = web_module.jwt_encode({"user": "admin"}, server._jwt_secret, exp_seconds=3600)
    request = FakeRequest(
        path="/api/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    async def handler(inner_request):
        return web.json_response({"user": inner_request["user"]})

    response = __import__("asyncio").run(server._middleware(request, handler))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["user"] == "admin"


def test_middleware_blocks_non_whitelisted_ip(web_module):
    server = web_module.TMemoryWebServer(
        plugin=SimpleNamespace(),
        config={
            "webui_enabled": True,
            "webui_password": "secret",
            "webui_ip_whitelist": "10.0.0.1",
        },
    )
    request = FakeRequest(path="/api/users", peer_ip="127.0.0.1")

    async def handler(_request):
        return web.json_response({"ok": True})

    response = __import__("asyncio").run(server._middleware(request, handler))
    payload = json.loads(response.text)

    assert response.status == 403
    assert payload["error"] == "IP not allowed"
