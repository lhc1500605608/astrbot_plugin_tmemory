"""TMEAAA-380 (T1/B1) — AstrBot Provider embedding/rerank 接入测试。

覆盖：
- adapters/provider.py 能力探测与 Provider 解析（自动 / 指定 id / 缺失）
- Provider 适配器（embed_text / embed_batch / dim / rerank 归一化）
- VectorManager provider 优先、缺失/异常回退 standalone、standalone 强制
- core/vector.embed_text provider 失败回退、维度校验
- core/vector.apply_provider_dim_change 维度变更 → 缓存失效 + 重建
- core/vector.rerank_results provider 优先

全部使用 duck-typed fake，不需要真实 AstrBot。
"""

from __future__ import annotations

import pytest


# ── fakes ────────────────────────────────────────────────────────────────


class FakeEmbeddingProvider:
    def __init__(self, provider_id="emb-1", dim=8):
        self.model_name = "fake-embed-model"
        self.provider_config = {"id": provider_id}
        self._dim = dim

    async def get_embedding(self, text):
        return [0.5] * self._dim

    async def get_embeddings(self, texts):
        return [[0.5] * self._dim for _ in texts]

    def get_dim(self):
        return self._dim


class FakeRerankProvider:
    def __init__(self, provider_id="rerank-1"):
        self.model_name = "fake-rerank-model"
        self.provider_config = {"id": provider_id}

    async def rerank(self, query, documents, top_n=None):
        class _R:
            def __init__(self, index, score):
                self.index = index
                self.relevance_score = score

        return [_R(1, 0.9), _R(0, 0.1)]


class FakeManager:
    def __init__(self, embeddings=(), reranks=()):
        self.embedding_provider_insts = list(embeddings)
        self.rerank_provider_insts = list(reranks)
        self.inst_map = {
            p.provider_config["id"]: p for p in list(embeddings) + list(reranks)
        }


class FakeContext:
    def __init__(self, manager):
        self.provider_manager = manager


class FakeVM:
    """core/vector.py 依赖的最小 VectorManager 鸭子类型。"""

    def __init__(
        self,
        provider=None,
        source="provider",
        provider_dim=0,
        provider_id="p",
        rerank_provider=None,
    ):
        self.embedding_provider = provider
        self.rerank_provider = rerank_provider
        self.source = source
        self.provider_dim = provider_dim
        self.provider_id = provider_id
        self.embedding_source = "provider"

    def status(self):
        return {
            "active_source": self.source,
            "embedding_source": self.embedding_source,
            "provider_id": self.provider_id,
            "provider_dim": self.provider_dim,
        }


class FailingEmbeddingProvider:
    async def embed_text(self, text):
        raise RuntimeError("upstream provider down")


@pytest.fixture()
def provider_port(plugin_module):
    from astrbot_plugin_tmemory.adapters import provider

    return provider


@pytest.fixture()
def vector_port(plugin_module):
    from astrbot_plugin_tmemory.core import vector

    return vector


@pytest.fixture()
def vm_module(plugin_module):
    import sys

    # test_plugin_baseline 会向 sys.modules 注入 DummyVectorManager（未清理），
    # 这里强制重新加载真实模块，避免同会话内的模块缓存污染。
    sys.modules.pop("astrbot_plugin_tmemory.vector_manager", None)
    from astrbot_plugin_tmemory import vector_manager

    return vector_manager


# ── adapters/provider.py ─────────────────────────────────────────────────


def test_provider_capabilities(provider_port):
    caps = provider_port.provider_capabilities(
        FakeContext(FakeManager(embeddings=[FakeEmbeddingProvider()], reranks=[FakeRerankProvider()]))
    )
    assert caps.has_manager is True
    assert caps.embedding is True
    assert caps.rerank is True
    assert caps.embedding_count == 1

    empty = provider_port.provider_capabilities(FakeContext(FakeManager()))
    assert empty.has_manager is True
    assert empty.embedding is False
    assert provider_port.provider_capabilities(None).has_manager is False


def test_resolve_embedding_provider_auto_and_by_id(provider_port):
    p1 = FakeEmbeddingProvider("emb-1", dim=4)
    p2 = FakeEmbeddingProvider("emb-2", dim=8)
    ctx = FakeContext(FakeManager(embeddings=[p1, p2]))

    auto = provider_port.resolve_embedding_provider(ctx)
    assert auto is not None
    assert auto.provider_id == "emb-1"
    assert auto.get_dim() == 4

    by_id = provider_port.resolve_embedding_provider(ctx, "emb-2")
    assert by_id is not None
    assert by_id.provider_id == "emb-2"
    assert by_id.get_dim() == 8


