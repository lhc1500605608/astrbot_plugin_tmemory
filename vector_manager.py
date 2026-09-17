"""
向量检索辅助管理器

负责：
- 初始化 Embedding 提供者（优先 AstrBot Provider，缺失/异常回退独立配置）
- 解析可选的 AstrBot Rerank Provider

配置（``vector_retrieval``）：
- ``embedding_source``: ``provider``（默认，优先平台 Provider）| ``standalone``
- ``embedding_provider_id``: 指定 AstrBot Embedding Provider；留空自动选第一个
- ``rerank_provider_id``: 指定 AstrBot Rerank Provider；留空不启用
- 其余 ``embedding_*`` 键为 standalone 回退配置（v0.10.0 行为）
"""

import logging
from typing import Any, Dict, Optional

from . import adapters as _adapters
from .embeddingProvider import (
    BaseEmbeddingProvider,
    LocalBgeEmbeddingProvider,
    VolcEmbeddingsProvider,
    OpenAIEmbeddingProvider,
)

logger = logging.getLogger("VectorManager")


class VectorManager:
    """向量检索辅助管理器（Embedding / Rerank 提供者）"""

    def __init__(self, db_path: str, config: dict, context: Any = None):
        self.db_path = db_path
        self.config = config
        self.context = context
        self.embedding_provider: Optional[BaseEmbeddingProvider] = None
        self.rerank_provider: Optional[Any] = None
        self.embedding_source: str = "provider"
        self.source: str = "none"
        self.provider_id: str = ""
        self.provider_model: str = ""
        self.provider_dim: int = 0
        self.fallback_reason: str = ""

    async def initialize(self):
        """初始化 Embedding / Rerank 提供者"""
        await self._init_embedding_provider(self.config)
        self._init_rerank_provider(self.config)

    async def _init_embedding_provider(self, config: dict):
        """初始化 Embedding 提供者：Provider / local 优先，回退 standalone。"""
        source = str(config.get("embedding_source", "provider") or "provider").strip().lower()
        if source not in ("provider", "standalone", "local"):
            source = "provider"
        self.embedding_source = source

        if source == "local":
            self._init_local_provider(config)
            if self.embedding_provider is not None:
                self.source = "local"
                self.provider_model = getattr(self.embedding_provider, "model_name", "")
                self.provider_dim = self.embedding_provider.get_dim()
                logger.info(
                    "[VectorManager] embedding source=local path=%s dim=%s",
                    config.get("local_embedding_path", ""),
                    self.provider_dim,
                )
            else:
                if not self.fallback_reason:
                    self.fallback_reason = "local embedding unavailable"
                logger.info(
                    "[VectorManager] embedding source=local unavailable (%s), "
                    "fallback=standalone",
                    self.fallback_reason,
                )

        if source == "provider":
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
                if not self.fallback_reason:
                    self.fallback_reason = "no available AstrBot embedding provider"
                logger.info(
                    "[VectorManager] embedding source=provider unavailable (%s), "
                    "fallback=standalone",
                    self.fallback_reason,
                )

        if self.embedding_provider is None:
            await self._init_standalone_provider(config)
            self.source = "standalone" if self.embedding_provider else "none"
            if self.embedding_provider is not None:
                logger.info(
                    "[VectorManager] embedding source=standalone type=%s model=%s",
                    config.get("embedding_provider", ""),
                    config.get("embedding_model", ""),
                )

    def _init_local_provider(self, config: dict):
        """初始化本地 bge-small-zh-v1.5 ONNX 提供者；不可用返回 None（不抛异常）。"""
        try:
            path = str(config.get("local_embedding_path", "data/bge-small-zh-v1.5") or "").strip()
            model_file = str(config.get("local_embedding_model_file", "model_quantized.onnx") or "").strip()
            max_length = int(config.get("local_embedding_max_length", 512) or 512)
            self.embedding_provider = LocalBgeEmbeddingProvider(
                path, model_file or "model_quantized.onnx", max_length
            )
        except Exception as e:
            self.embedding_provider = None
            self.fallback_reason = f"local embedding init failed: {e}"
            logger.warning("[VectorManager] local embedding init failed: %s", e)


    async def _init_standalone_provider(self, config: dict):
        """初始化独立配置的 Embedding 提供者（v0.10.0 回滚路径）。"""
        provider_type = config.get("embedding_provider", "volc")
        api_key = config.get("embedding_api_key", "")
        model = config.get("embedding_model", "")
        base_url = config.get("embedding_base_url", "")

        try:
            if provider_type == "volc":
                if not api_key:
                    logger.warning("[VectorManager] Embedding API key not configured")
                    return
                if not model:
                    model = "doubao-embedding-vision-251215"
                self.embedding_provider = VolcEmbeddingsProvider(api_key, model)

            elif provider_type == "openai":
                if not api_key:
                    logger.warning("[VectorManager] Embedding API key not configured")
                    return
                if not model:
                    model = "text-embedding-3-small"
                if not base_url:
                    base_url = "https://api.openai.com/v1"
                self.embedding_provider = OpenAIEmbeddingProvider(api_key, model, base_url)

            else:
                logger.warning(f"[VectorManager] Unknown embedding provider: {provider_type}")
                self.embedding_provider = None

        except Exception as e:
            logger.error(f"[VectorManager] Failed to init embedding provider: {e}")
            self.embedding_provider = None

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
                "[VectorManager] rerank provider id=%s unavailable, fallback to standalone",
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
            "standalone_provider": self.config.get("embedding_provider", ""),
            "local_embedding_path": self.config.get("local_embedding_path", ""),
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
