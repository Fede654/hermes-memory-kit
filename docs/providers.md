# Embedding Providers

## Supported

- `nvidia` — default
- `google`
- `local`
- `ollama` — local/LAN Ollama server (e.g. `bge-m3`, 1024-dim)

## Variables

### NVIDIA

- `NVIDIA_API_KEY`
- `HERMES_EMBED_NVIDIA_MODEL` — default `nvidia/llama-3.2-nemoretriever-300m-embed-v1` (2048 dims, fixed)

### Google / Gemini

- `GEMINI_API_KEY` or `GOOGLE_API_KEY`
- `HERMES_EMBED_GOOGLE_MODEL` — default `gemini-embedding-001`
- `HERMES_EMBED_GOOGLE_OUTPUT_DIMS` — default `768` (MRL-truncatable from 3072)

### Local

- `HERMES_EMBED_LOCAL_MODEL` — default `sentence-transformers/all-MiniLM-L6-v2`
- `HERMES_EMBED_DEVICE` — device passed to `SentenceTransformer`; defaults to
  `cuda`. Set `cpu` when several agent processes would otherwise load separate
  copies of the model into VRAM. Model instances are cached per model and
  device, so changing this setting in a process selects a distinct encoder.

### Ollama

- `HERMES_EMBED_OLLAMA_URL` — Ollama base URL, default `http://127.0.0.1:11434` (uses `/api/embed`)
- `HERMES_EMBED_OLLAMA_MODEL` — default `bge-m3` (1024 dims)
- No API key; the model must be pulled on the Ollama server. Good fit for a
  GPU/LAN-local embed server.

## Default

The toolkit starts with `nvidia` as the default provider. See the benchmark-based rationale below.

## NVIDIA vs Google — benchmark-based rationale

A benchmark harness (`scripts/embed_benchmark.py`) is provided so you can A/B test providers against your own corpus before deciding whether to switch. The default stays at `nvidia` based on the following analysis against a typical kit corpus of ~200 chapters:

**Storage**: Google at 768 dims uses ~62% less disk than NVIDIA at 2048 dims (e.g. 650 KB vs 1.7 MB for 211 chapters). Meaningful only at scale; imperceptible for small personal libraries.

**Retrieval quality**: on ~14 representative queries (specific topics + broad topics + negative controls), both providers surfaced the same top results for precise queries and diverged with roughly equal quality on broad queries. Neither dominated.

**Latency**: NVIDIA Nemoretriever tested faster end-to-end, especially in `hybrid-pack` mode. The gap is partly artifact of Google's free-tier rate limiting; on a paid Gemini tier the gap should shrink. On semantic-only search the difference is small (~10%).

**Conclusion**: unless your corpus is large enough that storage matters (say 10K+ chapters) or you already have a paid Gemini tier, `nvidia` remains the pragmatic default. Run the benchmark on your own corpus if in doubt.

### Running the benchmark yourself

See [`benchmarks/README.md`](benchmarks/README.md) for step-by-step instructions. The harness produces reports in `$HOME/hmk-benchmarks/` (outside any repo, safe by design). Corpus-specific data is never meant to be committed to the kit.

## Switching providers at runtime

No re-embedding is needed to *switch* retrieval — each `memoryctl.py` subcommand accepts `--provider` and `--model` flags that pick the matching set from `chapter_embeddings`. To *store* a different set, run:

```bash
./scripts/hmk memoryctl.py embed-backfill --provider <p> --model <m> --all
```

Multiple sets can coexist; selection at query time is via `--provider/--model` or the `HERMES_EMBED_PROVIDER` env var.

## Important Rule

Do not treat embedding scores from different models as directly comparable. The DB supports multiple sets per `(chapter_id, provider, model)`, but each retrieval should query a consistent provider/model pair.

## Schema note

The primary key of `chapter_embeddings` is `(chapter_id, provider, model)` — it does **not** include `dims`. If you re-embed the same `(provider, model)` pair at a different dimension, the previous set is overwritten. When switching Gemini from 768 → 1536 (or vice versa), clear the old set first via `scripts/embed_clear.py` to avoid silent dimension mixing.
