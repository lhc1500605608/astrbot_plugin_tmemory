"""
Embedding 提供者抽象层和实现。

支持：
- 抽象基类 BaseEmbeddingProvider
- 火山方舟 Embedding 提供者（VolcEmbeddingsProvider）
- OpenAI 兼容 Embedding 提供者（OpenAIEmbeddingProvider）
"""

from abc import ABC, abstractmethod
from typing import List
import aiohttp
import logging

logger = logging.getLogger("EmbeddingProvider")

class BaseEmbeddingProvider(ABC):
    """Embedding 提供者抽象基类"""
    
    def __init__(self):
        pass

    async def close(self) -> None:
        """关闭底层 HTTP 会话（如有）。子类按需覆盖。"""
        pass

    @abstractmethod
    async def embed_text(self, text: str) -> List[float]:
        """将文本向量化为向量
        
        Args:
            text: 输入文本
        
        Returns:
            List[float]: 向量数组（浮点数）
        
        Raises:
            Exception: 向量化失败时抛出
        """
        pass
    
    @abstractmethod
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量文本向量化
        
        Args:
            texts: 文本列表
        
        Returns:
            List[List[float]]: 向量列表（每项是一个向量）
        
        Raises:
            Exception: 批量向量化失败时抛出
        """
        pass


class VolcEmbeddingsProvider(BaseEmbeddingProvider):
    """火山方舟多模态 Embedding 提供者"""
    
    def __init__(self, api_key: str, model: str = "doubao-embedding-vision-251215"):
        self.api_key = api_key
        self.model = model
        self.api_url = "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal"
        self._session = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """关闭底层 HTTP 会话，释放连接资源。"""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as e:
                logger.warning("VolcEmbeddingsProvider.close() error: %s", e)
            self._session = None

    async def embed_text(self, text: str) -> List[float]:
        """向量化单条文本"""
        try:
            session = await self._get_session()
            payload = {
                "model": self.model,
                "input": [{"type": "text", "text": text}]
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            
            async with session.post(self.api_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Volc API error: status={resp.status}")
                    raise Exception(f"Volc API returned {resp.status}")
                
                data = await resp.json()
                # 火山方舟 API 返回格式
                # data 是一个字典，包含 "code" 和 "message" 字段
                # 实际的 embedding 在 data.data 字段中，data.data 是一个数组
                if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                    return data["data"][0].get("embedding", [])
                else:
                    logger.warning(f"Volc API unexpected response format: {data}")
                    raise Exception("Invalid Volc API response")
                    
        except Exception as e:
            logger.warning(f"Volc embedding failed for text '{text[:50]}...': {e}")
            raise
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量向量化文本"""
        try:
            session = await self._get_session()
            inputs = [{"type": "text", "text": t} for t in texts]
            payload = {
                "model": self.model,
                "input": inputs
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            
            async with session.post(self.api_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Volc API error: status={resp.status}")
                    raise Exception(f"Volc API returned {resp.status}")
                
                data = await resp.json()
                if "data" in data and isinstance(data["data"], list):
                    embeddings = []
                    for item in data["data"]:
                        embeddings.append(item.get("embedding", []))
                    return embeddings
                else:
                    logger.warning(f"Volc API unexpected response format: {data}")
                    raise Exception("Invalid Volc API response")
                    
        except Exception as e:
            logger.warning(f"Volc batch embedding failed: {e}")
            raise


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    """OpenAI 兼容 Embedding 提供者"""
    
    def __init__(self, api_key: str, model: str = "text-embedding-3-small", 
                 base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.model = model
        self.api_url = f"{base_url.rstrip('/')}/embeddings"
        self._session = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """关闭底层 HTTP 会话，释放连接资源。"""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as e:
                logger.warning("OpenAIEmbeddingProvider.close() error: %s", e)
            self._session = None

    async def embed_text(self, text: str) -> List[float]:
        """向量化单条文本"""
        try:
            session = await self._get_session()
            payload = {
                "model": self.model,
                "input": text
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            
            async with session.post(self.api_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"OpenAI API error: status={resp.status}")
                    raise Exception(f"OpenAI API returned {resp.status}")
                
                data = await resp.json()
                return data.get("data", [{}])[0].get("embedding", [])
                    
        except Exception as e:
            logger.warning(f"OpenAI embedding failed for text '{text[:50]}...': {e}")
            raise
    
    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量向量化文本"""
        try:
            session = await self._get_session()
            payload = {
                "model": self.model,
                "input": texts
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            
            async with session.post(self.api_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"OpenAI API error: status={resp.status}")
                    raise Exception(f"OpenAI API returned {resp.status}")
                
                data = await resp.json()
                embeddings = data.get("data", [])
                return [item.get("embedding", []) for item in embeddings]
                    
        except Exception as e:
            logger.warning(f"OpenAI batch embedding failed: {e}")
            raise