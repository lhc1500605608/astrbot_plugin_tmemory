"""
向量检索辅助管理器

负责：
- 初始化 Embedding 提供者（仅使用 AstrBot 已配置的 Embedding Provider）
- 解析可选的 AstrBot Rerank Provider

配置（``vector_retrieval``，provider-only）：
- ``embedding_provider_id``: 指定 AstrBot Embedding Provider；留空自动选第一个
- ``rerank_provider_id``: 指定 AstrBot Rerank Provider；留空不启用

``embedding_source`` 已收敛为 ``provider``；旧值（``standalone`` / ``local``）
由 ``core.config.parse_config`` 归一化并告警，不再驱动本模块。
"""

import logging
import time
from typing import Any, Dict, Optional

from . import adapters as _adapters

logger = logging.getLogger("VectorManager")


class VectorManager:
    """向量检索辅助管理器（Embedding / Rerank 提供者）"""

    def __init__(self, db_path: str, config: dict, context: Any = None):
        self.db_path = db_path
        self.config = config
        self.context = context
        self.embedding_provider: Optional[Any] = None
        self.rerank_provider: Optional[Any] = None
        self.embedding_source: str = "provider"
        self.source: str = "none"
        self.provider_id: str = ""
        self.provider_model: str = ""
        self.provider_dim: int = 0
        self.fallback_reason: str = ""
        # 上次解析尝试时刻（monotonic）；0.0 表示允许立即再次尝试。
        self._last_resolve_ts: float = 0.0

    async def initialize(self):
        """初始化 Embedding / Rerank 提供者"""
        await self._init_embedding_provider(self.config)
        self._init_rerank_provider(self.config)
        # provider 未就绪时保持 0.0，让首次使用可立即惰性补解析（TMEAAA-472）。
        if self.embedding_provider is not None:
            self._last_resolve_ts = time.monotonic()

    async def refresh(
        self, min_interval_sec: float = 0.0, force: bool = False
    ) -> bool:
        """provider 就绪后补解析（幂等）；返回 embedding provider 是否可用。

        AstrBot 4.28 冷启动会先加载插件、后实例化 provider，``initialize`` 时
        provider 列表为空；``OnAstrBotLoadedEvent`` 或首次 embed 时调用本方法补解析。

        ``min_interval_sec`` 用于热路径节流：距上次尝试不足该间隔时直接跳过，
        避免 provider 长期缺失时每次 embed 都重复探测并重复告警。
        ``force=True`` 时忽略节流并丢弃当前实例重新解析（TMEAAA-510：插件重载后
        旧 provider 的 httpx client 已关闭，必须重新拿到新实例）。
        """
        now = time.monotonic()
        if (
            not force
            and min_interval_sec > 0
            and (now - self._last_resolve_ts) < min_interval_sec
        ):
            return self.embedding_provider is not None
        self._last_resolve_ts = now
        if force or self.embedding_provider is None:
            if force:
                self.embedding_provider = None
            self.fallback_reason = ""
            await self._init_embedding_provider(self.config)
        if self.rerank_provider is None:
            self._init_rerank_provider(self.config)
        return self.embedding_provider is not None

    async def _init_embedding_provider(self, config: dict):
        """初始化 Embedding 提供者：仅解析 AstrBot Provider，缺失则降级为 none。"""
        self.embedding_source = "provider"
        try:
            provider = _adapters.provider.resolve_embedding_provider(
                self.context, str(config.get("embedding_provider_id", "") or "")
            )
        except Exception as e:  # 探测/解析绝不应阻断启动
            provider = None
            self.fallback_reason = f"provider resolve error: {e}"
            logger.warning("[VectorManager] embedding provider resolve failed: %s", e)

        if provider is not None:
            self.embedding_provider = provider
            self.source = "provider"
            self.fallback_reason = ""
            self.provider_id = getattr(provider, "provider_id", "")
            self.provider_model = getattr(provider, "model_name", "")
            self.provider_dim = provider.get_dim()
            logger.info(
                "[VectorManager] embedding source=provider id=%s model=%s dim=%s",
                self.provider_id or "<auto>",
                self.provider_model or "<unknown>",
                self.provider_dim,
            )
        else:
            self.source = "none"
            if not self.fallback_reason:
                self.fallback_reason = "no available AstrBot embedding provider"
            logger.info(
                "[VectorManager] embedding source=provider unavailable (%s)",
                self.fallback_reason,
            )

    def _init_rerank_provider(self, config: dict):
        """解析可选的 AstrBot Rerank Provider（失败仅告警，不影响嵌入）。"""
        provider_id = str(config.get("rerank_provider_id", "") or "")
        if not provider_id:
            return
        try:
            self.rerank_provider = _adapters.provider.resolve_rerank_provider(
                self.context, provider_id
            )
        except Exception as e:
            self.rerank_provider = None
            logger.warning("[VectorManager] rerank provider resolve failed: %s", e)
        if self.rerank_provider is not None:
            logger.info(
                "[VectorManager] rerank source=provider id=%s",
                getattr(self.rerank_provider, "provider_id", provider_id),
            )
        else:
            logger.info(
                "[VectorManager] rerank provider id=%s unavailable, fallback to HTTP",
                provider_id,
            )

    def status(self) -> Dict[str, object]:
        """返回 embedding/rerank 来源快照（供日志 / UI / 命令展示）。"""
        return {
            "embedding_source": self.embedding_source,
            "active_source": self.source,
            "provider_id": self.provider_id,
            "provider_model": self.provider_model,
            "provider_dim": self.provider_dim,
            "fallback_reason": self.fallback_reason,
            "rerank_source": "provider" if self.rerank_provider is not None else "disabled",
            "rerank_provider_id": getattr(self.rerank_provider, "provider_id", ""),
        }

    async def close(self):
        """关闭资源"""
        if self.embedding_provider is not None:
            close = getattr(self.embedding_provider, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception as e:
                    logger.warning("[VectorManager] embedding provider close error: %s", e)
        self.embedding_provider = None
        self.rerank_provider = None
        logger.info("[VectorManager] Closed")
