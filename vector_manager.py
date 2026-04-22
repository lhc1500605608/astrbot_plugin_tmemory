"""
向量检索辅助管理器

负责：
- 初始化 Embedding 提供者
"""

import logging
from typing import Optional

from .embeddingProvider import BaseEmbeddingProvider, VolcEmbeddingsProvider, OpenAIEmbeddingProvider

logger = logging.getLogger("VectorManager")


class VectorManager:
    """向量检索辅助管理器（仅管理 Embedding 提供者）"""

    def __init__(self, db_path: str, config: dict):
        self.db_path = db_path
        self.config = config
        self.embedding_provider: Optional[BaseEmbeddingProvider] = None

    async def initialize(self):
        """初始化 Embedding 提供者"""
        vector_retrieval_config = self.config.get("vector_retrieval", {})
        await self._init_embedding_provider(vector_retrieval_config)

    async def _init_embedding_provider(self, config: dict):
        """初始化 Embedding 提供者"""
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
                logger.info("[VectorManager] Using Volc Embeddings provider")

            elif provider_type == "openai":
                if not api_key:
                    logger.warning("[VectorManager] Embedding API key not configured")
                    return
                if not model:
                    model = "text-embedding-3-small"
                if not base_url:
                    base_url = "https://api.openai.com/v1"
                self.embedding_provider = OpenAIEmbeddingProvider(api_key, model, base_url)
                logger.info("[VectorManager] Using OpenAI Embeddings provider")

            else:
                logger.warning(f"[VectorManager] Unknown embedding provider: {provider_type}")
                self.embedding_provider = None

        except Exception as e:
            logger.error(f"[VectorManager] Failed to init embedding provider: {e}")
            self.embedding_provider = None

    async def close(self):
        """关闭资源"""
        self.embedding_provider = None
        logger.info("[VectorManager] Closed")
