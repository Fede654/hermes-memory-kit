# Architecture

Hermes Memory Kit separates:

1. HMK-native canon (`library.db`)
2. retrieval (`memoryctl.py`)
3. ingestion (`ingest_any.py`)
4. disposable HMK projection (`export_obsidian.py`)
5. skill (`librarian`)

It can also index an independently authored LLM Wiki. In that case the wiki
file is authoritative and the corresponding HMK chapter is a rebuildable
retrieval index.

Do not use the ambiguous word “wiki” for both surfaces:

- `$WIKI_PATH` / `~/wiki` is the authoritative, human-curated LLM Wiki;
- `$HMK_VAULT_DIR` is a generated HMK projection vault and must be disjoint.

See [the memory ownership contract](memory-ownership-contract.md).
