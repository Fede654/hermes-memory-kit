"""Hermes v0.22+ adapter: bind a separate memoryctl module to each provider instance.

The command-line toolkit keeps its env-based interface. The embedded provider uses
memory.hmk in config.yaml, never mutates process env, and never shares DB globals.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sqlite3
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_LOG = logging.getLogger(__name__)
# Encoders contain model weights only, never profile config, credentials or memories.
_ENCODERS = {}
_ENCODER_LOCK = threading.Lock()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_legacy = _load(__name__ + "._legacy", _ROOT / "legacy.py")


class HMKMemoryProvider(_legacy.HMKMemoryProvider):
    def is_available(self):
        return (_ROOT / "memoryctl.py").is_file()

    def initialize(self, session_id, **kwargs):
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config_readonly

        self._hermes_home = Path(kwargs.get("hermes_home") or get_hermes_home()).resolve()
        self._session_id = session_id
        self._config = dict(load_config_readonly().get("memory", {}).get("hmk", {}))
        self._interlocutor = self._slug_interlocutor(
            kwargs.get("chat_name"), kwargs.get("user_name"), kwargs.get("user_id"))
        self._base = self._hermes_home / "hmk-memory"
        self._base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db_path = str(self._base / "library.db")
        self._memoryctl = self._make_memoryctl()
        self._memoryctl.init_db()
        Path(self._db_path).chmod(0o600)
        self._engram_available = self._check_engram_columns()
        self._retriever = "engram_pack" if self._engram_available else "hybrid_pack"
        self._limit = int(self._config.get("limit", 8))
        self._threshold = float(self._config.get("threshold", 0.30))
        self._budget = int(self._config.get("budget_tokens", 1500))
        self._shelves = self._config.get("shelves") or None
        self._quotas = dict(self.DEFAULT_QUOTAS)
        self._import_builtin()

    def _setting(self, name, default=None):
        key = name.removeprefix("HMK_").lower()
        return self._config.get(key, default)

    def _make_memoryctl(self):
        mc = _load(__name__ + ".memoryctl", _ROOT / "memoryctl.py")
        policy = _load(__name__ + ".corpus_policy", _ROOT / "corpus_policy.py")
        mc.BASE_DIR = self._base
        mc.DB_PATH = Path(self._db_path)
        mc.HERMES_HOME = self._hermes_home
        mc.HERMES_ENV_PATH = None
        mc.WORKSPACE_ROOT = self._hermes_home
        mc.META_PATH_PREFIXES = [str(self._hermes_home)]
        mc.META_EXACT_PATHS = set()
        mc.BIBLIOTECA_PREFIX = ""
        mc.scan_content_for_secrets = policy.scan_content_for_secrets
        mc.should_block_file = policy.should_block_file
        mc.classify_source_kind = policy.classify_source_kind
        settings = {
            "HERMES_EMBED_PROVIDER": self._config.get("embedding_provider", "model2vec"),
            "HERMES_EMBED_MODEL": self._config.get("embedding_model", "minishlab/potion-multilingual-128M"),
            "HERMES_EMBED_DEVICE": "cpu",
            "HERMES_RERANK_PROVIDER": "none",
            "HERMES_EMBED_OLLAMA_URL": self._config.get("ollama_url", "http://127.0.0.1:11434"),
        }

        def scoped_key(name):
            if name in settings:
                return settings[name]
            if name.endswith(("_API_KEY", "_TOKEN")):
                from agent.secret_scope import get_secret
                return get_secret(name)
            return None

        mc.read_env_key = scoped_key
        mc.MODEL2VEC_CACHE = _ENCODERS
        encode = mc.embed_texts_model2vec

        def local_encode(*args, **kwargs):
            with _ENCODER_LOCK:
                try:
                    return encode(*args, **kwargs)
                except Exception as exc:
                    raise mc.EmbeddingBackendError(str(exc)) from exc

        mc.embed_texts_model2vec = local_encode
        semantic_search = mc.semantic_search

        def available_search(*args, **kwargs):
            try:
                return semantic_search(*args, **kwargs)
            except mc.EmbeddingBackendError as exc:
                _LOG.warning("HMK semantic search unavailable; using lexical recall: %s", exc)
                return []

        mc.semantic_search = available_search
        return mc

    def _check_engram_columns(self):
        with sqlite3.connect(self._db_path) as con:
            return any(row[1] == "engram_type" for row in con.execute("PRAGMA table_info(chapters)"))

    def _get_memoryctl(self):
        return self._memoryctl

    def handle_tool_call(self, tool_name, args, **kwargs):
        try:
            return super().handle_tool_call(tool_name, args, **kwargs)
        except (SystemExit, ValueError, TypeError, sqlite3.Error) as exc:
            # memoryctl's CLI validation raises SystemExit, which must not kill a gateway.
            return json.dumps({"success": False, "error": str(exc)})

    def on_session_end(self, messages):
        if self._config.get("distill_enabled", True):
            # MemoryManager owns async serialization and draining at session boundaries.
            # A detached thread here would outlive shutdown and race session switching.
            self._run_distill(messages)

    def on_pre_compress(self, messages):
        self.on_session_end(messages)
        return ""

    def _persist_candidates(self, candidates):
        """Deduplicate against exact stored text, not an uncalibrated hybrid score."""
        mc = self._get_memoryctl()
        count = 0
        for item in candidates:
            with mc.connect() as con:
                exists = con.execute("SELECT 1 FROM chapters WHERE raw=?", (mc.normalize_text(item["content"]),)).fetchone()
            if exists:
                continue
            mc.add_text(item["shelf"], item["title"], item["content"],
                        tags=["auto-distilled"], importance=item["importance"], replace=False)
            count += 1
        if count:
            try:
                mc.backfill_embeddings(only_missing=True)
            except mc.EmbeddingBackendError as exc:
                _LOG.warning("HMK memories saved; semantic indexing pending: %s", exc)
        return count

    def _import_builtin(self):
        """Import retained native summaries once; never remove or rewrite the originals."""
        mc = self._get_memoryctl()
        with mc.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS hermes_imports (filename TEXT PRIMARY KEY)")
        for filename, shelf in (("MEMORY.md", "library"), ("USER.md", "identity")):
            path = self._hermes_home / "memories" / filename
            if not path.is_file():
                continue
            with mc.connect() as con:
                if con.execute("SELECT 1 FROM hermes_imports WHERE filename=?", (filename,)).fetchone():
                    continue
            content = path.read_text(encoding="utf-8").strip()
            # Deterministic title + replace=True makes retry after interruption idempotent.
            if content:
                mc.add_text(shelf, "Imported " + filename, content, tags=["native-import"], replace=True)
            with mc.connect() as con:
                con.execute("INSERT OR IGNORE INTO hermes_imports VALUES (?)", (filename,))

    def verify(self):
        """Installation probe: real local embedding plus durable SQLite integrity."""
        mc = self._get_memoryctl()
        vectors = mc.embed_texts(mc.default_embed_provider(), ["memoria persistente"], model=mc.default_embed_model())
        if not vectors or not vectors[0]:
            raise RuntimeError("HMK embedding backend returned no vector")
        mc.backfill_embeddings(only_missing=True)
        with mc.connect() as con:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("HMK database integrity check failed")
        return {"database": self._db_path, "dimensions": len(vectors[0])}
