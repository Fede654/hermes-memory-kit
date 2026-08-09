from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


MEMORYCTL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "memoryctl.py"


class _Vector:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return self._values


def _load_memoryctl():
    name = "memoryctl_embedding_device_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MEMORYCTL_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_local_embedding_device_is_explicit_and_part_of_cache_key(
    monkeypatch, env_isolation
):
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model, *, device):
            calls.append((model, device))
            self.device = device

        def encode(self, texts, **kwargs):
            assert kwargs == {
                "normalize_embeddings": True,
                "convert_to_numpy": True,
            }
            value = 1.0 if self.device == "cpu" else 2.0
            return [_Vector([value]) for _ in texts]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    memoryctl = _load_memoryctl()
    memoryctl.LOCAL_MODEL_CACHE.clear()

    monkeypatch.setenv("HERMES_EMBED_DEVICE", " CPU ")
    assert memoryctl.embed_texts_local(["hello"], model="BAAI/bge-m3") == [[1.0]]
    assert memoryctl.embed_texts_local(["again"], model="BAAI/bge-m3") == [[1.0]]

    monkeypatch.setenv("HERMES_EMBED_DEVICE", "cuda")
    assert memoryctl.embed_texts_local(["hello"], model="BAAI/bge-m3") == [[2.0]]

    assert calls == [("BAAI/bge-m3", "cpu"), ("BAAI/bge-m3", "cuda")]
    assert set(memoryctl.LOCAL_MODEL_CACHE) == {
        ("BAAI/bge-m3", "cpu"),
        ("BAAI/bge-m3", "cuda"),
    }


def test_local_embedding_device_defaults_to_cuda(monkeypatch, env_isolation):
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model, *, device):
            calls.append((model, device))

        def encode(self, texts, **kwargs):
            return [_Vector([3.0]) for _ in texts]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    memoryctl = _load_memoryctl()
    memoryctl.LOCAL_MODEL_CACHE.clear()

    assert memoryctl.embed_texts_local(["hello"], model="plain-model") == [[3.0]]
    assert calls == [("plain-model", "cuda")]
