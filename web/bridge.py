"""Plugin Pages bridge 后端（Plan TMEAAA-354 Phase 3a / T5）。

由 AstrBot Dashboard 托管插件页并统一鉴权：

- 路由通过 ``context.register_web_api(route, view_handler, methods, desc)`` 注册，
  Dashboard 以 JWT + 权限做鉴权，插件不再自签 JWT。
- handler 使用 ``astrbot.api.web`` 的 request 代理读取请求，返回
  ``json_response`` / ``error_response`` 信封。
- 本模块的 ``PluginPagesBridge`` 业务方法只依赖一个 duck-typed request
  （``.query.get`` / ``await .json(default=...)`` / ``.username``），因此可以在
  不启动 AstrBot 的情况下做接口级契约测试。

legacy 能力映射（见 web/legacy_server.py）：登录由 Dashboard 鉴权取代，通过
``/session`` 暴露当前用户；其余统计 / 记忆 CRUD / 画像 / 蒸馏 / 身份 / 配置 /
导出全部对齐。
"""

from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from astrbot.api import logger as _astrbot_logger
except Exception:  # pragma: no cover - 仅测试环境走这里
    import logging

    _astrbot_logger = logging.getLogger("tmemory")

PLUGIN_NAME = "astrbot_plugin_tmemory"

# 与 legacy WebUI 一致：这些配置键允许通过 bridge 写回。
NESTED_CONFIG_KEYS = (
    "basic",
    "vector_retrieval",
    "distill",
    "distill_model_settings",
    "injection",
    "session_identity",
    "profile_storage",
    "proactive",
    "webui_settings",
    # 已弃用（保留以兼容旧配置写回）
    "consolidation_pipeline",
    "consolidation_model_settings",
)

BridgeResult = Tuple[Dict[str, Any], int]


def _load_web_sdk() -> Any:
    """加载并校验 ``astrbot.api.web`` 运行时契约（bridge 的硬依赖）。

    与 ``adapters/version.py:_probe_plugin_pages`` 使用同一契约，保证「能力探测」
    与「运行时」一致：AstrBot <4.28（如 4.23.2）虽有 ``register_web_api`` 但无
    ``astrbot.api.web``，必须在注册阶段拒绝，避免注册成功、请求时全路由 500。
    """
    from ..adapters.version import (
        PLUGIN_PAGES_MIN_VERSION,
        PLUGIN_PAGES_WEB_MODULE,
        plugin_web_request_contract_ok,
    )

    try:
        import importlib

        web_mod = importlib.import_module(PLUGIN_PAGES_WEB_MODULE)
    except Exception as exc:  # pragma: no cover - 由 capability probe 提前拦截
        raise RuntimeError(
            f"{PLUGIN_PAGES_WEB_MODULE} 不可用（需要 AstrBot >= "
            f"{PLUGIN_PAGES_MIN_VERSION}），跳过 plugin pages bridge 注册。"
        ) from exc

    for name in ("json_response", "error_response"):
        if not callable(getattr(web_mod, name, None)):
            raise RuntimeError(f"{PLUGIN_PAGES_WEB_MODULE}.{name} 缺失或不可调用")

    if not plugin_web_request_contract_ok(getattr(web_mod, "request", None)):
        raise RuntimeError(f"{PLUGIN_PAGES_WEB_MODULE}.request 契约不可用")
    return web_mod


@dataclass(frozen=True)
class BridgeRoute:
    """一条 bridge 路由声明（一个 handler 可绑定多个 HTTP 方法）。"""

    methods: Tuple[str, ...]
    path: str
    handler: str
    desc: str


# ── 请求解析 helpers ────────────────────────────────────────────────────────


async def _json_object(request: Any) -> Dict[str, Any]:
    payload = await request.json(default={})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("json object required")
    return payload


def _query_str(request: Any, key: str, default: str = "") -> str:
    return str(request.query.get(key, default) or "")


