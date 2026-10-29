# Hermes v0.22 integration

The repository root is an installable `memory-provider` plugin. Install an exact
commit with Hermes' native plugin installer; its PM prepares `model2vec` from
`plugin.yaml`. The older template remains usable by standalone HMK workspaces.

The provider binds its SQLite database to `<HERMES_HOME>/hmk-memory/library.db`.
It reads `memory.hmk` in that profile's `config.yaml`; process-wide HMK path and
embedding environment variables never select its database or backend.

```yaml
memory:
  provider: hmk-memory
  memory_enabled: false
  user_profile_enabled: false
  hmk:
    embedding_provider: model2vec
    embedding_model: minishlab/potion-multilingual-128M
    distill_enabled: true
    budget_tokens: 1500
    limit: 8
    threshold: 0.30
```

Local CPU embeddings require an initial public model download, then use the
local model cache. No memory-service key is required. The first installation
probe warms the model and checks SQLite integrity before Hermes changes its
memory selection. If the embedding backend later fails, durable writes remain
in SQLite and recall falls back to lexical search. Indexing catches up on the
next successful write or installation probe.

`remember` writes facts, `recall` retrieves them, and `librarian` searches,
expands, updates and deletes records. Provider instructions are static for a
session; dynamic recall uses Hermes' prefetch channel. Native MEMORY.md and
USER.md are imported once and kept intact on disk. Re-enabling native memory
restores those original files; later HMK-only writes remain in SQLite.

Session-end and pre-compression extraction use Hermes' configured auxiliary LLM, with optional
`distill_provider`, `distill_model` and `distill_timeout` overrides under
`memory.hmk`. It can incur normal model usage. Hermes owns serialization and
shutdown draining; this plugin launches no detached extraction threads.
Set `distill_enabled: false` to use deliberate writes only.

The embedded provider currently supports Unix hosts (memoryctl uses flock).
No cross-profile sharing is enabled. Configuration and memory data stay out of
Git. Deleting a profile removes its memory along with the rest of that home.

## Validation

From a Hermes checkout, with the test environment prepared:

```sh
scripts/run_tests.sh /path/to/hermes-memory-kit/tests/
```

The runtime integration tests require real Hermes imports and the public local
embedding model. They cover A→B→A database isolation, tool discovery, CRUD,
persistence after reopen, stable system instructions, native-summary import,
embedding outages and serialized session-end extraction. Only the extraction
model response is supplied locally; no paid model requests are made.
