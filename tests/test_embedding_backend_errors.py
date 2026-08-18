"""A dead embedding backend must not be able to kill the gateway.

memoryctl is imported as a library by the hmk-memory plugin, which runs inside
the Hermes gateway process, so `SystemExit` from a backend failure terminates
the agent. Upstream converted the hosted providers (nvidia/gemini/google) to
`EmbeddingBackendError`; these cover the ollama provider, where the backend is
typically a *separate machine* — a LAN GPU host that reboots, loses power, or
restarts its container — so transport failure is the expected case, not an edge
one.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest

MEMORYCTL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "memoryctl.py"


def _load_memoryctl():
    name = "memoryctl_backend_errors_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, MEMORYCTL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mc(monkeypatch):
    module = _load_memoryctl()
    monkeypatch.setattr(module, "read_env_key",
                        lambda k, *a, **kw: "http://127.0.0.1:59999" if k == "HERMES_EMBED_OLLAMA_URL" else None)
    return module


def _urlopen_raising(exc):
    def _open(*_a, **_kw):
        raise exc
    return _open


def test_unreachable_ollama_raises_backend_error_not_system_exit(mc, monkeypatch):
    """The embed host being down is the common case — a reboot, or power loss."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        _urlopen_raising(urllib.error.URLError("connection refused")))
    with pytest.raises(mc.EmbeddingBackendError) as ei:
        mc.embed_texts_ollama(["hello"])
    assert "ollama embedding request failed" in str(ei.value)


def test_unreachable_ollama_is_not_a_systemexit(mc, monkeypatch):
    """Explicit: SystemExit here would terminate the gateway process."""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        _urlopen_raising(OSError("host unreachable")))
    with pytest.raises(Exception) as ei:
        mc.embed_texts_ollama(["hello"])
    assert not isinstance(ei.value, SystemExit)


def test_malformed_ollama_response_raises_backend_error(mc, monkeypatch):
    import io
    import urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(b'{"nope": 1}'))
    with pytest.raises(mc.EmbeddingBackendError) as ei:
        mc.embed_texts_ollama(["hello"])
    assert "missing embeddings" in str(ei.value)


def test_short_ollama_response_raises_backend_error(mc, monkeypatch):
    import io
    import urllib.request

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b'{"embeddings": [[0.1, 0.2]]}'))
    with pytest.raises(mc.EmbeddingBackendError) as ei:
        mc.embed_texts_ollama(["one", "two"])
    assert "unexpected ollama embedding response size" in str(ei.value)


def test_unsupported_provider_raises_backend_error(mc):
    with pytest.raises(mc.EmbeddingBackendError):
        mc.embed_texts("no-such-provider", ["hello"])
