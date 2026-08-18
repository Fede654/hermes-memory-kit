# HMK to collective-memory publication

## Boundary

This is a publication adapter, not database synchronization.

- HMK remains authoritative for records authored natively in HMK.
- An LLM Wiki file remains authoritative when HMK only indexes that file.
- collective-memory receives reviewed, derived Markdown artifacts.
- The adapter never reads through a network API, changes the HMK database, or
  writes into collective-memory's code or index database.

The destination corpus and publication state are separate roots. Publication
state contains receipts and tombstones and should not itself be indexed.

## Fail-closed selection

A chapter is publishable only when all of these are true:

1. Its policy entry uses `action: publish`.
2. The collection, shelf, classification, and license are allowlisted.
3. Classification is `public` or `tribe-shared`; `private` cannot be
   allowlisted.
4. Consent is exactly `explicit`.
5. The chapter also carries the `collective:publish` tag. This is the second,
   record-local opt-in.
6. `source_kind` is `text` and `source_path` is empty, proving that HMK is the
   source rather than a retrieval index for a Wiki/file source.
7. The reject-only redaction scanner finds no credential-shaped content.

The adapter does not redact automatically. Silent substitution could change
meaning while making the artifact look reviewed. A rejected chapter must be
edited or distilled in HMK and reviewed again.

The example closed policy is
[`policies/collective-publication.example.json`](../policies/collective-publication.example.json).
Copy it outside the repository, set real principals/collections, and add
individual chapter entries. Unknown fields and unsafe paths are rejected.

## Review and publish flow

Planning is the default-safe operation:

```bash
python3 scripts/publish_collective.py plan \
  --database /path/to/library.db \
  --policy /path/to/collective-publication.json
```

Without `--plan-out`, this prints only a count and digest and changes nothing.
To create the private review artifact:

```bash
python3 scripts/publish_collective.py plan \
  --database /path/to/library.db \
  --policy /path/to/collective-publication.json \
  --plan-out /private/review/publication-plan.json
```

The mode-0600 plan contains the exact Markdown proposed for publication.
Reviewers must inspect that content and supply a separate closed approval:

```json
{
  "schema": "hmk-collective-publication-approval/v1",
  "plan_sha256": "<exact digest printed by plan>",
  "decision": "approved",
  "reviewer_principal": "reviewer@example",
  "approved_at": "2026-07-31T00:00:00Z"
}
```

The reviewer principal must differ from the publisher principal. The adapter
does not generate approvals. Current v1 approvals are local attestations, not
cryptographic signatures; a later version should bind them to separate GitHub
App identities or detached signatures.

Publish the already-reviewed bytes:

```bash
python3 scripts/publish_collective.py publish \
  --database /path/to/library.db \
  --policy /path/to/collective-publication.json \
  --plan /private/review/publication-plan.json \
  --approval /private/review/publication-approval.json \
  --destination /path/to/collective-corpus \
  --state-dir /private/state/hmk-collective-publication
```

Before writing, publish revalidates the policy, approval, source chapter hash,
artifact hash, target path, previous receipt state, and on-disk target. A
different source or target is a hard conflict. An exact artifact is unchanged;
an update from the previously receipted hash is replaced atomically. If state
or receipt publication fails, corpus changes are rolled back.

## Artifact and collective-memory mapping

Each source URI is stable:

```text
hmk://<source-instance>/chapters/<chapter-id>
```

Its filename is the first 24 hexadecimal characters of the URI's SHA-256.
The Markdown frontmatter records source URI, author/publisher principals,
source timestamps, content hash, classification, consent, license, collection,
and derivation chain. The body starts with a normal H1 and can be ingested by
collective-memory without an API or schema change.

With the example `path_prefix: shared-knowledge/hmk`, current
collective-memory behavior is:

| collective-memory surface | Adapter result |
|---|---|
| corpus | one reviewed `.md` file under the configured prefix |
| project/map node | first path component, here `shared-knowledge` |
| kind | `fs_doc` |
| title | first H1 from the generated artifact |
| Atlas | normal semantic document node and neighbors |
| discovery | eligible as primary evidence; findings remain proposals subject to collective-memory review |

The collective index remains a rebuildable view. The receipt, not the index,
is the publication audit record.

## Revocation

Replace a publish entry with a closed revoke entry:

```json
{
  "action": "revoke",
  "chapter_id": 42,
  "collection": "shared-knowledge",
  "author_principal": "author@example",
  "revocation_reason": "source owner withdrew consent"
}
```

Plan and approve it exactly like a publication. Apply removes only an artifact
whose hash still matches the last receipt and writes a content-free tombstone
to the private state. Drift blocks deletion. Rebuild the collective-memory
index after publication or revocation so its atomic index no longer references
the old corpus generation.

## Ownership and rollout

The adapter/exporter is owned here. collective-memory's filesystem policy,
indexer, ACLs, service users, and runtime remain Mariano's review domain.
No live corpus should be targeted until:

1. this contract and its tests are reviewed;
2. Mariano confirms the destination collection and reindex trigger;
3. publisher and reviewer principals are assigned to distinct operators;
4. a synthetic publish/revoke drill succeeds in an isolated corpus.
