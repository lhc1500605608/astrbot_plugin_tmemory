"""Provider port — AstrBot Embedding / Rerank Provider 能力探测与调用封装。

Plan TMEAAA-379 T1 (B1) / 破坏性变更 BC-1：让插件优先复用 AstrBot 平台配置的
Embedding / Rerank Provider（知识库嵌入体系），缺失或异常时回退到插件自带的
独立配置（``volc`` / ``openai``）。

上游路径 / 引入版本 / 降级策略：
- ``astrbot.core.provider.provider.EmbeddingProvider``
  （``get_embedding(str) -> list[float]`` / ``get_embeddings(list[str]) -> list[list[float]]``
  / ``get_dim() -> int``）——知识库嵌入体系，4.5+ 存在，4.28 契约稳定。
- ``astrbot.core.provider.provider.RerankProvider.rerank(query, documents, top_n)``
  ——知识库重排，返回 ``list[RerankResult]``（``index`` / ``relevance_score``）。
- 解析入口：``astrbot.core.star.context.Context.provider_manager``
  （4.28 公共方法 ``get_all_embedding_providers()``；manager 暴露
  ``embedding_provider_insts`` / ``rerank_provider_insts`` / ``inst_map``）。
- 降级策略：本模块所有探测/解析均 try/except，能力缺失、配置的 provider_id 不存在
  或实例异常一律返回 ``None``；调用方（``vector_manager.py`` / ``core/vector.py``）
  回退 standalone，不抛异常、不阻断插件启动。

边界说明：本模块属于 ``adapters/`` 端口层，但**不 import astrbot**——上游 Context /
Provider 以 duck-typing 处理，便于在无 AstrBot 的单元测试环境中验证降级路径。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("astrbot")

SOURCE_PROVIDER = "astrbot_provider"
PROVIDER_MIN_VERSION = "4.28"
PROVIDER_FALLBACK_HINT = (
    "AstrBot Provider 嵌入不可用（需要 Context.provider_manager 暴露 "
    "embedding_provider_insts，EmbeddingProvider 契约 4.5+），已回退独立配置。"
)


@dataclass(frozen=True)
class ProviderCapabilities:
    """一次探测得到的 AstrBot Embedding / Rerank 能力快照。"""

    has_manager: bool
    embedding: bool
    rerank: bool
    embedding_count: int
    rerank_count: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "has_provider_manager": self.has_manager,
            "embedding_provider": self.embedding,
            "rerank_provider": self.rerank,
            "embedding_provider_count": self.embedding_count,
            "rerank_provider_count": self.rerank_count,
            "provider_min_version": PROVIDER_MIN_VERSION,
        }


def _get_manager(source: Any) -> Optional[Any]:
    """从 Context 或 ProviderManager 解析出 provider manager（失败返回 None）。"""
    if source is None:
        return None
    try:
        manager = getattr(source, "provider_manager", None)
        if manager is not None:
            return manager
        if hasattr(source, "embedding_provider_insts") or hasattr(
            source, "inst_map"
        ):
            return source
    except Exception:
        return None
    return None


def _instances(manager: Any, attr: str) -> List[Any]:
    try:
        items = getattr(manager, attr, None)
        if not items:
            return []
        return [item for item in list(items) if item is not None]
    except Exception:
        return []


def _is_embedding_provider(obj: Any) -> bool:
    return (
        obj is not None
        and callable(getattr(obj, "get_embedding", None))
        and callable(getattr(obj, "get_embeddings", None))
        and callable(getattr(obj, "get_dim", None))
    )


def _is_rerank_provider(obj: Any) -> bool:
    return obj is not None and callable(getattr(obj, "rerank", None))


def provider_id_of(obj: Any) -> str:
    """尽力读取 provider 实例的 ID（``meta().id`` 或 ``provider_config['id']``）。"""
    try:
        meta = getattr(obj, "meta", None)
        if callable(meta):
            value = getattr(meta(), "id", None)
            if value:
                return str(value)
    except Exception:
        pass
    try:
        config = getattr(obj, "provider_config", None)
        if isinstance(config, dict) and config.get("id"):
            return str(config["id"])
    except Exception:
        pass
    return ""


def provider_model_of(obj: Any) -> str:
    try:
        return str(getattr(obj, "model_name", "") or "")
    except Exception:
        return ""


def _find_by_id(manager: Any, provider_id: str, predicate) -> Optional[Any]:
    try:
        inst_map = getattr(manager, "inst_map", None)
        if isinstance(inst_map, dict):
            candidate = inst_map.get(provider_id)
            if predicate(candidate):
                return candidate
    except Exception:
        pass
    return None


def _resolve(
    source: Any,
    provider_id: str,
    attr: str,
    predicate,
) -> Optional[Any]:
    manager = _get_manager(source)
    if manager is None:
        return None
    candidates = _instances(manager, attr)
    if provider_id:
        found = _find_by_id(manager, provider_id, predicate)
        if found is None:
            found = next(
                (c for c in candidates if provider_id_of(c) == provider_id), None
            )
        if found is None:
            logger.warning(
                "[tmemory] AstrBot provider id=%s not found among %s",
                provider_id,
                attr,
            )
            return None
        return found if predicate(found) else None
    if candidates:
        return candidates[0]
    return None


def provider_capabilities(source: Any) -> ProviderCapabilities:
    """探测 AstrBot Provider 能力与可用实例数量（不抛异常）。"""
    manager = _get_manager(source)
    if manager is None:
        return ProviderCapabilities(False, False, False, 0, 0)
    embeddings = _instances(manager, "embedding_provider_insts")
    reranks = _instances(manager, "rerank_provider_insts")
    return ProviderCapabilities(
        has_manager=True,
        embedding=any(_is_embedding_provider(x) for x in embeddings),
        rerank=any(_is_rerank_provider(x) for x in reranks),
        embedding_count=len(embeddings),
        rerank_count=len(reranks),
    )


class ProviderEmbeddingAdapter:
    """把 AstrBot ``EmbeddingProvider`` 适配为插件内部 embedding 提供者接口。

    暴露 ``embed_text`` / ``embed_batch``（对齐 VectorManager 消费的提供者接口）
    以及 ``provider_id`` / ``model_name`` / ``get_dim()`` / ``source``。
    """

    source = SOURCE_PROVIDER

    def __init__(self, provider: Any, provider_id: str = "") -> None:
        self._provider = provider
        self.provider_id = provider_id or provider_id_of(provider)

    @property
    def model_name(self) -> str:
        return provider_model_of(self._provider)

    def get_dim(self) -> int:
        try:
            return int(self._provider.get_dim())
        except Exception as e:
            logger.warning("[tmemory] provider get_dim() failed: %s", e)
            return 0

    async def embed_text(self, text: str) -> List[float]:
        vec = await self._provider.get_embedding(text)
        return [float(x) for x in vec] if vec else []

    async def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        items = list(texts)
        try:
            result = await self._provider.get_embeddings(items)
            return [[float(x) for x in vec] for vec in result]
        except Exception as e:
            logger.warning(
                "[tmemory] provider embed_batch failed, retry one-by-one: %s", e
            )
            return [await self.embed_text(t) for t in items]

    async def close(self) -> None:
        """AstrBot Provider 生命周期由平台管理，无需插件释放。"""
        return None


class ProviderRerankAdapter:
    """把 AstrBot ``RerankProvider`` 适配为归一化 ``[{index, relevance_score}]``。"""

    source = SOURCE_PROVIDER

    def __init__(self, provider: Any, provider_id: str = "") -> None:
        self._provider = provider
        self.provider_id = provider_id or provider_id_of(provider)

    @property
    def model_name(self) -> str:
        return provider_model_of(self._provider)

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        top_n: Optional[int] = None,
    ) -> List[Dict[str, object]]:
        results = await self._provider.rerank(query, list(documents), top_n)
        normalized: List[Dict[str, object]] = []
        for item in results or []:
            index = getattr(item, "index", None)
            score = getattr(item, "relevance_score", None)
            if isinstance(item, dict):
                index = item.get("index") if index is None else index
                score = (
                    item.get("relevance_score") if score is None else score
                )
            if index is None:
                continue
            normalized.append(
                {"index": int(index), "relevance_score": float(score or 0.0)}
            )
        return normalized


def list_embedding_providers(source: Any) -> List[Dict[str, object]]:
    """枚举 AstrBot 已配置的 Embedding Provider（能力缺失返回 []，绝不抛异常）。

    返回 ``[{"id": str, "model": str, "dim": int}]``，供插件页下拉选择 /
    bridge ``GET /embedding/providers`` 使用。
    """
    try:
        manager = _get_manager(source)
        if manager is None:
            return []
        providers: List[Dict[str, object]] = []
        for item in _instances(manager, "embedding_provider_insts"):
            if not _is_embedding_provider(item):
                continue
            try:
                dim = int(item.get_dim())
            except Exception:
                dim = 0
            providers.append(
                {
                    "id": provider_id_of(item),
                    "model": provider_model_of(item),
                    "dim": dim,
                }
            )
        return providers
    except Exception as e:  # noqa: BLE001 - 枚举失败绝不 500
        logger.warning("[tmemory] list embedding providers failed: %s", e)
        return []


def resolve_embedding_provider(
    source: Any, provider_id: str = ""
) -> Optional[ProviderEmbeddingAdapter]:
    """解析 AstrBot Embedding Provider；不可用返回 None（不抛异常）。"""
    try:
        provider = _resolve(
            source, str(provider_id or "").strip(), "embedding_provider_insts",
            _is_embedding_provider,
        )
    except Exception as e:
        logger.warning("[tmemory] resolve embedding provider failed: %s", e)
        return None
    if provider is None:
        return None
    return ProviderEmbeddingAdapter(provider)


def resolve_rerank_provider(
    source: Any, provider_id: str = ""
) -> Optional[ProviderRerankAdapter]:
    """解析 AstrBot Rerank Provider；不可用返回 None（不抛异常）。"""
    try:
        provider = _resolve(
            source, str(provider_id or "").strip(), "rerank_provider_insts",
            _is_rerank_provider,
        )
    except Exception as e:
        logger.warning("[tmemory] resolve rerank provider failed: %s", e)
        return None
    if provider is None:
        return None
    return ProviderRerankAdapter(provider)


__all__ = [
    "PROVIDER_FALLBACK_HINT",
    "PROVIDER_MIN_VERSION",
    "SOURCE_PROVIDER",
    "ProviderCapabilities",
    "ProviderEmbeddingAdapter",
    "ProviderRerankAdapter",
    "list_embedding_providers",
    "provider_capabilities",
    "provider_id_of",
    "provider_model_of",
    "resolve_embedding_provider",
    "resolve_rerank_provider",
]
