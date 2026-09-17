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
import os

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


class LocalBgeEmbeddingProvider(BaseEmbeddingProvider):
    """本地 bge-small-zh-v1.5 ONNX Embedding 提供者（零 API 成本）。

    Plan TMEAAA-379 B3：模型由 ``tools/download_bge_onnx.py`` 下载到
    ``data/bge-small-zh-v1.5``（Xenova 导出，含 ``model_quantized.onnx`` +
    ``tokenizer.json``）。依赖 ``onnxruntime`` 与 ``tokenizers``，均为可选依赖：
    缺失或模型不存在时构造抛 ``RuntimeError``，由 VectorManager 捕获并回退
    standalone，不阻断插件启动。
    """

    source = "local"

    def __init__(
        self,
        model_dir: str,
        model_file: str = "model_quantized.onnx",
        max_length: int = 512,
    ):
        self.model_dir = model_dir
        self.model_file = model_file
        self.max_length = max(16, int(max_length or 512))
        self.model_name = f"bge-local:{os.path.basename(model_file)}"
        self._session = None
        self._tokenizer = None
        self._input_names = set()
        self._dim = 0
        self._load()

    def _load(self) -> None:
        model_path = os.path.join(self.model_dir, self.model_file)
        tokenizer_path = os.path.join(self.model_dir, "tokenizer.json")
        if not os.path.isfile(model_path):
            raise RuntimeError(
                f"local embedding model not found: {model_path} "
                f"(run tools/download_bge_onnx.py first)"
            )
        if not os.path.isfile(tokenizer_path):
            raise RuntimeError(f"local embedding tokenizer not found: {tokenizer_path}")
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            raise RuntimeError(
                "local embedding requires optional deps 'onnxruntime' and 'tokenizers'"
            ) from e

        tokenizer = Tokenizer.from_file(tokenizer_path)
        tokenizer.enable_truncation(max_length=self.max_length)
        tokenizer.enable_padding()
        self._tokenizer = tokenizer

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        self._session = ort.InferenceSession(
            model_path, sess_options=sess_options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        dim = 0
        for out in self._session.get_outputs():
            shape = list(getattr(out, "shape", []) or [])
            if shape and isinstance(shape[-1], int):
                dim = max(dim, int(shape[-1]))
        self._dim = dim
        logger.info(
            "[EmbeddingProvider] local bge loaded: model=%s dim=%s inputs=%s",
            self.model_file,
            self._dim,
            sorted(self._input_names),
        )

    def get_dim(self) -> int:
        return int(self._dim or 0)

    def _encode(self, texts: List[str]) -> List[List[float]]:
        import numpy as np

        encodings = self._tokenizer.encode_batch([str(t or "") for t in texts])
        feed = {}
        if "input_ids" in self._input_names:
            feed["input_ids"] = np.asarray([e.ids for e in encodings], dtype=np.int64)
        if "attention_mask" in self._input_names:
            feed["attention_mask"] = np.asarray(
                [e.attention_mask for e in encodings], dtype=np.int64
            )
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.asarray(
                [e.type_ids for e in encodings], dtype=np.int64
            )

        outputs = self._session.run(None, feed)
        output_names = [o.name for o in self._session.get_outputs()]
        embeddings = None
        for name, value in zip(output_names, outputs):
            arr = np.asarray(value)
            if arr.ndim == 3:
                mask = feed.get("attention_mask")
                if mask is None:
                    mask = np.ones(arr.shape[:2], dtype=np.int64)
                mask_f = mask.astype(np.float32)[..., None]
                summed = (arr * mask_f).sum(axis=1)
                counts = np.clip(mask_f.sum(axis=1), 1e-9, None)
                embeddings = summed / counts
                break
            if arr.ndim == 2 and ("sentence_embedding" in name or embeddings is None):
                embeddings = arr
                break
        if embeddings is None:
            raise RuntimeError("local embedding model produced no usable output")

        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        normalized = embeddings / np.clip(norms, 1e-9, None)
        return [[float(x) for x in row] for row in normalized]

    async def embed_text(self, text: str) -> List[float]:
        import asyncio

        result = await asyncio.to_thread(self._encode, [text])
        return result[0] if result else []

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        import asyncio

        items = list(texts)
        if not items:
            return []
        return await asyncio.to_thread(self._encode, items)

    async def close(self) -> None:
        self._session = None
        self._tokenizer = None