def _require_positive_int(value: Any, *, field: str) -> int:
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
    value: Any, *, field: str, min_count: int = 2
) -> List[int]:
    if not isinstance(value, list):
        raise ValueError(
            f"{field} must contain at least {min_count} positive integers"
        )
    parsed = [_require_positive_int(item, field=field) for item in value]
    if len(parsed) < min_count:
        raise ValueError(f"{field} must contain at least {min_count} positive integers")
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{field} must be unique")
    return parsed


def _allowed_config_keys(plugin: Any) -> set:
    allowed = {field.name for field in fields(type(plugin._cfg))}
    allowed.update(NESTED_CONFIG_KEYS)
    config = getattr(plugin, "config", None)
    if isinstance(config, dict):
        allowed.update(config.keys())
    return allowed


def _validate_config_patch(plugin: Any, patch: Dict[str, Any]) -> None:
    if not patch:
        raise ValueError("config patch is empty")

    unknown = sorted(key for key in patch if key not in _allowed_config_keys(plugin))
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(unknown)}")

    for nested_key in NESTED_CONFIG_KEYS:
        if nested_key in patch and not isinstance(patch[nested_key], dict):
            raise ValueError(f"{nested_key} must be a json object")


async def _save_plugin_config(current_config: Any) -> bool:
    """优先 save_config_async()，回退同步 save_config()；两者皆无返回 False。"""
    save_async = getattr(current_config, "save_config_async", None)
    if callable(save_async):
        result = save_async()
        if inspect.isawaitable(result):
            await result
        return True

    save_sync = getattr(current_config, "save_config", None)
    if callable(save_sync):
        save_sync()
        return True
    return False


def _config_warnings(cfg: Any) -> List[str]:
    from ..adapters import version as _version_adapter

    warnings: List[str] = []
    if (
        getattr(cfg, "inject_position", "") == "extra_user_temp"
        and not _version_adapter.extra_user_temp_available()
    ):
        warnings.append(_version_adapter.extra_user_temp_hint())
    return warnings


def build_bridge_payload(payload: Any, status: int) -> Tuple[Any, int]:
    """把 handler 的 ``(payload, status)`` 转成 bridge 兼容 JSON 信封。

    - ``status >= 400`` → ``{"status": "error", "message": ...}``（前端 reject）。
    - 其余保持原 payload（前端 resolve）。
    """
    if status >= 400:
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message")
        else:
            message = str(payload)
        body: dict[str, Any] = {
            "status": "error",
            "message": str(message or "request failed"),
        }
        if isinstance(payload, dict) and payload.get("category"):
            body["category"] = str(payload["category"])
        return body, status
    return payload, status


def _redistill_error_status(category: str) -> int:
    """重新蒸馏失败分类 → HTTP 状态码：not_found=404, 配置缺失=400, 上游失败=502。"""
    if category == "not_found":
        return 404
    if category == "no_provider":
        return 400
    return 502


# ── bridge 实现 ─────────────────────────────────────────────────────────────


