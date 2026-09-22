"""TMEAAA-380 (T1/B1) — AstrBot Provider embedding/rerank 接入测试。

覆盖：
- adapters/provider.py 能力探测、Provider 解析（自动 / 指定 id / 缺失）、枚举
- Provider 适配器（embed_text / embed_batch / dim / rerank 归一化）
- VectorManager provider-only（旧 standalone/local 配置被忽略）
- core/vector.embed_text provider 失败返回 None、维度校验
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


class ClosedClientEmbeddingProvider:
    """模拟插件重载后 httpx client 已关闭的 provider。"""

    async def embed_text(self, text):
        raise RuntimeError("Cannot send a request, as the client has been closed.")


class RecoveringVM(FakeVM):
    """TMEAAA-510：force refresh 后换上新 provider 的鸭子类型 VectorManager。"""

    def __init__(self, provider, recovered_provider, **kwargs):
        super().__init__(provider=provider, **kwargs)
        self._recovered = recovered_provider
        self.refresh_calls = []

    async def refresh(self, min_interval_sec=0.0, force=False):
        self.refresh_calls.append(force)
        if force:
            self.embedding_provider = self._recovered
        return self.embedding_provider is not None


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


def test_list_embedding_providers_enumerates(provider_port):
    p1 = FakeEmbeddingProvider("emb-1", dim=4)
    p2 = FakeEmbeddingProvider("emb-2", dim=8)
    ctx = FakeContext(
        FakeManager(embeddings=[p1, p2], reranks=[FakeRerankProvider("rr-1")])
    )
    assert provider_port.list_embedding_providers(ctx) == [
        {"id": "emb-1", "model": "fake-embed-model", "dim": 4},
        {"id": "emb-2", "model": "fake-embed-model", "dim": 8},
    ]


def test_list_embedding_providers_capability_missing_returns_empty(provider_port):
    assert provider_port.list_embedding_providers(None) == []
    assert provider_port.list_embedding_providers(FakeContext(FakeManager())) == []
    assert provider_port.list_embedding_providers(object()) == []


def test_list_embedding_providers_broken_dim_is_zero(provider_port):
    class BrokenDimProvider(FakeEmbeddingProvider):
        def get_dim(self):
            raise RuntimeError("dim unavailable")

    ctx = FakeContext(FakeManager(embeddings=[BrokenDimProvider("emb-x", dim=8)]))
    assert provider_port.list_embedding_providers(ctx) == [
        {"id": "emb-x", "model": "fake-embed-model", "dim": 0}
    ]


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
async def test_vector_manager_provider_only_ignores_standalone_config(vm_module):
    """embedding 收敛为 provider-only：不再回退 standalone/local 配置。"""
    cfg = {
        "embedding_source": "standalone",
        "embedding_provider": "openai",
        "embedding_api_key": "test-key",
        "embedding_model": "text-embedding-3-small",
    }
    vm = vm_module.VectorManager(":memory:", cfg, context=None)
    await vm.initialize()
    assert vm.embedding_provider is None
    assert vm.source == "none"
    assert vm.embedding_source == "provider"
    assert vm.fallback_reason
    assert vm.status()["active_source"] == "none"


@pytest.mark.asyncio
async def test_vector_manager_legacy_local_source_still_uses_provider(vm_module):
    """旧配置 embedding_source=local 不再启用本地模型，仍走 Provider。"""
    ctx = FakeContext(FakeManager(embeddings=[FakeEmbeddingProvider("emb-1")]))
    cfg = {
        "embedding_source": "local",
        "local_embedding_path": "/nonexistent/bge-model",
    }
    vm = vm_module.VectorManager(":memory:", cfg, context=ctx)
    await vm.initialize()
    assert vm.embedding_source == "provider"
    assert vm.source == "provider"
    assert vm.provider_id == "emb-1"


@pytest.mark.asyncio
async def test_vector_manager_missing_provider_is_none(vm_module):
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


# ── TMEAAA-472：冷启动 provider 晚于插件 initialize 的补解析 ──────────────


@pytest.mark.asyncio
async def test_vector_manager_refresh_recovers_after_provider_ready(vm_module):
    """init 时 provider 列表为空、就绪后 refresh 恢复 provider。"""
    manager = FakeManager()  # AstrBot 冷启动：provider 尚未实例化
    ctx = FakeContext(manager)
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=ctx
    )
    await vm.initialize()
    assert vm.source == "none"
    assert vm.status()["active_source"] == "none"
    assert vm.fallback_reason

    provider = FakeEmbeddingProvider("emb-1", dim=1024)
    manager.embedding_provider_insts = [provider]
    manager.inst_map = {"emb-1": provider}

    await vm.refresh()

    assert vm.source == "provider"
    assert vm.provider_id == "emb-1"
    assert vm.provider_dim == 1024
    assert vm.status()["active_source"] == "provider"
    assert vm.fallback_reason == ""


@pytest.mark.asyncio
async def test_vector_manager_force_refresh_replaces_stale_provider(vm_module):
    """TMEAAA-510：force=True 丢弃已关闭的旧实例，重新解析到新 provider。"""
    old = FakeEmbeddingProvider("emb-1", dim=8)
    ctx = FakeContext(FakeManager(embeddings=[old]))
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=ctx
    )
    await vm.initialize()
    assert vm.embedding_provider is not None
    assert vm.embedding_provider._provider is old

    new = FakeEmbeddingProvider("emb-1", dim=8)
    ctx.provider_manager.embedding_provider_insts = [new]
    ctx.provider_manager.inst_map = {"emb-1": new}

    assert await vm.refresh(force=True) is True
    assert vm.embedding_provider._provider is new


@pytest.mark.asyncio
async def test_embed_text_lazily_recovers_provider_after_ready(plugin, vector_port, vm_module):
    """provider 缺失时首次 embed 惰性补解析（不依赖 on_astrbot_loaded 时序）。"""
    manager = FakeManager()
    ctx = FakeContext(manager)
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=ctx
    )
    await vm.initialize()
    plugin._vector_manager = vm
    plugin._cfg.embed_dim = 1024

    provider = FakeEmbeddingProvider("emb-1", dim=1024)
    manager.embedding_provider_insts = [provider]
    manager.inst_map = {"emb-1": provider}

    vec = await vector_port.embed_text(plugin, "hello")
    assert vec is not None and len(vec) == 1024
    assert vm.source == "provider"


@pytest.mark.asyncio
async def test_resume_vector_provider_after_astrbot_loaded(plugin, vm_module):
    """OnAstrBotLoadedEvent 补解析：重启后无需再保存即可 active_source=provider。"""
    manager = FakeManager()
    ctx = FakeContext(manager)
    plugin._cfg.enable_vector_search = True
    plugin._cfg.embedding_provider_id = "emb-1"
    plugin._cfg.embed_dim = 1024
    plugin.context = ctx
    vm = vm_module.VectorManager(
        plugin.db_path, {"embedding_provider_id": "emb-1"}, context=ctx
    )
    await vm.initialize()
    plugin._vector_manager = vm
    assert vm.source == "none"

    provider = FakeEmbeddingProvider("emb-1", dim=1024)
    manager.embedding_provider_insts = [provider]
    manager.inst_map = {"emb-1": provider}

    await plugin._resume_vector_provider()

    assert vm.source == "provider"
    assert vm.provider_id == "emb-1"
    assert vm.provider_dim == 1024
    assert vm.status()["active_source"] == "provider"


@pytest.mark.asyncio
async def test_resume_vector_provider_noop_when_vector_search_disabled(plugin):
    plugin._cfg.enable_vector_search = False
    plugin._vector_manager = None
    await plugin._resume_vector_provider()
    assert plugin._vector_manager is None


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
async def test_embed_text_provider_failure_returns_none(plugin, vector_port):
    plugin._vec_available = False
    plugin._cfg.embed_base_url = "https://example.invalid/v1"
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


# ── TMEAAA-510：插件重载后 provider client 已关闭 → 强制刷新 + 重试一次 ──────


@pytest.mark.asyncio
async def test_embed_text_refreshes_and_retries_after_client_closed(plugin, vector_port):
    plugin._cfg.embed_dim = 1024
    vm = RecoveringVM(
        provider=ClosedClientEmbeddingProvider(),
        recovered_provider=embedding_adapter(dim=1024),
        provider_dim=1024,
    )
    plugin._vector_manager = vm

    vec = await vector_port.embed_text(plugin, "hello")

    assert vec is not None and len(vec) == 1024
    assert vm.refresh_calls == [True]
    assert plugin._embed_provider_fail_count == 0
    assert plugin._embed_ok_count == 1


@pytest.mark.asyncio
async def test_embed_text_returns_none_when_retry_also_fails(plugin, vector_port):
    plugin._cfg.embed_dim = 1024
    vm = RecoveringVM(
        provider=ClosedClientEmbeddingProvider(),
        recovered_provider=ClosedClientEmbeddingProvider(),
        provider_dim=1024,
    )
    plugin._vector_manager = vm

    vec = await vector_port.embed_text(plugin, "hello")

    assert vec is None
    assert vm.refresh_calls == [True]
    assert plugin._embed_provider_fail_count == 1


@pytest.mark.asyncio
async def test_embed_text_does_not_refresh_on_unrelated_error(plugin, vector_port):
    plugin._cfg.embed_dim = 1024
    vm = RecoveringVM(
        provider=FailingEmbeddingProvider(),
        recovered_provider=embedding_adapter(dim=1024),
        provider_dim=1024,
    )
    plugin._vector_manager = vm

    vec = await vector_port.embed_text(plugin, "hello")

    assert vec is None
    assert vm.refresh_calls == []
    assert plugin._embed_provider_fail_count == 1


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


# ── TMEAAA-478: 维度调和后配置热加载回退 / force 重建清空索引 ─────────────


class _FakeEvent:
    def __init__(self, message_str):
        self.message_str = message_str

    def plain_result(self, text):
        return text


@pytest.mark.asyncio
async def test_effective_embed_dim_prefers_provider_dim(plugin, vector_port):
    """provider 激活时以 provider_dim 为权威，忽略回退的 _cfg.embed_dim。"""
    plugin._cfg.embed_dim = 2048
    plugin._vector_manager = FakeVM(provider=embedding_adapter(dim=1024), provider_dim=1024)
    assert vector_port.effective_embed_dim(plugin) == 1024

    plugin._vector_manager = FakeVM(provider=embedding_adapter(dim=1024), provider_dim=0)
    assert vector_port.effective_embed_dim(plugin) == 2048


@pytest.mark.asyncio
async def test_embed_text_accepts_provider_dim_when_cfg_stale(plugin, vector_port):
    """配置热加载把 embed_dim 回退成旧值后，embed 仍按 provider 维度成功并同步 _cfg。"""
    plugin._cfg.embed_dim = 2048
    plugin._vector_manager = FakeVM(provider=embedding_adapter(dim=1024), provider_dim=1024)

    vec = await vector_port.embed_text(plugin, "hello")

    assert vec is not None and len(vec) == 1024
    assert plugin._cfg.embed_dim == 1024
    assert plugin._embed_fail_count == 0


@pytest.mark.asyncio
async def test_apply_provider_dim_change_persists_to_config(plugin, vector_port):
    """调和后写回 vector_retrieval.vector_dim，热加载不会回退。"""
    plugin._cfg.embed_dim = 2048
    plugin.config["vector_retrieval"] = {"vector_dim": 2048}
    plugin._vector_manager = FakeVM(provider=FakeEmbeddingProvider(dim=1024), provider_dim=1024)

    result = await vector_port.apply_provider_dim_change(plugin)

    assert result["changed"] is True
    assert plugin.config["vector_retrieval"]["vector_dim"] == 1024


def test_vec_table_dim_reads_declared_dim(plugin, vector_port):
    with plugin._db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_vectors"
            "(memory_id INTEGER PRIMARY KEY, embedding float[8])"
        )
    assert vector_port.vec_table_dim(plugin) == 8
    assert vector_port.vec_table_dim(plugin, table="missing_table") == 0


@pytest.mark.asyncio
async def test_tm_vec_rebuild_force_aborts_on_embed_failure_without_clearing(plugin):
    """预检 embed 失败时不清空现有索引（TMEAAA-478 现象 2）。"""
    plugin._vec_available = True
    plugin._vector_manager = FakeVM(
        provider=FailingEmbeddingProvider(), source="provider", provider_dim=1024
    )
    with plugin._db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_vectors"
            "(memory_id INTEGER PRIMARY KEY, embedding float[8])"
        )
        conn.execute("INSERT INTO memory_vectors(memory_id, embedding) VALUES(1, x'00')")

    outputs = [
        msg
        async for msg in plugin._handle_tm_vec_rebuild(
            _FakeEvent("/tm_vec_rebuild force=true")
        )
    ]

    assert any("已中止" in str(m) for m in outputs)
    with plugin._db() as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM memory_vectors").fetchone()["c"]
    assert rows == 1


@pytest.mark.asyncio
async def test_tm_vec_rebuild_force_aborts_on_table_dim_mismatch(plugin, vector_port):
    """向量表维度与 embedding 维度不一致时不清空索引。"""
    plugin._vec_available = True
    plugin._vector_manager = FakeVM(
        provider=embedding_adapter(dim=8), source="provider", provider_dim=8
    )
    with plugin._db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_vectors"
            "(memory_id INTEGER PRIMARY KEY, embedding float[4])"
        )
        conn.execute("INSERT INTO memory_vectors(memory_id, embedding) VALUES(1, x'00')")

    outputs = [
        msg
        async for msg in plugin._handle_tm_vec_rebuild(
            _FakeEvent("/tm_vec_rebuild force=true")
        )
    ]

    assert any("已中止" in str(m) for m in outputs)
    with plugin._db() as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM memory_vectors").fetchone()["c"]
    assert rows == 1


# ── TMEAAA-517：嵌入缓存计数属性未初始化 → AttributeError（热路径/重载竞态）──


class _FakeSqliteVec:
    @staticmethod
    def serialize_float32(vec):
        import struct

        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def deserialize_float32(blob):
        import struct

        return list(struct.unpack(f"{len(blob) // 4}f", blob))


def test_plugin_constructor_initializes_embed_cache_counters(plugin):
    """构造期即初始化缓存计数，杜绝热路径先于 initialize 的 AttributeError。"""
    assert plugin._embed_cache_hit_count == 0
    assert plugin._embed_cache_miss_count == 0


@pytest.mark.asyncio
async def test_query_embedding_cache_miss_without_counter_attrs(plugin, vector_port):
    """计数属性缺失的实例走缓存未命中路径：不抛错且计数正确。"""
    plugin._vec_available = True
    for attr in ("_embed_cache_hit_count", "_embed_cache_miss_count"):
        if hasattr(plugin, attr):
            delattr(plugin, attr)

    assert await vector_port.get_cached_query_embedding(plugin, "unseen query") is None
    assert plugin._embed_cache_miss_count == 1


@pytest.mark.asyncio
async def test_query_embedding_cache_hit_without_counter_attrs(plugin, vector_port):
    """计数属性缺失的实例走缓存命中路径：不抛错且计数正确。"""
    plugin._vec_available = True
    plugin._cfg.embed_dim = 4
    plugin._sqlite_vec = _FakeSqliteVec()
    await vector_port.store_query_embedding(plugin, "hello", [1.0, 2.0, 3.0, 4.0])
    for attr in ("_embed_cache_hit_count", "_embed_cache_miss_count"):
        if hasattr(plugin, attr):
            delattr(plugin, attr)

    vec = await vector_port.get_cached_query_embedding(plugin, "hello")

    assert vec is not None and len(vec) == 4
    assert plugin._embed_cache_hit_count == 1
    assert getattr(plugin, "_embed_cache_miss_count", 0) == 0


def provider_adapter():
    from astrbot_plugin_tmemory.adapters import provider

    return provider.ProviderRerankAdapter(FakeRerankProvider())


def embedding_adapter(dim=8):
    from astrbot_plugin_tmemory.adapters import provider

    return provider.ProviderEmbeddingAdapter(FakeEmbeddingProvider(dim=dim))


# ── TMEAAA-519：平台 provider client 被外部关闭 → 自愈必须拿到可用 client ──


class _FakeHttpClient:
    """模拟 provider 底层 httpx client 的关闭状态。"""

    def __init__(self):
        self.is_closed = False


class FakePlatformEmbeddingProvider:
    """模拟 AstrBot EmbeddingProvider：``client`` 可被外部关闭。"""

    def __init__(self, provider_config, provider_settings=None):
        self.provider_config = provider_config
        self.provider_settings = provider_settings or {}
        self.model_name = "fake-platform"
        self.client = _FakeHttpClient()
        self._dim = int(provider_config.get("embedding_dimensions", 4) or 4)

    async def get_embedding(self, text):
        if self.client.is_closed:
            raise RuntimeError(
                "Cannot send a request, as the client has been closed."
            )
        return [0.5] * self._dim

    async def get_embeddings(self, texts):
        return [await self.get_embedding(t) for t in texts]

    def get_dim(self):
        return self._dim


def platform_provider(provider_id="emb-1", dim=4, closed=False):
    p = FakePlatformEmbeddingProvider(
        {"id": provider_id, "embedding_dimensions": dim}
    )
    p.client.is_closed = closed
    return p


def test_instance_is_closed_detects_client_state(provider_port):
    assert provider_port.instance_is_closed(platform_provider(closed=True)) is True
    assert provider_port.instance_is_closed(platform_provider(closed=False)) is False
    assert provider_port.instance_is_closed(FakeEmbeddingProvider()) is False


def test_resolve_prefers_live_duplicate_over_closed_instance(provider_port):
    """AstrBot 不在 terminate 时清理 embedding_provider_insts → 列表里旧实例已关闭。"""
    stale = platform_provider(closed=True)
    live = platform_provider(closed=False)
    ctx = FakeContext(FakeManager(embeddings=[stale, live]))

    auto = provider_port.resolve_embedding_provider(ctx)
    assert auto is not None and auto._provider is live

    by_id = provider_port.resolve_embedding_provider(ctx, "emb-1")
    assert by_id is not None and by_id._provider is live


def test_resolve_honours_exclude(provider_port):
    only = platform_provider()
    ctx = FakeContext(FakeManager(embeddings=[only]))
    assert provider_port.resolve_embedding_provider(ctx, "emb-1") is not None
    assert (
        provider_port.resolve_embedding_provider(
            ctx, "emb-1", exclude={id(only)}
        )
        is None
    )


@pytest.mark.asyncio
async def test_vector_manager_refresh_skips_failed_instance(vm_module):
    stale = platform_provider(closed=True)
    manager = FakeManager(embeddings=[stale])
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=FakeContext(manager)
    )
    await vm.initialize()
    assert vm.embedding_provider._provider is stale

    live = platform_provider(closed=False)
    manager.embedding_provider_insts = [stale, live]
    manager.inst_map = {"emb-1": stale}

    await vm.refresh(force=True, exclude={id(stale)})
    assert vm.embedding_provider._provider is live


@pytest.mark.asyncio
async def test_vector_manager_rebuilds_client_when_only_closed_instance(vm_module):
    stale = platform_provider(closed=True)
    manager = FakeManager(embeddings=[stale])
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=FakeContext(manager)
    )
    await vm.initialize()
    failed = vm.embedding_provider

    await vm.refresh(force=True, exclude={id(stale)})
    assert vm.embedding_provider is None

    assert await vm.rebuild_embedding_provider(failed) is True
    assert vm.embedding_provider is not None
    assert vm.embedding_provider._provider is not stale
    assert vm.embedding_provider._provider.client.is_closed is False
    assert vm.rebuilt_count == 1


@pytest.mark.asyncio
async def test_embed_text_recovers_by_rebuilding_closed_client(
    plugin, vector_port, vm_module
):
    """生产复现场景：仅有一个已被关闭的平台实例，重解析无效 → 重建 client 恢复。"""
    stale = platform_provider(closed=True)
    manager = FakeManager(embeddings=[stale])
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=FakeContext(manager)
    )
    await vm.initialize()
    plugin._vector_manager = vm
    plugin._cfg.embed_dim = 4

    vec = await vector_port.embed_text(plugin, "hello")

    assert vec is not None and len(vec) == 4
    assert plugin._embed_ok_count == 1
    assert plugin._embed_provider_fail_count == 0
    assert vm.rebuilt_count == 1
    assert vm.embedding_provider._provider is not stale
    assert vm.embedding_provider._provider.client.is_closed is False


@pytest.mark.asyncio
async def test_embed_text_recovery_uses_duplicate_without_rebuild(
    plugin, vector_port, vm_module
):
    """列表中存在可用重复实例时，重解析换实例即可，无需重建。"""
    stale = platform_provider(closed=True)
    live = platform_provider(closed=False)
    manager = FakeManager(embeddings=[stale])
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=FakeContext(manager)
    )
    await vm.initialize()
    plugin._vector_manager = vm
    plugin._cfg.embed_dim = 4

    manager.embedding_provider_insts = [stale, live]
    manager.inst_map = {"emb-1": stale}

    vec = await vector_port.embed_text(plugin, "hello")

    assert vec is not None and len(vec) == 4
    assert vm.embedding_provider._provider is live
    assert vm.rebuilt_count == 0
    assert plugin._embed_provider_fail_count == 0


class WrappedConnectionErrorProvider(FakePlatformEmbeddingProvider):
    """部分 openai 版本把已关闭 client 包装成 “Connection error.”。"""

    async def get_embedding(self, text):
        if self.client.is_closed:
            raise RuntimeError("Connection error.")
        return await super().get_embedding(text)


@pytest.mark.asyncio
async def test_embed_text_recovers_when_error_message_wrapped(
    plugin, vector_port, vm_module
):
    """异常文案不含 closed 时，靠实例 client 关闭状态仍能触发自愈。"""
    stale = WrappedConnectionErrorProvider({"id": "emb-1", "embedding_dimensions": 4})
    stale.client.is_closed = True
    manager = FakeManager(embeddings=[stale])
    vm = vm_module.VectorManager(
        ":memory:", {"embedding_provider_id": "emb-1"}, context=FakeContext(manager)
    )
    await vm.initialize()
    plugin._vector_manager = vm
    plugin._cfg.embed_dim = 4

    vec = await vector_port.embed_text(plugin, "hello")

    assert vec is not None and len(vec) == 4
    assert vm.rebuilt_count == 1
    assert plugin._embed_provider_fail_count == 0
