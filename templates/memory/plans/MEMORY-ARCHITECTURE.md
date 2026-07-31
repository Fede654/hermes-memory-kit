# MEMORY-ARCHITECTURE

Layers:

- identity
- state
- plans
- episodes
- library
- evidence

Rules:

- assign one authority per artifact class
- HMK-native records: HMK first, projection after
- LLM Wiki records: wiki first, HMK retrieval index after
- never overlap the generated projection vault with `$WIKI_PATH`
- raw only on demand
- `null_retrieval` is valid
