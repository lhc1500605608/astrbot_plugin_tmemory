"""本地 embedding 集成与降级测试（Plan TMEAAA-379 B3）。"""

import pytest


def test_local_provider_raises_when_model_missing(tmp_path, plugin_module):
    from astrbot_plugin_tmemory.embeddingProvider import LocalBgeEmbeddingProvider

    with pytest.raises(RuntimeError):
        LocalBgeEmbeddingProvider(str(tmp_path), "model_quantized.onnx", 512)


@pytest.mark.asyncio
async def test_vector_manager_local_fallback_to_none(plugin_module):
    from astrbot_plugin_tmemory.vector_manager import VectorManager

    vm = VectorManager(
        ":memory:",
        {
            "embedding_source": "local",
            "local_embedding_path": "/nonexistent/bge-model",
        },
    )
    await vm.initialize()
    assert vm.embedding_provider is None
    assert vm.source == "none"
    assert "local embedding" in vm.fallback_reason


@pytest.mark.asyncio
async def test_vector_manager_local_success(monkeypatch, plugin_module):
    import astrbot_plugin_tmemory.vector_manager as vm_mod

    class _FakeLocalProvider:
        source = "local"
        model_name = "fake-bge"

        def __init__(self, *args, **kwargs):
            pass

        def get_dim(self):
            return 512

        async def embed_text(self, text):
            return [0.0] * 8

        async def embed_batch(self, texts):
            return [[0.0] * 8 for _ in texts]

        async def close(self):
            return None

    monkeypatch.setattr(vm_mod, "LocalBgeEmbeddingProvider", _FakeLocalProvider)
    vm = vm_mod.VectorManager(
        ":memory:",
        {"embedding_source": "local", "local_embedding_path": "data/bge-small-zh-v1.5"},
    )
    await vm.initialize()
    assert vm.source == "local"
    assert vm.embedding_provider is not None
    assert vm.provider_dim == 512
    status = vm.status()
    assert status["active_source"] == "local"
    assert status["local_embedding_path"] == "data/bge-small-zh-v1.5"