def test_resolve_embedding_provider_missing_returns_none(provider_port):
    ctx = FakeContext(FakeManager(embeddings=[FakeEmbeddingProvider("emb-1")]))
    assert provider_port.resolve_embedding_provider(ctx, "does-not-exist") is None
    assert provider_port.resolve_embedding_provider(None) is None
    assert provider_port.resolve_embedding_provider(FakeContext(FakeManager())) is None


@pytest.mark.asyncio
async def test_embedding_adapter_contract(provider_port):
    adapter = provider_port.ProviderEmbeddingAdapter(FakeEmbeddingProvider(dim=3))
    assert adapter.source == provider_port.SOURCE_PROVIDER
    assert adapter.model_name == "fake-embed-model"
    assert await adapter.embed_text("hi") == [0.5, 0.5, 0.5]
    batch = await adapter.embed_batch(["a", "b"])
    assert len(batch) == 2 and len(batch[0]) == 3
    assert adapter.get_dim() == 3


def test_rerank_adapter_normalizes_results(provider_port):
    adapter = provider_port.ProviderRerankAdapter(FakeRerankProvider())
    assert adapter.provider_id == "rerank-1"


@pytest.mark.asyncio
async def test_rerank_adapter_rerank(provider_port):
    adapter = provider_port.ProviderRerankAdapter(FakeRerankProvider())
    results = await adapter.rerank("q", ["d0", "d1"], 2)
    assert results == [
        {"index": 1, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.1},
    ]


# ── vector_manager.py ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vector_manager_prefers_provider(vm_module):
    ctx = FakeContext(FakeManager(embeddings=[FakeEmbeddingProvider("emb-1", dim=12)]))
    vm = vm_module.VectorManager(":memory:", {"embedding_source": "provider"}, context=ctx)
    await vm.initialize()
    assert vm.source == "provider"
    assert vm.provider_id == "emb-1"
    assert vm.provider_dim == 12
    assert vm.status()["active_source"] == "provider"
    assert await vm.embedding_provider.embed_text("x") == [0.5] * 12


@pytest.mark.asyncio
async def test_vector_manager_falls_back_to_standalone(vm_module):
    cfg = {
        "embedding_source": "provider",
        "embedding_provider": "openai",
        "embedding_api_key": "test-key",
        "embedding_model": "text-embedding-3-small",
    }
    vm = vm_module.VectorManager(":memory:", cfg, context=None)
    await vm.initialize()
    assert vm.source == "standalone"
    assert vm.fallback_reason
    from astrbot_plugin_tmemory.embeddingProvider import OpenAIEmbeddingProvider

    assert isinstance(vm.embedding_provider, OpenAIEmbeddingProvider)


@pytest.mark.asyncio
async def test_vector_manager_standalone_mode_skips_provider(vm_module):
    ctx = FakeContext(FakeManager(embeddings=[FakeEmbeddingProvider("emb-1")]))
    cfg = {
        "embedding_source": "standalone",
        "embedding_provider": "openai",
        "embedding_api_key": "test-key",
    }
    vm = vm_module.VectorManager(":memory:", cfg, context=ctx)
    await vm.initialize()
    assert vm.source == "standalone"


@pytest.mark.asyncio
async def test_vector_manager_missing_provider_no_standalone_is_none(vm_module):
    vm = vm_module.VectorManager(":memory:", {"embedding_source": "provider"}, context=None)
    await vm.initialize()
    assert vm.embedding_provider is None
    assert vm.source == "none"


@pytest.mark.asyncio
async def test_vector_manager_rerank_provider(vm_module):
    ctx = FakeContext(FakeManager(reranks=[FakeRerankProvider("rr-9")]))
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_source": "provider", "rerank_provider_id": "rr-9"}, context=ctx
    )
    await vm.initialize()
    assert vm.rerank_provider is not None
    assert vm.status()["rerank_source"] == "provider"


# ── core/vector.py ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_text_provider_success(plugin, vector_port):
    dim = plugin._cfg.embed_dim
    plugin._vector_manager = FakeVM(provider=embedding_adapter(dim=dim))
    vec = await vector_port.embed_text(plugin, "hello")
    assert vec is not None and len(vec) == dim
    assert plugin._embed_last_source == "provider"
    assert plugin._embed_ok_count == 1


