# Daimon personal-memory projection API v1

## Purpose and authority

This API gives Daimon Matrix a narrow local boundary for projecting its
policy-authorized personal-memory view into HMK retrieval. “Matrix” here means
the `daimon-matrix` component; the protocol has no dependency on Matrix.org.

Daimon Matrix remains authoritative for `/me`, memory identity, event order,
category, correction/retraction, classification and projection policy. HMK
stores a disposable retrieval view plus local receipts. A projected chapter is
never HMK-native authority and cannot become collective memory by implication.

The boundary accepts only canonical JSON on standard input and emits canonical
JSON on standard output. It does not accept paths, URLs, database handles,
SQL, credentials, keys, session/prompt state or network endpoints. Statement
text is inert content; the caller must already have authorized it for the
declared classification.

## Stable identity and namespace

The namespace identity is exactly:

```text
(source_instance, subject_me_id, projector_id, projector_version)
```

Within it, projection identity adds `memory_id`. IDs are SHA-256-derived from
canonical bytes with domain separation. Titles, slugs, shelves, tags, paths,
semantic similarity and local chapter IDs have no authority.

Every request binds the intended HMK instance, API version and SQLite schema
version. The local operator supplies the logical instance ID through
`--instance-id` or `HMK_INSTANCE_ID`; a mismatch fails closed.

## Operations

`project` creates the first active view. Its head must be sequence 1 with null
predecessors. `advance` requires the exact current event ID/hash and the next
contiguous sequence. It updates only the projected view and preserves the old
receipt/history. `retract` has the same predecessor rule, removes the row from
FTS retrieval and retains its provenance, last statement reference and audit
history.

Source checkpoints cannot regress. Equal checkpoint sequence with a different
hash is a fork. A repeated idempotency key with different request bytes is a
conflict. A semantically identical already-applied head can be recovered under
a new transport request and returns the original receipt; the effect is not
executed twice.

The supported commands are:

```bash
python3 scripts/daimon_projection.py --instance-id hmk:my-agent apply < request.json
python3 scripts/daimon_projection.py --instance-id hmk:my-agent inspect < query.json
python3 scripts/daimon_projection.py --instance-id hmk:my-agent verify < query.json
python3 scripts/daimon_projection.py --instance-id hmk:my-agent rebuild-plan < request.json
python3 scripts/daimon_projection.py --instance-id hmk:my-agent rebuild-apply < apply.json
```

Inputs must match their canonical compact UTF-8 encoding (one optional final
newline is accepted). Unknown fields, duplicate keys, floats, non-NFC strings,
unsafe integers and oversized documents fail before a transaction begins.
Diagnostics contain only a stable code.

## Rebuild protocol

Rebuild is deliberately two-phase:

1. `rebuild-plan` validates one exact namespace, checkpoint, sorted manifest,
   destination collisions and the current generation/hash.
2. `rebuild-apply` validates the content-derived plan ID and confirms that the
   namespace has not drifted before replacing that namespace atomically.

The plan never names a shelf, filesystem root or author-wide deletion target.
An empty manifest clears only the exact namespace. HMK-native, Wiki-indexed and
other subject/projector namespaces survive unchanged. Accepted source
high-water never moves backwards. History and prior receipts are retained even
when their active rows are rebuilt.

## Retrieval and publication

`memoryctl search` and `expand` keep their existing fields and add `origin`.
For a projected row it contains `kind: daimon-projection`, source and subject,
author, memory/category, exact event head, statement hash/media type/
classification, checkpoint, projector and active state. A normal row reports
its HMK `source_kind`; a misleading tag cannot impersonate provenance.

Projection chapters are FTS-readable but embedding-disabled. Generic ingest,
update, delete, chapter clearing and link mutation refuse projection-managed
rows. `publish_collective.py` accepts only `source_kind: text`, so projections
are excluded even if a caller fabricates the publication tag and allowlists
the projection shelf. Eventual publication requires a separate reviewed
derivation retaining Matrix provenance.

## Schemas and vectors

Closed Draft 2020-12 schemas live under
`schemas/daimon-projection/v1/`. Deterministic requests, receipts, queries,
rebuild artifacts and one negative semantic-conflict case live under
`vectors/daimon-projection/v1/`. `index.json` binds every file by SHA-256 and
defines scenario order.

Regenerate them with:

```bash
python3 scripts/generate_daimon_projection_vectors.py
```

Tests regenerate the corpus under different hash seeds, timezones and locales
and require byte-identical output. Downstream adapters should pin a released
HMK commit and validate these vectors before enabling writes.

## Migration, backup and rollback

Schema v1 is installed in one SQLite transaction. A failure leaves no partial
Daimon tables; retrying is safe. An unknown stored schema version is refused,
not downgraded. Existing projection tables are checked for all required
provenance columns.

Before enabling the writer on an existing HMK workspace, make a SQLite backup
through the backup API and verify both `PRAGMA integrity_check` and
`PRAGMA foreign_key_check`. A normal HMK backup includes projection rows,
history, idempotency receipts and rebuild receipts.

Before any projections exist, rollback may remove the unused API and tables.
After use, do not drop or reinterpret the tables. Disable the writer, retain
the database/backup as audit evidence, and ship a forward migration. Active
projection chapters may be made retrieval-invisible only through exact
retractions or a verified empty-namespace rebuild.

## Verification checklist

```bash
python3 -m py_compile scripts/memoryctl.py scripts/daimon_projection.py \
  scripts/generate_daimon_projection_vectors.py
pytest -q
python3 scripts/generate_daimon_projection_vectors.py --output /tmp/hmk-vectors
```

No live Matrix store, live agent memory, model, embedding service, network API
or collective corpus is required by the tests.
