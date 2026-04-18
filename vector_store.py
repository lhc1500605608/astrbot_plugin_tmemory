"""
向量存储抽象层和实现。

支持：
- 抽象基类 BaseVectorStore
- sqlite-vec 实现（封装现有 hybrid_search.py）
- Qdrant 实现（主向量存储）
"""

from abc import abstractmethod
from typing import List, Dict, Optional
import sqlite3
import logging

logger = logging.getLogger("VectorStore")

class BaseVectorStore:
    """向量存储抽象基类"""
    
    def __init__(self, vector_dim: int):
        self.vector_dim = vector_dim
        self.available = False
    
    @abstractmethod
    async def add(self, id: str, vector: List[float], metadata: dict) -> bool:
        """添加向量到存储
        
        Args:
            id: 唯一标识符
            vector: 向量数据（浮点数组）
            metadata: 元数据字典（user_id, session_id, canonical_user_id 等）
        
        Returns:
            bool: 成功返回 True，失败返回 False
        """
        pass
    
    @abstractmethod
    async def search(self, query_vector: List[float], top_k: int, 
                 filter_dict: dict = None) -> List[dict]:
        """相似性搜索
        
        Args:
            query_vector: 查询向量
            top_k: 返回结果数量
            filter_dict: Metadata 过滤条件（例如 {"user_id": "xxx"}）
        
        Returns:
            List[dict]: 搜索结果列表，每项包含：
                - id: 记忆 ID
                - score: 相似度分数（0-1，越高越相似）
                - metadata: 元数据字典
        """
        pass
    
    @abstractmethod
    async def delete(self, id: str) -> bool:
        """删除指定 ID 的向量"""
        pass
    
    @abstractmethod
    async def close(self):
        """关闭连接，释放资源"""
        pass
    
    @abstractmethod
    async def rebuild(self, vector_dim: int) -> bool:
        """重建向量库（维度变更时）
        
        Args:
            vector_dim: 新的向量维度
        
        Returns:
            bool: 成功返回 True，失败返回 False
        """
        pass


class SqliteVecVectorStore(BaseVectorStore):
    """sqlite-vec 向量存储实现（封装现有 hybrid_search.py）"""
    
    def __init__(self, conn: sqlite3.Connection, vector_dim: int, 
                 table_name: str = "memory_vectors"):
        self.conn = conn
        self.vector_dim = vector_dim
        self.table_vec = table_name
        self.available = True
        
        # 导入现有实现
        try:
            from . import hybrid_search
            
            self._knn_retriever = hybrid_search.SQLiteVecKNNRetriever(conn, vector_dim, table_name)
            logger.info(f"Initialized SqliteVecVectorStore with dim={vector_dim}")
        except Exception as e:
            logger.error(f"Failed to import hybrid_search: {e}")
            self.available = False
    
    async def add(self, id: str, vector: List[float], metadata: dict) -> bool:
        """添加向量到 sqlite-vec"""
        if not self.available:
            logger.warning("sqlite-vec store not available")
            return False
        
        try:
            vec_bytes = self._serialize_vector(vector)
            cursor = self.conn.cursor()
            
            # 插入向量到 memory_vectors 表
            cursor.execute(
                f"INSERT INTO {self.table_vec} (memory_id, embedding) VALUES (?, ?)",
                (id, vec_bytes)
            )
            
            # 如果有 metadata，更新 metadata 表
            if metadata:
                # 这里简化实现，实际需要更复杂的 metadata 处理
                # 使用现有的 memories 表存储其他字段
                pass
            
            self.conn.commit()
            logger.debug(f"Added vector: {id}")
            return True
        except Exception as e:
            logger.error(f"Failed to add vector: {e}")
            return False
    
    def _serialize_vector(self, vector: List[float]) -> bytes:
        """序列化向量为 bytes（numpy 序列化）"""
        import numpy as np
        return np.array(vector, dtype=np.float32).tobytes()
    
    async def search(self, query_vector: List[float], top_k: int, 
                 filter_dict: dict = None) -> List[dict]:
        """向量搜索（使用现有的 KNN 实现）"""
        if not self.available:
            return []
        
        try:
            results = self._knn_retriever.search_knn(query_vector, top_k)
            
            formatted = []
            for item in results:
                formatted.append({
                    "id": item["id"],
                    "score": item["score"],
                    "metadata": {}  # TODO: 从 memories 表获取完整 metadata
                })
            
            return formatted
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []
    
    async def delete(self, id: str) -> bool:
        """删除向量"""
        if not self.available:
            return False
        
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                f"DELETE FROM {self.table_vec} WHERE memory_id = ?",
                (id,)
            )
            self.conn.commit()
            logger.debug(f"Deleted vector: {id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete vector: {e}")
            return False
    
    async def close(self):
        """关闭连接"""
        # sqlite-vec 的连接由外部管理，这里不需要特殊处理
        pass
    
    async def rebuild(self, vector_dim: int) -> bool:
        """重建 sqlite-vec 向量库"""
        try:
            # 删除旧的向量表
            cursor = self.conn.cursor()
            cursor.execute(f"DROP TABLE IF EXISTS {self.table_vec}")
            
            # 重新创建表（使用新的维度）
            cursor.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.table_vec} "
                f"USING sqlite_vec(embedding_float_array({vector_dim}))"
            )
            
            self.conn.commit()
            self.vector_dim = vector_dim
            logger.info(f"Rebuilt vector store with new dimension: {vector_dim}")
            return True
        except Exception as e:
            logger.error(f"Failed to rebuild vector store: {e}")
            return False


