"""LLM port — 对 AstrBot ProviderRequest/内容部件内部结构的访问封装。"""

from __future__ import annotations

import logging
from typing import Any

from . import version as _version

logger = logging.getLogger("astrbot")


async def generate(plugin: Any, **kwargs):
    """One plugin-owned gateway to AstrBot; never mutate a shared Provider."""
    legacy_model = kwargs.pop("model_id", None)
    if legacy_model:
        kwargs.setdefault("model", legacy_model)
    from ..core.runtime_reliability import reserve, settle
    import asyncio
    if getattr(plugin, "_terminating", False):
        raise RuntimeError("plugin is stopping")
    reservation = reserve(plugin, kwargs)
    tasks = getattr(plugin, "_inflight_llm_tasks", None)
    if tasks is None:
        tasks = plugin._inflight_llm_tasks = set()

    async def request():
        try:
            try:
                response = await plugin.context.llm_generate(**kwargs)
            except BaseException:
                settle(plugin, reservation, error=True)
                raise
            settle(plugin, reservation, response)
            return response
        finally:
            tasks.discard(asyncio.current_task())

    task = asyncio.create_task(request())
    tasks.add(task)
    return await task


def inject_extra_user_temp(req: Any, block: str) -> bool:
    """把 memory block 作为「临时用户内容部件」追加到 ProviderRequest。

    上游路径: astrbot.core.agent.message.TextPart（内部） +
              TextPart.mark_as_temp()（4.28 起新增） +
              ProviderRequest.extra_user_content_parts（公共，4.28 未变）
    引入版本: mark_as_temp 仅 >=4.28；该位置在 4.16–4.27 不存在
    降级策略: 能力探测不可用（或运行期异常）返回 False，调用方回退到
              system_prompt 注入并显式告警/UI 提示（不再静默降级）
    """
    # 能力探测门槛：4.16–4.27 直接判定不可用，避免仅靠 getattr 静默跳过。
    if not _version.has_mark_as_temp():
        return False
    try:
        from astrbot.core.agent.message import TextPart  # type: ignore

        part = TextPart(text=block)
        mark_fn = getattr(part, "mark_as_temp", None)
        if not callable(mark_fn):
            return False
        mark_fn()
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            req.extra_user_content_parts = []
            parts = req.extra_user_content_parts
        parts.append(part)
        return True
    except Exception:
        return False
