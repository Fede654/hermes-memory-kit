# Memory ownership contract v1

This contract removes the ambiguous claim that either `library.db` or “the
wiki” is universally canonical. Authority belongs to one store per artifact
class. Other copies are indexes or projections and must be rebuildable from
that authority.

The machine-readable policy is
[`policies/memory-ownership.v1.json`](../policies/memory-ownership.v1.json).

## The two wiki surfaces

They are different products and must never share a target directory:

| Surface | Typical path | Owner | Purpose |
|---|---|---|---|
| LLM Wiki | `$WIKI_PATH`, normally `~/wiki` | Human/agent curation workflow | Authoritative raw evidence and long-form curated knowledge |
| HMK projection vault | `$HMK_VAULT_DIR`, normally a workspace `wiki/` | `export_obsidian.py` | Generated, disposable navigation over HMK-native records |

`export_obsidian.py` refuses any target that equals, contains, or is contained
by the LLM Wiki root. Deleting and rebuilding the HMK projection vault is safe.
Deleting or regenerating the LLM Wiki from HMK is forbidden.

## Authority by artifact

| Artifact | Authority | Other surface |
|---|---|---|
| Raw LLM Wiki evidence | File under `$WIKI_PATH/**/raw/` | HMK `evidence` chapter is a retrieval index |
| Curated LLM Wiki note | File under `$WIKI_PATH` | HMK `library` chapter is a retrieval index |
| HMK-native identity/state/plan/episode/atomic note | `library.db` | HMK projection vault is disposable navigation |
| Live project branch/blocker/task state | Project `MEMORY.md` and GitHub | May be linked, never copied as durable wiki truth |
| Dialogue continuity | Session log and `DIALOGUE-HANDOFF.md` | HMK retrieval may supplement, not replace it |
| Daimon `/me` personal memory | Daimon Matrix signed/evented memory ledger | HMK `daimon-projection` row is a disposable retrieval view |

An HMK `library` shelf can therefore contain both authoritative HMK-native
records and rebuildable indexes of LLM Wiki notes. Shelf alone does not decide
authority. The record's origin does:

- a record created directly through `librarian`/`memoryctl` has
  `origin=native`;
- a record created by `wiki_publish` has a wiki source path and content hash
  in its publication receipt and is an index of that file.
- a record created by `daimon_projection.py` has explicit Matrix source,
  subject/author, memory/head, classification, checkpoint and projector fields;
  its projection history is audit evidence but Matrix remains authoritative.

Until HMK persists origin fields directly, the verified `wiki_publish` receipt,
source path, file hash, tags, and `derived_from` edge are the provenance
boundary. The HMK→collective-memory adapter must carry that origin explicitly
rather than infer it from shelf or title.

## Write paths

### HMK-native memory

Write through `librarian` or `memoryctl`; never through raw SQL. Back up before
schema, provider, bulk rewrite, or destructive maintenance. `export_obsidian`
may project selected native records only to the isolated projection vault.

### LLM Wiki publication

Write drafts under the configured staging directory and publish through
`wiki_publish`. Its transaction:

1. validates frontmatter, taxonomy, links, and raw-body hash;
2. snapshots the HMK database;
3. atomically writes raw/note/index files;
4. creates HMK retrieval-index chapters and the `derived_from` edge;
5. appends the wiki log and verifies the chain;
6. restores both file and database state if any step fails.

The files are authoritative. A failed or missing HMK index is repaired from the
files; it is not a reason to overwrite the files with an older DB chapter.

### Daimon personal-memory projection

Write only through the closed `daimon_projection.py` API. Generic HMK ingest,
update, delete, linking and collective publication reject projection-managed
chapters. Correction/retraction follows Matrix event predecessors; rebuild is
limited to one exact source/subject/projector namespace. See
[the Daimon projection contract](daimon-projection.md).

## Drift resolution

| Drift | Resolution |
|---|---|
| Wiki file hash differs from its declared raw hash | Stop; preserve both versions and investigate source mutation |
| Wiki file differs from its indexed HMK chapter | Re-index HMK from the wiki file after a verified DB snapshot |
| HMK-native record differs from projection note | Rebuild the projection from HMK |
| Daimon projection differs from Matrix source head | Stop retrieval for that namespace and rebuild it from Matrix; never write the HMK value back as source truth |
| Project `MEMORY.md` disagrees with Git branch/runtime | Git/runtime wins for mechanical state; repair `MEMORY.md` |
| Publication receipt or provenance edge is missing | Treat HMK copy as untrusted index; rebuild the full chain through the gate |

Never “merge both” automatically. The authoritative side replaces its replica
after preservation and validation.

## Recovery

- LLM Wiki recovery uses its Git/Obsidian history plus raw-body checksums. HMK
  is not a backup of the wiki.
- HMK recovery uses a SQLite backup API snapshot with integrity and
  foreign-key checks. A wiki index is not a database backup.
- HMK projection recovery deletes/rebuilds the isolated projection vault.
- Daimon projection recovery verifies its receipts/checkpoint and atomically
  rebuilds one namespace from Matrix.
- Project operational recovery reconciles GitHub, Git, runtime, then
  `MEMORY.md` in that order.

## Publication boundary for collective memory

Collective memory is a downstream publication surface, not another authority.
The adapter publishes immutable, provenance-bearing versions:

- wiki-origin records cite the authoritative wiki path/hash and their evidence
  edge;
- HMK-native records cite the HMK record ID, content hash, and memory policy;
- updates create a new version and supersession edge;
- deletions become tombstones, never silent disappearance.

Daimon projection rows are excluded from the current HMK-native publication
adapter. Publication, if later designed, must be a distinct reviewed derivation
that preserves Matrix provenance and consent.
