"""Run with Hermes' scripts/run_tests.sh: real discovery, SQLite and local embeddings.

Only the auxiliary model response is supplied locally; memory paths and lifecycle
are the production implementation. No model API or user data is touched.
"""
from contextlib import contextmanager
from pathlib import Path
import json
import shutil
import sqlite3

import pytest

pytest.importorskip("agent.memory_manager")
from agent.memory_manager import MemoryManager
from agent.secret_scope import set_secret_scope, reset_secret_scope
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from plugins.memory import load_memory_provider

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def scope(home):
    token = set_hermes_home_override(home)
    secret = set_secret_scope({}, profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(secret)
        reset_hermes_home_override(token)


def install(home):
    home.mkdir()
    plugin = home / "plugins/hmk-memory"
    shutil.copytree(ROOT, plugin, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text("memory:\n  provider: hmk-memory\n  memory_enabled: false\n  user_profile_enabled: false\n  hmk:\n    threshold: 0\n", encoding="utf-8")
    with scope(home):
        provider = load_memory_provider("hmk-memory")
        assert provider is not None
        manager = MemoryManager(external_prefetch_timeout=30)
        manager.add_provider(provider)
        assert manager.initialize_for_agent(session_id="test", hermes_home=str(home))
    return manager, provider


def test_profile_crud_persistence_and_semantic_recall(tmp_path, monkeypatch):
    # A stale launch environment must never redirect either profile's database.
    monkeypatch.setenv("HMK_DB_PATH", str(tmp_path / "wrong.db"))
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(tmp_path / "wrong"))
    a, pa = install(tmp_path / "a")
    b, pb = install(tmp_path / "b")
    try:
        with scope(tmp_path / "a"):
            assert {s["name"] for s in a.get_all_tool_schemas()} >= {"remember", "recall", "librarian"}
            prompt = pa.system_prompt_block()
            result = a.handle_tool_call("remember", {"title": "Valeria", "content": "Valeria prefiere viajar en tren y evitar los aviones."})
            assert "Saved" in result
            # No shared keyword: this exercises semantic, rather than exact lexical, retrieval.
            assert pa._get_memoryctl().semantic_search("preferencias de transporte", limit=3)
            assert "Valeria" in a.prefetch_all("Valeria")
            row = pa._get_memoryctl().search("Valeria")[0]
            a.handle_tool_call("librarian", {"action": "update", "chapter_id": row["id"], "content": "Valeria ahora prefiere viajar en barco."})
            assert "barco" in a.handle_tool_call("recall", {"query": "Valeria"})
            assert pa.system_prompt_block() == prompt
        with scope(tmp_path / "b"):
            assert "barco" not in b.handle_tool_call("recall", {"query": "Valeria"})
            assert "Saved" in b.handle_tool_call("remember", {"title": "Bruno", "content": "Bruno prefiere las bicicletas."})
        with scope(tmp_path / "a"):
            assert "bicicletas" not in a.handle_tool_call("recall", {"query": "Bruno"})
            assert "barco" in a.handle_tool_call("recall", {"query": "Valeria"})
            a.shutdown_all()
            reopened = load_memory_provider("hmk-memory")
            reopened.initialize("reopened", hermes_home=str(tmp_path / "a"))
            assert "barco" in reopened.handle_tool_call("recall", {"query": "Valeria"})
            assert "false" in reopened.handle_tool_call("librarian", {"action": "delete", "chapter_id": 999999})
            reopened.handle_tool_call("librarian", {"action": "delete", "chapter_id": row["id"]})
            assert not reopened._get_memoryctl().search("Valeria")
            reopened.shutdown()
        assert not (tmp_path / "wrong.db").exists()
    finally:
        with scope(tmp_path / "a"):
            a.shutdown_all()
        with scope(tmp_path / "b"):
            b.shutdown_all()


def test_session_boundary_import_and_embedding_outage(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    manager, provider = install(home)
    with scope(home):
        try:
            memories = home / "memories"
            memories.mkdir(exist_ok=True)
            (memories / "USER.md").write_text("El usuario vive en Islandia.", encoding="utf-8")
            provider._import_builtin()
            provider._import_builtin()
            mc = provider._get_memoryctl()
            assert len(mc.search("Islandia")) == 1
            # Failed semantic indexing must preserve both durable writes and lexical recall.
            def unavailable(*args, **kwargs):
                raise mc.EmbeddingBackendError("test backend outage")
            monkeypatch.setattr(mc, "embed_texts", unavailable)
            assert "Saved" in manager.handle_tool_call("remember", {"title": "Proyecto", "content": "Proyecto Aurora usa SQLite."})
            assert "Aurora" in manager.handle_tool_call("recall", {"query": "Aurora"})
            monkeypatch.setattr(provider, "_extract_candidates", lambda transcript: [{
                "shelf": "plans", "title": "Decisión", "content": "Aurora se publica los viernes.", "importance": 0.8, "tags": "",
            }])
            transcript = [{"role": role, "content": "Conversamos sobre el proyecto Aurora y su publicación. " * 4}
                          for role in ("user", "assistant", "user", "assistant")]
            manager.commit_session_boundary_async(transcript, new_session_id="next", reason="test")
            assert manager.flush_pending(timeout=10)
            assert "viernes" in manager.handle_tool_call("recall", {"query": "viernes"})
            manager.on_pre_compress(transcript)
            assert len(mc.search("viernes")) == 1
            assert (memories / "USER.md").read_text() == "El usuario vive en Islandia."
            assert Path(provider._db_path).stat().st_mode & 0o777 == 0o600
        finally:
            manager.shutdown_all()
