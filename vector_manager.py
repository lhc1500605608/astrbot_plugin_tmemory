"""
向量检索与降级管理器

负责：
- 初始化主向量存储和降级存储
- 实现降级策略（主存储失败时自动切换）
- 向量维度变更时的重建逻辑
"""

import asyncio
import hashlib
import logging
from typing import Optional, List, Dict, Any
import os

from .vector_store import BaseVectorStore, SqliteVecVectorStore, QdrantVectorStore
from .embeddingProvider import BaseEmbeddingProvider, VolcEmbeddingsProvider, OpenAIEmbeddingProvider

logger = logging.getLogger("VectorManager")

class VectorManager:
    """向量检索与降级管理器"""
    
    def __init__(self, db_path: str, config: dict):
        self.db_path = db_path
        self.config = config
        
        # 主向量存储
        self.primary_store: Optional[BaseVectorStore] = None
        self.primary_store_type: Optional[str] = None
        
        # 降级存储
        self.fallback_store: Optional[BaseVectorStore] = None
        self.fallback_store_type: Optional[str] = None
        
        # Embedding 提供者
        self.embedding_provider: Optional[BaseEmbeddingProvider] = None
        
        # 向量维度（用于检测维度变更）
        self.current_vector_dim: Optional[int] = None
        self._load_task: Optional[asyncio.Task] = None
        
    async def initialize(self):
        """初始化向量存储和 Embedding 提供者"""
        vector_retieval_config = self.config.get("vector_retrieval", {})
        
        # 获取向量维度配置
        vector_dim_config = int(vector_retieval_config.get("vector_dim", 2048))
        self.current_vector_dim = vector_dim_config
        
        # 初始化 Embedding 提供者
        await self._init_embedding_provider(vector_retieval_config)
        
        # 初始化主向量存储和降级存储
        await self._init_vector_stores(vector_retieval_config, vector_dim_config)
        
        # 检测向量维度变更，触发重建
        if await self._detect_dimension_change():
            logger.info(f"Vector dimension changed, triggering rebuild...")
            await self._rebuild_all_vector_stores(vector_dim_config)
    
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
                
                # 如果没有指定模型，使用默认值
                if not model:
                    model = "doubao-embedding-vision-251215"
                
                self.embedding_provider = VolcEmbeddingsProvider(api_key, model)
                logger.info("[VectorManager] Using Volc Embeddings provider")
                
            elif provider_type == "openai":
                if not api_key:
                    logger.warning("[VectorManager] Embedding API key not configured")
                    return
                
                # 如果没有指定模型，使用默认值
                if not model:
                    model = "text-embedding-3-small"
                
                # 如果没有指定 base_url，使用默认值
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
    
    async def _init_vector_stores(self, config: dict, vector_dim: int):
        """初始化向量存储（主 + 降级）"""
        backend_type = config.get("vector_backend", "qdrant")
        
        # 初始化 Qdrant（主存储）
        try:
            if backend_type == "qdrant":
                url = config.get("qdrant_url", "http://localhost:6333")
                api_key = config.get("qdrant_api_key", "")
                collection = config.get("qdrant_collection", "astrbot_memory")
                
                self.primary_store_type = "qdrant"
                self.primary_store = QdrantVectorStore(url, collection, vector_dim, api_key)
                
                if await self.primary_store.connect():
                    logger.info("[VectorManager] Primary store (Qdrant) connected successfully")
                else:
                    logger.warning("[VectorManager] Failed to connect to Qdrant, will use fallback")
                    raise Exception("Primary store connection failed")
                    
        except Exception as e:
            logger.error(f"[VectorManager] Failed to init Qdrant: {e}")
            self.primary_store = None
            self.primary_store_type = None
        
        # 初始化 sqlite-vec（降级存储）
        try:
            self.fallback_store_type = "sqlite-vec"
            # 封装现有的 hybrid_search.py 实现
            # 注意：这里需要导入现有的 sqlite-vec 实现
            from .hybrid_search import HybridMemorySystem
            
            # sqlite-vec 需要通过 HybridMemorySystem 访问
            # 这里暂时使用 None，稍后在 main.py 中集成
            self.fallback_store = None
            logger.info("[VectorManager] Fallback store (sqlite-vec) initialized")
            
        except Exception as e:
            logger.error(f"[VectorManager] Failed to init sqlite-vec fallback: {e}")
            self.fallback_store = None
            self.fallback_store_type = None
    
    async def _detect_dimension_change(self) -> bool:
        """检测向量维度是否变更"""
        if not self.current_vector_dim:
            return False
        
        vector_retieval_config = self.config.get("vector_retrieval", {})
        new_dim = int(vector_retieval_config.get("vector_dim", 2048))
        
        return new_dim != self.current_vector_dim
    
    async def _rebuild_all_vector_stores(self, new_dim: int):
        """重建所有向量存储"""
        logger.info(f"[VectorManager] Rebuilding vector stores with dimension: {new_dim}")
        
        rebuild_tasks = []
        
        if self.primary_store and self.primary_store.available:
            rebuild_tasks.append(self.primary_store.rebuild(new_dim))
        
        if self.fallback_store and self.fallback_store.available:
            rebuild_tasks.append(self.fallback_store.rebuild(new_dim))
        
        # 等待所有重建任务完成
        if rebuild_tasks:
            await asyncio.gather(*rebuild_tasks)
        
        # 更新当前维度
        self.current_vector_dim = new_dim
        logger.info("[VectorManager] Vector stores rebuilt successfully")
    
    def get_active_store(self) -> Optional[BaseVectorStore]:
        """获取当前可用的向量存储（优先主存储，失败时使用降级）"""
        if self.primary_store and self.primary_store.available:
            return self.primary_store
        elif self.fallback_store and self.fallback_store.available:
            return self.fallback_store
        return None
    
    def get_embedding_provider(self) -> Optional[BaseEmbeddingProvider]:
        """获取当前 Embedding 提供者"""
        return self.embedding_provider
    
    def get_vector_dim(self) -> Optional[int]:
        """获取当前向量维度"""
        return self.current_vector_dim
    
    async def add_vector_memory(self, id: str, vector: List[float], metadata: dict) -> bool:
        """添加向量记忆（使用当前可用的存储）"""
        store = self.get_active_store()
        if not store or not self.embedding_provider:
            return False
        
        try:
            return await store.add(id, vector, metadata)
        except Exception as e:
            logger.error(f"[VectorManager] Failed to add vector: {e}")
            return False
    
    async def search_vector_memories(self, query_vector: List[float], top_k: int = 5, 
                                   filter_dict: dict = None) -> List[dict]:
        """向量检索记忆（使用当前可用的存储）"""
        store = self.get_active_store()
        if not store or not self.embedding_provider:
            return []
        
        try:
            return await store.search(query_vector, top_k, filter_dict)
        except Exception as e:
            logger.error(f"[VectorManager] Failed to search vectors: {e}")
            return []
    
    async def delete_vector_memory(self, id: str) -> bool:
        """删除向量记忆"""
        store = self.get_active_store()
        if not store:
            return False
        
        try:
            return await store.delete(id)
        except Exception as e:
            logger.error(f"[VectorManager] Failed to delete vector: {e}")
            return False
    
    async def close(self):
        """关闭所有连接和资源"""
        logger.info("[VectorManager] Closing vector stores and embeddings provider")
        
        # 关闭 Embedding 提供者
        if self.embedding_provider:
            # Embedding 提供者的关闭逻辑需要实现
            # OpenAI/Volc 使用 aiohttp.Session，需要在 terminate 时关闭
            pass
        
        # 关闭向量存储
        if self.primary_store:
            await self.primary_store.close()
        
        if self.fallback_store:
            await self.fallback_store.close()
        
        logger.info("[VectorManager] All resources closed")