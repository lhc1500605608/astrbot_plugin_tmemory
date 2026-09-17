"""Config/path port — AstrBot 数据目录与配置对象的边界封装。"""

from __future__ import annotations

from typing import Optional


def get_astrbot_data_path() -> Optional[str]:
    """AstrBot 数据根目录（用于解析插件持久化路径）。

    上游路径: astrbot.core.utils.astrbot_path.get_astrbot_data_path（内部工具）
    引入版本: 4.16–4.28 语义未变（4.28 另增 builtin_plugin/workspaces/system_tmp 路径）
    降级策略: import/异常返回 None，调用方退回 cwd/data/plugin_data
    """
    try:
        from astrbot.core.utils.astrbot_path import (  # type: ignore
            get_astrbot_data_path as _get_data_path,
        )

        return str(_get_data_path())
    except Exception:
        return None