class PluginPagesBridge:
    """把 legacy WebUI 能力暴露为 Dashboard 插件页 Web API。"""

    ROUTES: Tuple[BridgeRoute, ...] = (
        BridgeRoute(("GET",), "/session", "session", "当前 Dashboard 登录态"),
        BridgeRoute(("GET",), "/users", "users", "用户列表"),
        BridgeRoute(("GET",), "/stats", "stats", "全局统计"),
        BridgeRoute(("GET",), "/mindmap", "mindmap", "记忆思维导图"),
        BridgeRoute(("GET",), "/memories", "memories", "记忆列表"),
        BridgeRoute(("GET",), "/events", "events", "审计事件"),
        BridgeRoute(("GET",), "/pending", "pending", "待蒸馏用户"),
        BridgeRoute(("GET",), "/identities", "identities", "身份绑定"),
        BridgeRoute(("GET",), "/distill/history", "distill_history", "蒸馏历史与预算"),
        BridgeRoute(("POST",), "/memory/add", "memory_add", "新增记忆"),
        BridgeRoute(("POST",), "/memory/update", "memory_update", "更新记忆"),
        BridgeRoute(("POST",), "/memory/delete", "memory_delete", "删除记忆"),
        BridgeRoute(("POST",), "/memory/pin", "memory_pin", "常驻/取消常驻记忆"),
        BridgeRoute(("POST",), "/memory/refine", "memory_refine", "记忆提纯"),
        BridgeRoute(("POST",), "/memory/redistill", "memory_redistill", "重新蒸馏单条记忆"),
        BridgeRoute(("POST",), "/memory/merge", "memory_merge", "合并记忆"),
        BridgeRoute(("POST",), "/memory/split", "memory_split", "拆分记忆"),
        BridgeRoute(("POST",), "/distill", "distill_trigger", "触发蒸馏"),
        BridgeRoute(("POST",), "/distill/pause", "distill_pause", "暂停/恢复蒸馏"),
        BridgeRoute(("POST",), "/identity/merge", "identity_merge", "合并用户身份"),
        BridgeRoute(("POST",), "/identity/rebind", "identity_rebind", "重绑身份"),
        BridgeRoute(("GET",), "/profile/summary", "profile_summary", "画像摘要"),
        BridgeRoute(("GET",), "/profile/items", "profile_items", "画像条目"),
        BridgeRoute(
            ("GET",),
            "/profile/items/<id>/evidence",
            "profile_item_evidence",
            "画像证据",
        ),
        BridgeRoute(("POST",), "/profile/item/update", "profile_item_update", "更新画像"),
        BridgeRoute(("POST",), "/profile/item/archive", "profile_item_archive", "归档画像"),
        BridgeRoute(
            ("POST",), "/profile/items/merge", "profile_items_merge", "合并画像条目"
        ),
        BridgeRoute(("POST",), "/user/export", "user_export", "导出用户数据"),
        BridgeRoute(("POST",), "/user/purge", "user_purge", "清除用户数据"),
        BridgeRoute(("POST",), "/export", "export_data", "导出数据集（记忆+绑定）"),
        BridgeRoute(("POST",), "/import", "import_data", "导入数据集（dry-run/幂等/备份）"),
        BridgeRoute(("GET",), "/config", "config_get", "读取插件配置"),
        BridgeRoute(
            ("POST", "PATCH"), "/config", "config_update", "更新插件配置"
        ),
        BridgeRoute(
            ("GET",),
            "/embedding/providers",
            "embedding_providers",
            "AstrBot Embedding Provider 列表",
        ),
        BridgeRoute(
            ("POST",),
            "/embedding/provider",
            "embedding_provider_set",
            "选择 Embedding Provider",
        ),
        BridgeRoute(("GET",), "/capabilities", "capabilities", "能力探测与降级打点"),
        BridgeRoute(
            ("POST",), "/test/conversation", "test_conversation", "写入测试对话"
        ),
    )

    def __init__(
        self,
        plugin: Any,
        admin_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.plugin = plugin
        self._admin = None
        self._admin_factory = admin_factory or self._default_admin_factory

    @property
    def plugin_name(self) -> str:
        return getattr(self.plugin, "plugin_name", None) or PLUGIN_NAME

    def _default_admin_factory(self) -> Any:
        from ..core.admin_service import AdminService

        return AdminService(self.plugin)

    def admin(self) -> Any:
        if self._admin is None:
            self._admin = self._admin_factory()
        return self._admin

    # ── 注册 ────────────────────────────────────────────────────────────

    def register(self, context: Any) -> int:
        """把所有 bridge 路由注册到 AstrBot Context，返回注册条数。"""
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            raise RuntimeError("context.register_web_api is not available")

        # 运行时契约校验：与能力探测一致，SDK 缺失时拒绝注册（否则请求期 500）。
        _load_web_sdk()

        for route in self.ROUTES:
            handler = getattr(self, route.handler)
            register_web_api(
                f"/{self.plugin_name}{route.path}",
                self._wrap(handler),
                list(route.methods),
                route.desc,
            )
        return len(self.ROUTES)

    def _wrap(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        async def view(**path_params: Any) -> Any:
            web_sdk = _load_web_sdk()
            error_response = web_sdk.error_response
            json_response = web_sdk.json_response
            ab_request = web_sdk.request

            try:
                payload, status = await handler(ab_request, **path_params)
                body, final_status = build_bridge_payload(payload, status)
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
            except LookupError as exc:
                return error_response(str(exc), status_code=404)
            except Exception as exc:  # noqa: BLE001 - bridge 必须兜底
                _astrbot_logger.exception(
                    "[tmemory-pages] bridge handler failed: %s", handler.__name__
                )
                return error_response(
                    f"内部错误: {type(exc).__name__}: {exc}", status_code=500
                )

            if final_status >= 400:
                message = body.get("message") if isinstance(body, dict) else None
                # 保留结构化错误字段（如 category），避免只回 message 丢失语义。
                if isinstance(body, dict) and set(body) - {"status", "message"}:
                    return json_response(body, status_code=final_status)
                return error_response(str(message or "request failed"), status_code=final_status)
            return json_response(body, status_code=final_status)

        return view

    # ── 登录态 ──────────────────────────────────────────────────────────

    async def session(self, request: Any) -> BridgeResult:
        """Dashboard 已完成 JWT 鉴权，这里只回显当前用户。"""
        return {
            "authenticated": True,
            "username": getattr(request, "username", None) or "",
            "auth": "dashboard",
        }, 200

    # ── 只读 ────────────────────────────────────────────────────────────

    async def users(self, request: Any) -> BridgeResult:
        return {"users": self.admin().get_users()}, 200

    async def stats(self, request: Any) -> BridgeResult:
        return dict(self.admin().get_global_stats()), 200

    async def mindmap(self, request: Any) -> BridgeResult:
        return dict(self.admin().get_mindmap_data(_query_str(request, "user"))), 200

    async def memories(self, request: Any) -> BridgeResult:
        return {"memories": self.admin().get_memories(_query_str(request, "user"))}, 200

    async def events(self, request: Any) -> BridgeResult:
        return {"events": self.admin().get_events(_query_str(request, "user"))}, 200

    async def pending(self, request: Any) -> BridgeResult:
        return {"pending": self.admin().get_pending()}, 200

    async def identities(self, request: Any) -> BridgeResult:
        return {"bindings": self.admin().get_identities()}, 200

    async def distill_history(self, request: Any) -> BridgeResult:
        admin = self.admin()
        return {
            "history": admin.get_distill_history(limit=30),
            "budget": admin.get_distill_budget_info(),
        }, 200

    async def capabilities(self, request: Any) -> BridgeResult:
        from ..adapters import version as _version_adapter
        from ..core.sqlite_env import (
            collect_sqlite_capabilities,
            last_dim_change_dict,
            sqlite_env_dict,
        )

        try:
            stats: Any = self.admin().get_global_stats()
        except Exception:  # noqa: BLE001 - DB 异常不应让能力面板整体 500
            stats = None
        capabilities = _version_adapter.get_capabilities().to_dict()
        capabilities.update(collect_sqlite_capabilities(self.plugin, stats))

        return {
            "capabilities": capabilities,
            "sqlite_env": sqlite_env_dict(self.plugin),
            "warnings": _config_warnings(self.plugin._cfg),
            "runtime": {
                "extra_user_temp_fallback_count": getattr(
                    self.plugin, "_extra_user_temp_fallback_count", 0
                ),
                "persona_private_fallback_count": getattr(
                    self.plugin, "_persona_private_fallback_count", 0
                ),
                "last_dim_change": last_dim_change_dict(self.plugin),
            },
        }, 200

    # ── 记忆 CRUD ───────────────────────────────────────────────────────

    async def memory_add(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        memory = str(data.get("memory", "") or "").strip()
        if not user or not memory:
            return {"error": "user and memory are required"}, 400
        mem_id = self.admin().add_memory(
            user=user,
            memory=memory,
            score=float(data.get("score", 0.7)),
            memory_type=str(data.get("memory_type", "fact")),
            importance=float(data.get("importance", 0.6)),
            confidence=float(data.get("confidence", 0.7)),
        )
        return {"ok": True, "memory_id": mem_id}, 200

    async def memory_update(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        data["id"] = _require_positive_int(data.get("id"), field="id")
        self.admin().update_memory(data["id"], data)
        return {"ok": True}, 200

    async def memory_delete(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        mem_id = _require_positive_int(data.get("id"), field="id")
        return {"ok": self.admin().delete_memory(mem_id)}, 200

    async def memory_pin(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        mem_id = _require_positive_int(data.get("id"), field="id")
        pinned = bool(data.get("pinned", True))
        return {"ok": self.admin().set_pinned(mem_id, pinned), "pinned": pinned}, 200

    async def memory_refine(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        if not user:
            return {"error": "user is required"}, 400
        result = await self.admin().refine_memories(
            user=user,
            mode=str(data.get("mode", "")).lower(),
            limit=int(data.get("limit", 0)),
            dry_run=bool(data.get("dry_run", False)),
            include_pinned=bool(data.get("include_pinned", False)),
            extra_instruction=str(data.get("extra_instruction", "")).strip(),
            unified_msg_origin=str(data.get("unified_msg_origin", "")),
        )
        return {"ok": True, **result}, 200

    async def memory_redistill(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        if not user:
            return {"error": "user is required"}, 400
        memory_id = _require_positive_int(data.get("id"), field="id")
        try:
            result = await self.admin().redistill_memory(
                user=user,
                memory_id=memory_id,
                unified_msg_origin=str(data.get("unified_msg_origin", "") or ""),
            )
        except LookupError as exc:
            return {"error": str(exc), "category": "not_found"}, 404
        if not result.get("ok"):
            return result, _redistill_error_status(str(result.get("category", "")))
        return result, 200

    async def memory_merge(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        ids = data.get("ids", [])
        merged_text = str(data.get("memory", "") or "").strip()
        if not user or not isinstance(ids, list) or len(ids) < 2:
            return {"error": "user and ids(>=2) are required"}, 400
        result = await self.admin().merge_memories(user, ids, merged_text)
        return {"ok": True, **result}, 200

    async def memory_split(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        memory_id = _require_positive_int(data.get("id"), field="id")
        if not user:
            return {"error": "user and id are required"}, 400
        segments = data.get("segments", None)
        result = await self.admin().split_memory(
            user=user,
            memory_id=memory_id,
            segments=segments if isinstance(segments, list) else None,
            unified_msg_origin=str(data.get("unified_msg_origin", "")),
        )
        return {"ok": True, **result}, 200

    # ── 蒸馏 ────────────────────────────────────────────────────────────

    async def distill_trigger(self, request: Any) -> BridgeResult:
        result = await self.admin().trigger_distill()
        return {"ok": True, **result}, 200

    async def distill_pause(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        pause = bool(data.get("pause", True))
        self.admin().set_distill_pause(pause)
        return {"ok": True, "distill_pause": pause}, 200

    # ── 身份 ────────────────────────────────────────────────────────────

    async def identity_merge(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        from_id = str(data.get("from_user", "") or "").strip()
        to_id = str(data.get("to_user", "") or "").strip()
        if not from_id or not to_id:
            return {"error": "from_user and to_user are required"}, 400
        if from_id == to_id:
            return {"error": "两个用户 ID 相同，无需合并"}, 400
        moved = self.admin().merge_users(from_id, to_id)
        return {
            "ok": True,
            "moved": moved,
            "from_user": from_id,
            "to_user": to_id,
        }, 200

    async def identity_rebind(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        binding_id = _require_positive_int(data.get("binding_id"), field="binding_id")
        new_canonical = str(data.get("new_canonical_user_id", "") or "").strip()
        if not new_canonical:
            return {"error": "binding_id and new_canonical_user_id are required"}, 400
        self.admin().rebind_user(binding_id, new_canonical)
        return {"ok": True}, 200

    # ── 画像 ────────────────────────────────────────────────────────────

    async def profile_summary(self, request: Any) -> BridgeResult:
        return dict(self.admin().get_profile_summary(_query_str(request, "user"))), 200

    async def profile_items(self, request: Any) -> BridgeResult:
        return {
            "items": self.admin().get_profile_items(
                _query_str(request, "user"),
                _query_str(request, "facet_type"),
                _query_str(request, "status", "active"),
            )
        }, 200

    async def profile_item_evidence(self, request: Any, id: Any) -> BridgeResult:
        item_id = _require_positive_int(id, field="id")
        return {"evidence": self.admin().get_profile_item_evidence(item_id)}, 200

    async def profile_item_update(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        data["id"] = _require_positive_int(data.get("id"), field="id")
        self.admin().update_profile_item(data["id"], data)
        return {"ok": True}, 200

    async def profile_item_archive(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        item_id = _require_positive_int(data.get("id"), field="id")
        return {"ok": self.admin().archive_profile_item(item_id)}, 200

    async def profile_items_merge(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        if not user:
            return {"error": "user is required"}, 400
        ids = _require_distinct_positive_ints(data.get("ids"), field="ids")
        result = self.admin().merge_profile_items(user, ids)
        return {"ok": True, **result}, 200

    # ── 用户数据导出 / 清除 ─────────────────────────────────────────────

    async def user_export(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        if not user:
            return {"error": "user is required"}, 400
        return dict(self.admin().export_user(user)), 200

    async def user_purge(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        user = str(data.get("user", "") or "").strip()
        if not user:
            return {"error": "user is required"}, 400
        return {"ok": True, **self.admin().purge_user(user)}, 200

    # ── 数据集导出 / 导入（B6） ─────────────────────────────────────────

    async def export_data(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        users = data.get("users")
        if users is not None and not isinstance(users, list):
            return {"error": "users must be a list"}, 400
        return dict(self.admin().export_dataset(users)), 200

    async def import_data(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        payload = data.get("payload", data.get("data"))
        if payload is None:
            return {"error": "payload is required"}, 400
        try:
            result = self.admin().import_dataset(
                payload,
                dry_run=bool(data.get("dry_run", True)),
                on_conflict=str(data.get("on_conflict", "skip") or "skip"),
                backup=bool(data.get("backup", True)),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return dict(result), (200 if result.get("ok") else 409)

    # ── 配置 ────────────────────────────────────────────────────────────

    async def config_get(self, request: Any) -> BridgeResult:
        keys = [
            key.strip()
            for key in str(request.query.get("keys", "") or "").split(",")
            if key.strip()
        ]

        config_dict = asdict(self.plugin._cfg)
        config_dict.pop("capture_skip_regex", None)

        if not keys:
            return config_dict, 200
        return {key: config_dict[key] for key in keys if key in config_dict}, 200

    _PROACTIVE_KEYS = {
        "proactive_enabled",
        "proactive_interval_sec",
        "proactive_max_candidates_per_cycle",
        "proactive_per_user_window_sec",
        "proactive_per_user_max_per_window",
        "proactive_min_interval_sec",
        "proactive_daily_budget",
        "proactive_reminder_enabled",
        "proactive_recall_enabled",
        "proactive_opt_in_default",
        "proactive_max_message_chars",
    }

    async def config_update(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        _validate_config_patch(self.plugin, data)

        current_config = self.plugin.config

        proactive_patch = {}
        for key, value in data.items():
            current_config[key] = value
            if key in self._PROACTIVE_KEYS:
                proactive_patch[key] = value

        if proactive_patch:
            nested = current_config.get("proactive")
            if not isinstance(nested, dict):
                nested = {}
                current_config["proactive"] = nested
            nested.update(proactive_patch)

        saved = await _save_plugin_config(current_config)

        from ..core.config import parse_config

        self.plugin._cfg = parse_config(current_config)

        payload: Dict[str, Any] = {"status": "ok"}
        if not saved:
            _astrbot_logger.warning(
                "[tmemory-pages] 配置对象未提供 save_config/save_config_async，"
                "仅内存生效（重启后可能丢失）"
            )
        warnings = _config_warnings(self.plugin._cfg)
        if warnings:
            payload["warnings"] = warnings
        return payload, 200

    # ── Embedding Provider（bridge：SDK 无法直连核心 /api/v1/providers）──

    def _embedding_providers_payload(self) -> Dict[str, Any]:
        from ..adapters import provider as _provider_adapter

        plugin = self.plugin
        cfg = plugin._cfg
        vm = getattr(plugin, "_vector_manager", None)
        return {
            "enabled": bool(getattr(cfg, "enable_vector_search", False)),
            "configured_provider_id": str(
                getattr(cfg, "embedding_provider_id", "") or ""
            ),
            "active_source": str(getattr(vm, "source", "none") or "none"),
            "active_provider_id": str(getattr(vm, "provider_id", "") or ""),
            "active_model": str(getattr(vm, "provider_model", "") or ""),
            "active_dim": int(getattr(vm, "provider_dim", 0) or 0),
            "fallback_reason": str(getattr(vm, "fallback_reason", "") or ""),
            "providers": _provider_adapter.list_embedding_providers(
                getattr(plugin, "context", None)
            ),
        }

    async def embedding_providers(self, request: Any) -> BridgeResult:
        """枚举 AstrBot 已配置的 Embedding Provider 供前端下拉选择。"""
        return self._embedding_providers_payload(), 200

    async def embedding_provider_set(self, request: Any) -> BridgeResult:
        """写入 vector_retrieval.embedding_provider_id 并 best-effort 重初始化。"""
        data = await _json_object(request)
        provider_id = str(data.get("provider_id", "") or "").strip()

        provider_ids = {
            str(item.get("id", ""))
            for item in self._embedding_providers_payload()["providers"]
        }
        if provider_id and provider_id not in provider_ids:
            return {"error": f"embedding provider id not found: {provider_id}"}, 400

        current_config = self.plugin.config
        vector_cfg = current_config.get("vector_retrieval")
        if not isinstance(vector_cfg, dict):
            vector_cfg = {}
            current_config["vector_retrieval"] = vector_cfg
        vector_cfg["embedding_provider_id"] = provider_id
        await _save_plugin_config(current_config)

        from ..core.config import parse_config

        self.plugin._cfg = parse_config(current_config)
        await self._reinit_vector_manager()

        payload = self._embedding_providers_payload()
        payload["ok"] = True
        return payload, 200

    async def _reinit_vector_manager(self) -> None:
        """best-effort：切换 Provider 后重建 VectorManager（失败仅告警）。"""
        plugin = self.plugin
        if not bool(getattr(plugin._cfg, "enable_vector_search", False)):
            return
        try:
            from ..vector_manager import VectorManager

            vm = VectorManager(
                plugin.db_path, plugin._get_vector_retrieval_config_from_cfg()
            )
            vm.context = getattr(plugin, "context", None)
            await vm.initialize()
            previous = getattr(plugin, "_vector_manager", None)
            plugin._vector_manager = vm
            if previous is not None and previous is not vm:
                try:
                    await previous.close()
                except Exception as close_exc:  # noqa: BLE001
                    _astrbot_logger.warning(
                        "[tmemory-pages] previous VectorManager close failed: %s",
                        close_exc,
                    )
        except Exception as exc:  # noqa: BLE001 - 切换失败仅告警
            _astrbot_logger.warning(
                "[tmemory-pages] VectorManager re-init after provider change failed: %s",
                exc,
            )

    # ── 测试对话 ────────────────────────────────────────────────────────

    async def test_conversation(self, request: Any) -> BridgeResult:
        data = await _json_object(request)
        result = await self.admin().insert_test_conversation(
            user_id=str(data.get("user_id", "") or "").strip(),
            role=str(data.get("role", "user") or "user").strip().lower(),
            content=str(data.get("content", "") or "").strip(),
            source_adapter=str(data.get("source_adapter", "") or "").strip(),
            source_user_id=str(data.get("source_user_id", "") or "").strip(),
            unified_msg_origin=str(data.get("unified_msg_origin", "") or "").strip(),
            scope=str(data.get("scope", "user") or "user").strip(),
            persona_id=str(data.get("persona_id", "") or "").strip(),
        )
        if result.get("ok"):
            return dict(result), 200
        return dict(result), 400


__all__ = [
    "PLUGIN_NAME",
    "BridgeRoute",
    "PluginPagesBridge",
    "build_bridge_payload",
]