class QdrantVectorStore(BaseVectorStore):
    """Qdrant 向量存储实现"""
    
    def __init__(self, url: str = "http://localhost:6333", 
                 collection_name: str = "astrbot_memory", 
                 vector_dim: int = 2048, api_key: str = ""):
        self.url = url
        self.collection_name = collection_name
        self.vector_dim = vector_dim
        self.api_key = api_key
        self.client = None
        self.available = False
    
    async def connect(self) -> bool:
        """连接到 Qdrant"""
        try:
            from qdrant_client import QdrantClient, models
            
            if self.api_key:
                self.client = QdrantClient(
                    url=self.url,
                    api_key=self.api_key,
                    timeout=30
                )
            else:
                self.client = QdrantClient(
                    url=self.url,
                    timeout=30
                )
            
            # 检查并创建 Collection
            collections = self.client.get_collections()
            if not any(c.name == self.collection_name for c in collections):
                self.create_collection()
            
            self.available = True
            logger.info(f"Connected to Qdrant: {self.url}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Qdrant: {e}")
            self.client = None
            self.available = False
            return False
    
    def create_collection(self):
        """创建向量 Collection"""
        try:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.vector_dim,
                    distance=models.Distance.COSINE
                )
            )
            logger.info(f"Created collection: {self.collection_name}")
        except Exception as e:
            logger.error(f"Failed to create collection: {e}")
    
    async def add(self, id: str, vector: List[float], metadata: dict) -> bool:
        """添加向量到 Qdrant"""
        if not self.available or not self.client:
            logger.warning("Qdrant client not available")
            return False
        
        try:
            self.client.upsert(
                collection_name=self.collection_name,
                points=[
                    models.PointStruct(
                        id=id,
                        vector=vector,
                        payload=metadata
                    )
                ]
            )
            logger.debug(f"Added vector: {id}")
            return True
        except Exception as e:
            logger.error(f"Failed to upsert point {id}: {e}")
            return False
    
    async def search(self, query_vector: List[float], top_k: int, 
                 filter_dict: dict = None) -> List[dict]:
        """相似性搜索（支持 Metadata 过滤）"""
        if not self.available or not self.client:
            return []
        
        try:
            search_filter = self._build_filter(filter_dict) if filter_dict else None
            
            results = self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                query_filter=search_filter,
                limit=top_k,
                with_payload=True
            )
            
            formatted = []
            for hit in results:
                formatted.append({
                    "id": hit.id,
                    "score": hit.score,
                    "metadata": hit.payload
                })
            
            return formatted
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []
    
    def _build_filter(self, filter_dict: dict):
        """构建 Metadata 过滤器"""
       
        
        if not filter_dict:
            return None
        
        from qdrant_client import models
        
        conditions = []
        for key, value in filter_dict.items():
            if key in ["user_id", "session_id", "canonical_user_id"]:
                conditions.append(
                    models.FieldCondition(
                        key=key,
                        match=models.MatchValue(value=value)
                    )
                )
        
        if conditions:
            return models.Filter(must=conditions)
        return None
    
    async def delete(self, id: str) -> bool:
        """删除指定 ID 的向量"""
        if not self.available or not self.client:
            return False
        
        try:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(point_ids=[id])
            )
            logger.debug(f"Deleted vector: {id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete point {id}: {e}")
            return False
    
    async def close(self):
        """关闭连接"""
        # Qdrant 客户端是同步的，不需要特殊清理
        self.client = None
        logger.info("Qdrant connection closed")
    
    async def rebuild(self, vector_dim: int) -> bool:
        """重建 Qdrant Collection（维度变更时）"""
        try:
            # 删除旧的 Collection
            collections = self.client.get_collections()
            if any(c.name == self.collection_name for c in collections):
                self.client.delete_collection(self.collection_name)
            
            # 重新创建 Collection（使用新的维度）
            self.vector_dim = vector_dim
            self.create_collection()
            
            logger.info(f"Rebuilt Qdrant collection with new dimension: {vector_dim}")
            return True
        except Exception as e:
            logger.error(f"Failed to rebuild Qdrant collection: {e}")
            return False