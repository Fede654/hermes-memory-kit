# Architecture

Hermes Memory Kit separates:

1. HMK-native canon (`library.db`)
2. retrieval (`memoryctl.py`)
3. ingestion (`ingest_any.py`)
4. disposable HMK projection (`export_obsidian.py`)
5. skill (`librarian`)
6. optional reviewed publication (`publish_collective.py`)

It can also index an independently authored LLM Wiki. In that case the wiki
file is authoritative and the corresponding HMK chapter is a rebuildable
retrieval index.

Do not use the ambiguous word “wiki” for both surfaces:

- `$WIKI_PATH` / `~/wiki` is the authoritative, human-curated LLM Wiki;
- `$HMK_VAULT_DIR` is a generated HMK projection vault and must be disjoint.

See [the memory ownership contract](memory-ownership-contract.md).

`publish_collective.py` is deliberately outside retrieval and projection. It
reads HMK in SQLite read-only mode, accepts only dual-opted-in HMK-native
records, produces an exact review plan, and requires an independent approval
bound to that plan before writing derived Markdown to a collective-memory
corpus. See [the publication contract](collective-memory-publication.md).