@pytest.mark.asyncio
async def test_embed_text_provider_failure_falls_back(plugin, vector_port):
    plugin._vec_available = False
    plugin._cfg.embed_base_url = ""
    plugin._vector_manager = FakeVM(provider=FailingEmbeddingProvider())
    vec = await vector_port.embed_text(plugin, "hello")
    assert vec is None
    assert plugin._embed_provider_fail_count == 1


@pytest.mark.asyncio
async def test_embed_text_provider_dim_mismatch_rejected(plugin, vector_port):
    plugin._vector_manager = FakeVM(provider=embedding_adapter(dim=plugin._cfg.embed_dim + 1))
    vec = await vector_port.embed_text(plugin, "hello")
    assert vec is None
    assert plugin._embed_fail_count == 1


@pytest.mark.asyncio
async def test_apply_provider_dim_change_updates_dim_and_clears_cache(plugin, vector_port):
    plugin._cfg.embed_dim = 2048
    plugin._vector_manager = FakeVM(provider=FakeEmbeddingProvider(dim=1024), provider_dim=1024)
    with plugin._db() as conn:
        conn.execute(
            "INSERT INTO query_embedding_cache("
            "query_hash, query_text, embedding, embed_dim, created_at, last_hit_at, hit_count"
            ") VALUES(?,?,?,?,?,?,1)",
            ("h", "q", b"\x00", 2048, "now", "now"),
        )

    result = await vector_port.apply_provider_dim_change(plugin)

    assert result["changed"] is True
    assert plugin._cfg.embed_dim == 1024
    assert result["cache_cleared"] is True
    with plugin._db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM query_embedding_cache").fetchone()["c"]
    assert count == 0


@pytest.mark.asyncio
async def test_apply_provider_dim_change_rebuilds_when_vec_available(plugin, vector_port, monkeypatch):
    plugin._cfg.embed_dim = 2048
    plugin._cfg.auto_rebuild_on_dim_change = True
    plugin._vec_available = True
    plugin._vector_manager = FakeVM(provider=FakeEmbeddingProvider(dim=512), provider_dim=512)

    calls = {}

    def fake_init_db(vec_available, embed_dim):
        calls["init_db"] = (vec_available, embed_dim)

    monkeypatch.setattr(plugin._db_mgr, "init_db", fake_init_db)
    # v0.11.1: 重建前先在连接上探测 vec0；测试环境无扩展，需显式模拟可用。
    monkeypatch.setattr(plugin._db_mgr, "vec0_available", staticmethod(lambda conn: True))
    monkeypatch.setattr(plugin._db_mgr, "vec_enabled", True)

    async def fake_rebuild(p):
        return (3, 1)

    monkeypatch.setattr(vector_port, "rebuild_vector_index", fake_rebuild)

    result = await vector_port.apply_provider_dim_change(plugin)
    assert result["changed"] is True
    assert result["rebuilt"] is True
    assert result["rebuilt_ok"] == 3 and result["rebuilt_fail"] == 1
    assert calls["init_db"] == (True, 512)


@pytest.mark.asyncio
async def test_apply_provider_dim_change_noop_when_same_or_non_provider(plugin, vector_port):
    plugin._cfg.embed_dim = 1024
    plugin._vector_manager = FakeVM(provider=FakeEmbeddingProvider(dim=1024), provider_dim=1024)
    result = await vector_port.apply_provider_dim_change(plugin)
    assert result["changed"] is False

    plugin._cfg.embed_dim = 2048
    plugin._vector_manager = FakeVM(provider=FakeEmbeddingProvider(dim=512), source="standalone", provider_dim=512)
    assert (await vector_port.apply_provider_dim_change(plugin))["changed"] is False


@pytest.mark.asyncio
async def test_rerank_results_prefers_provider(plugin, vector_port):
    plugin._vector_manager = FakeVM(rerank_provider=provider_adapter())
    candidates = [
        {"memory": "doc-0", "id": 0},
        {"memory": "doc-1", "id": 1},
    ]
    result = await vector_port.rerank_results(plugin, "query", candidates, top_n=2)
    assert [r["id"] for r in result] == [1, 0]
    assert result[0]["rerank_score"] == pytest.approx(0.9)


def provider_adapter():
    from astrbot_plugin_tmemory.adapters import provider

    return provider.ProviderRerankAdapter(FakeRerankProvider())


def embedding_adapter(dim=8):
    from astrbot_plugin_tmemory.adapters import provider

    return provider.ProviderEmbeddingAdapter(FakeEmbeddingProvider(dim=dim))
