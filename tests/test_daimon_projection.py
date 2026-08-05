"""Real-SQLite contract tests for the provenance-safe Daimon projection API."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ME_A = "dm:being:v1:" + "A" * 43
ME_B = "dm:being:v1:" + "B" * 43
INSTANCE = "hmk:synthetic-instance"
SOURCE = "matrix:synthetic-instance"
PROJECTOR = {"id": "matrix:personal-memory-projector", "version": "1.0.0"}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    base = tmp_path / "agent-memory"
    base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    monkeypatch.setenv("HMK_INSTANCE_ID", INSTANCE)
    monkeypatch.delenv("HMK_DB_PATH", raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("daimon_projection", None)
    sys.modules.pop("memoryctl", None)
    memoryctl = importlib.import_module("memoryctl")
    projection = importlib.import_module("daimon_projection")
    memoryctl.init_db()
    return memoryctl, projection, projection.ProjectionAPI(INSTANCE), base


def _event(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "hmk-projection:" + label))


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _statement(text: str, classification: str = "protected") -> dict[str, object]:
    wire = text.encode()
    return {
        "sha256": hashlib.sha256(wire).hexdigest(),
        "byte_length": len(wire),
        "media_type": "text/plain",
        "classification": classification,
        "text": text,
    }


def _request(
    projection,
    *,
    label: str,
    operation: str = "project",
    subject: str = ME_A,
    memory_id: str | None = None,
    sequence: int = 1,
    predecessor_label: str | None = None,
    text: str | None = "synthetic orchard memory",
    checkpoint_sequence: int | None = None,
    checkpoint_hash: str | None = None,
) -> dict[str, object]:
    return {
        "schema": projection.REQUEST_SCHEMA,
        "adapter": {"id": projection.ADAPTER_ID, "version": projection.API_VERSION},
        "request_id": _event("request:" + label),
        "idempotency_key": "projection:" + label,
        "operation": operation,
        "target": {
            "instance_id": INSTANCE,
            "api_version": projection.API_VERSION,
            "schema_version": projection.SCHEMA_VERSION,
        },
        "source_instance": SOURCE,
        "subject_me_id": subject,
        "author_me_id": subject,
        "memory_id": memory_id or _event("memory:default"),
        "category": "personal-insight",
        "head": {
            "event_id": _event("head:" + label),
            "event_hash": _digest("head:" + label),
            "sequence": sequence,
            "predecessor_event_id": None
            if predecessor_label is None
            else _event("head:" + predecessor_label),
            "predecessor_hash": None
            if predecessor_label is None
            else _digest("head:" + predecessor_label),
        },
        "statement": None if text is None else _statement(text),
        "projector": copy.deepcopy(PROJECTOR),
        "source_checkpoint": {
            "sequence": checkpoint_sequence or sequence,
            "hash": checkpoint_hash or _digest(f"checkpoint:{checkpoint_sequence or sequence}"),
        },
    }


def _inspect(projection, memory_id: str, *, subject: str = ME_A) -> dict[str, object]:
    return {
        "schema": projection.INSPECT_SCHEMA,
        "target": {
            "instance_id": INSTANCE,
            "api_version": projection.API_VERSION,
            "schema_version": projection.SCHEMA_VERSION,
        },
        "source_instance": SOURCE,
        "subject_me_id": subject,
        "memory_id": memory_id,
        "projector": copy.deepcopy(PROJECTOR),
    }


def _verify(projection, *, subject: str = ME_A) -> dict[str, object]:
    return {
        "schema": projection.VERIFY_SCHEMA,
        "target": {
            "instance_id": INSTANCE,
            "api_version": projection.API_VERSION,
            "schema_version": projection.SCHEMA_VERSION,
        },
        "source_instance": SOURCE,
        "subject_me_id": subject,
        "projector": copy.deepcopy(PROJECTOR),
    }


def _rebuild_request(
    projection,
    entries: list[dict[str, object]],
    *,
    label: str,
    checkpoint_sequence: int,
    checkpoint_hash: str | None = None,
    subject: str = ME_A,
) -> dict[str, object]:
    return {
        "schema": projection.REBUILD_REQUEST_SCHEMA,
        "request_id": _event("rebuild:" + label),
        "idempotency_key": "rebuild:" + label,
        "target": {
            "instance_id": INSTANCE,
            "api_version": projection.API_VERSION,
            "schema_version": projection.SCHEMA_VERSION,
        },
        "source_instance": SOURCE,
        "subject_me_id": subject,
        "projector": copy.deepcopy(PROJECTOR),
        "source_checkpoint": {
            "sequence": checkpoint_sequence,
            "hash": checkpoint_hash or _digest(f"checkpoint:{checkpoint_sequence}"),
        },
        "entries": entries,
    }


def _entry(
    *, memory_label: str, head_label: str, sequence: int, text: str, subject: str = ME_A
) -> dict[str, object]:
    return {
        "memory_id": _event("memory:" + memory_label),
        "author_me_id": subject,
        "category": "personal-insight",
        "head": {
            "event_id": _event("head:" + head_label),
            "event_hash": _digest("head:" + head_label),
            "sequence": sequence,
        },
        "statement": _statement(text),
    }


def test_project_advance_retract_replay_retrieval_and_generic_guards(runtime):
    memoryctl, projection, api, _base = runtime
    memory_id = _event("memory:default")
    first = _request(projection, label="first")
    receipt = api.apply(first)
    assert receipt == api.apply(first)
    duplicate_route = copy.deepcopy(first)
    duplicate_route["request_id"] = _event("request:first-route-two")
    duplicate_route["idempotency_key"] = "projection:first-route-two"
    assert api.apply(duplicate_route) == receipt

    inspected = api.inspect(_inspect(projection, memory_id))
    assert inspected["projection"]["active"] is True
    assert inspected["projection"]["statement"]["text"] == "synthetic orchard memory"
    hits = memoryctl.search("orchard")
    assert len(hits) == 1
    assert hits[0]["origin"] == {
        "kind": "daimon-projection",
        "projection_id": receipt["current"]["projection_id"],
        "namespace_id": receipt["current"]["namespace_id"],
        "source_instance": SOURCE,
        "subject_me_id": ME_A,
        "author_me_id": ME_A,
        "memory_id": memory_id,
        "category": "personal-insight",
        "head_event_id": first["head"]["event_id"],
        "head_event_hash": first["head"]["event_hash"],
        "head_sequence": 1,
        "statement_hash": first["statement"]["sha256"],
        "statement_media_type": "text/plain",
        "classification": "protected",
        "source_checkpoint_sequence": 1,
        "source_checkpoint_hash": first["source_checkpoint"]["hash"],
        "projector_id": PROJECTOR["id"],
        "projector_version": PROJECTOR["version"],
        "active": True,
    }
    chapter_id = hits[0]["id"]
    assert memoryctl.expand(chapter_id)["origin"]["kind"] == "daimon-projection"

    advance = _request(
        projection,
        label="second",
        operation="advance",
        sequence=2,
        predecessor_label="first",
        text="synthetic river correction",
    )
    advanced = api.apply(advance)
    assert advanced["previous"]["head"]["sequence"] == 1
    assert memoryctl.search("orchard") == []
    assert memoryctl.search("river")[0]["origin"]["head_sequence"] == 2

    with pytest.raises(SystemExit, match="projection-managed"):
        memoryctl.update_chapter(chapter_id, content="forbidden")
    with pytest.raises(SystemExit, match="projection-managed"):
        memoryctl.delete_chapter(chapter_id)
    native = memoryctl.add_text("library", "native", "native survivor")
    with pytest.raises(SystemExit, match="projection-managed"):
        memoryctl.add_link(native, chapter_id, "related")
    with pytest.raises(SystemExit, match="versioned projection API"):
        memoryctl.add_text("daimon-projection", "forbidden", "forbidden")

    retract = _request(
        projection,
        label="third",
        operation="retract",
        sequence=3,
        predecessor_label="second",
        text=None,
    )
    retracted = api.apply(retract)
    assert retracted["current"]["active"] is False
    assert memoryctl.search("river") == []
    assert api.inspect(_inspect(projection, memory_id))["projection"]["active"] is False
    con = memoryctl.connect()
    assert con.execute("SELECT COUNT(*) FROM daimon_projection_history").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM chapters WHERE id=?", (native,)).fetchone()[0] == 1
    con.close()


def test_contract_substitution_conflicts_and_two_identity_isolation(runtime):
    _memoryctl, projection, api, _base = runtime
    request = _request(projection, label="base")
    cases = []
    wrong_author = copy.deepcopy(request)
    wrong_author["author_me_id"] = ME_B
    cases.append((wrong_author, "projection_author_not_subject"))
    wrong_target = copy.deepcopy(request)
    wrong_target["target"]["instance_id"] = "hmk:other-instance"
    cases.append((wrong_target, "projection_target_instance_mismatch"))
    wrong_category = copy.deepcopy(request)
    wrong_category["category"] = "tribal-knowledge"
    cases.append((wrong_category, "unsupported_projection_category"))
    wrong_hash = copy.deepcopy(request)
    wrong_hash["statement"]["sha256"] = "0" * 64
    cases.append((wrong_hash, "projection_statement_hash_mismatch"))
    extra = copy.deepcopy(request)
    extra["database_path"] = "/private/library.db"
    cases.append((extra, "invalid_projection_request"))
    for value, code in cases:
        with pytest.raises(projection.ProjectionError, match=code):
            api.apply(value)

    base_receipt = api.apply(request)
    changed_idempotency = copy.deepcopy(request)
    changed_idempotency["statement"] = _statement("different bytes")
    with pytest.raises(projection.ProjectionError, match="projection_idempotency_conflict"):
        api.apply(changed_idempotency)
    changed_head = copy.deepcopy(changed_idempotency)
    changed_head["request_id"] = _event("request:changed-head")
    changed_head["idempotency_key"] = "projection:changed-head"
    with pytest.raises(projection.ProjectionError, match="projection_head_content_conflict"):
        api.apply(changed_head)

    wrong_predecessor = _request(
        projection,
        label="wrong-predecessor",
        operation="advance",
        sequence=2,
        predecessor_label="someone-else",
    )
    with pytest.raises(projection.ProjectionError, match="projection_predecessor_mismatch"):
        api.apply(wrong_predecessor)
    skipped = _request(
        projection,
        label="skipped",
        operation="advance",
        sequence=3,
        predecessor_label="base",
    )
    with pytest.raises(projection.ProjectionError, match="projection_sequence_not_contiguous"):
        api.apply(skipped)
    fork = _request(
        projection,
        label="fork",
        memory_id=_event("memory:fork"),
        sequence=1,
        checkpoint_sequence=1,
        checkpoint_hash=_digest("different-checkpoint-one"),
    )
    with pytest.raises(projection.ProjectionError, match="projection_checkpoint_fork"):
        api.apply(fork)

    other = _request(
        projection,
        label="other-subject",
        subject=ME_B,
        text="synthetic orchard memory",
    )
    other_receipt = api.apply(other)
    assert (
        other_receipt["current"]["projection_id"]
        != base_receipt["current"]["projection_id"]
    )
    assert api.inspect(
        _inspect(projection, other["memory_id"], subject=ME_B)
    )["projection"]["author_me_id"] == ME_B


def test_transaction_faults_and_response_loss_are_exactly_once(runtime):
    memoryctl, projection, _api, _base = runtime
    request = _request(projection, label="fault")

    def before_commit(stage):
        if stage == "after_chapter_before_projection":
            raise RuntimeError("synthetic precommit fault")

    broken = projection.ProjectionAPI(INSTANCE, before_commit)
    with pytest.raises(RuntimeError, match="precommit"):
        broken.apply(request)
    con = memoryctl.connect()
    assert con.execute("SELECT COUNT(*) FROM daimon_projections").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM books WHERE source_kind='daimon-projection'").fetchone()[0] == 0
    con.close()

    committed_receipt = None

    def after_commit(stage):
        if stage == "after_commit_before_response":
            raise ConnectionError("synthetic response loss")

    lost = projection.ProjectionAPI(INSTANCE, after_commit)
    with pytest.raises(ConnectionError, match="response loss"):
        lost.apply(request)
    committed_receipt = projection.ProjectionAPI(INSTANCE).apply(request)
    assert committed_receipt["outcome"] == "applied"
    con = memoryctl.connect()
    assert con.execute("SELECT COUNT(*) FROM daimon_projections").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM daimon_projection_history").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM chapters_fts").fetchone()[0] == 1
    con.close()


def test_rebuild_is_atomic_deterministic_scoped_and_preserves_native(runtime):
    memoryctl, projection, api, _base = runtime
    native = memoryctl.add_text("library", "native", "native untouched row")
    old = _request(projection, label="old", text="old projection row")
    api.apply(old)
    checkpoint_hash = _digest("checkpoint:10")
    entries = [
        _entry(memory_label="a", head_label="rebuild-a", sequence=4, text="alpha rebuild"),
        _entry(memory_label="b", head_label="rebuild-b", sequence=7, text="beta rebuild"),
    ]
    rebuild_request = _rebuild_request(
        projection,
        entries,
        label="full",
        checkpoint_sequence=10,
        checkpoint_hash=checkpoint_hash,
    )
    plan = api.rebuild_plan(rebuild_request)
    assert plan == api.rebuild_plan(rebuild_request)
    original = api.verify(_verify(projection))

    def fail_after_clear(stage):
        if stage == "after_clear_before_rebuild":
            raise RuntimeError("synthetic rebuild fault")

    broken = projection.ProjectionAPI(INSTANCE, fail_after_clear)
    with pytest.raises(RuntimeError, match="rebuild fault"):
        broken.rebuild_apply({"schema": projection.REBUILD_APPLY_SCHEMA, "plan": plan})
    assert api.verify(_verify(projection)) == original

    receipt = api.rebuild_apply(
        {"schema": projection.REBUILD_APPLY_SCHEMA, "plan": plan}
    )
    assert receipt == api.rebuild_apply(
        {"schema": projection.REBUILD_APPLY_SCHEMA, "plan": plan}
    )
    verified = api.verify(_verify(projection))
    assert verified["manifest_hash"] == plan["manifest_hash"]
    assert verified["logical_hash"] == plan["manifest_hash"]
    assert len(verified["projections"]) == 2
    con = memoryctl.connect()
    assert con.execute("SELECT COUNT(*) FROM chapters WHERE id=?", (native,)).fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM books WHERE source_kind='daimon-projection'").fetchone()[0] == 2
    con.close()

    empty_request = _rebuild_request(
        projection,
        [],
        label="empty",
        checkpoint_sequence=10,
        checkpoint_hash=checkpoint_hash,
    )
    empty_plan = api.rebuild_plan(empty_request)
    api.rebuild_apply({"schema": projection.REBUILD_APPLY_SCHEMA, "plan": empty_plan})
    assert api.verify(_verify(projection))["projections"] == []
    restore_request = _rebuild_request(
        projection,
        entries,
        label="restore",
        checkpoint_sequence=10,
        checkpoint_hash=checkpoint_hash,
    )
    restore_plan = api.rebuild_plan(restore_request)
    api.rebuild_apply({"schema": projection.REBUILD_APPLY_SCHEMA, "plan": restore_plan})
    restored = api.verify(_verify(projection))
    assert restored["logical_hash"] == verified["logical_hash"]


def test_rebuild_drift_regression_collision_and_cross_namespace(runtime):
    memoryctl, projection, api, _base = runtime
    first = _request(projection, label="one")
    api.apply(first)
    entries = [
        _entry(memory_label="new", head_label="new", sequence=2, text="new row")
    ]
    plan = api.rebuild_plan(
        _rebuild_request(projection, entries, label="stale-plan", checkpoint_sequence=2)
    )
    advance = _request(
        projection,
        label="two",
        operation="advance",
        sequence=2,
        predecessor_label="one",
    )
    api.apply(advance)
    with pytest.raises(projection.ProjectionError, match="rebuild_target_drift"):
        api.rebuild_apply({"schema": projection.REBUILD_APPLY_SCHEMA, "plan": plan})
    with pytest.raises(projection.ProjectionError, match="projection_checkpoint_regression"):
        api.rebuild_plan(
            _rebuild_request(
                projection,
                [
                    _entry(
                        memory_label="regression",
                        head_label="regression",
                        sequence=1,
                        text="regressed row",
                    )
                ],
                label="regression",
                checkpoint_sequence=1,
            )
        )

    other = _request(projection, label="other", subject=ME_B)
    api.apply(other)
    before_other = api.verify(_verify(projection, subject=ME_B))
    current = api.verify(_verify(projection))
    same_plan = api.rebuild_plan(
        _rebuild_request(
            projection,
            entries,
            label="scoped",
            checkpoint_sequence=current["source_checkpoint"]["sequence"],
            checkpoint_hash=current["source_checkpoint"]["hash"],
        )
    )
    api.rebuild_apply({"schema": projection.REBUILD_APPLY_SCHEMA, "plan": same_plan})
    assert api.verify(_verify(projection, subject=ME_B)) == before_other

    collision_entry = _entry(
        memory_label="collision", head_label="collision", sequence=3, text="collision"
    )
    identity = {
        "source_instance": SOURCE,
        "subject_me_id": ME_A,
        "projector_id": PROJECTOR["id"],
        "projector_version": PROJECTOR["version"],
    }
    ns_id = projection.namespace_id(identity)
    proj_id = projection.projection_id(ns_id, collision_entry["memory_id"])
    slug = "daimon-" + proj_id.rsplit(":", 1)[-1].lower()
    con = memoryctl.connect()
    shelf = con.execute("SELECT id FROM shelves WHERE name='daimon-projection'").fetchone()[0]
    con.execute(
        "INSERT INTO books(shelf_id,slug,title,source_path,source_kind,created_at,updated_at) VALUES(?,?,?,NULL,'text',1,1)",
        (shelf, slug, "collision"),
    )
    con.commit()
    con.close()
    with pytest.raises(projection.ProjectionError, match="projection_destination_collision"):
        api.rebuild_plan(
            _rebuild_request(
                projection,
                [collision_entry],
                label="collision",
                checkpoint_sequence=3,
            )
        )


def test_cli_canonical_boundary_and_concurrent_process_replay(runtime):
    _memoryctl, projection, _api, base = runtime
    request = _request(projection, label="cli")
    environment = os.environ.copy()
    environment["HMK_AGENT_MEMORY_BASE"] = str(base)
    environment["HMK_INSTANCE_ID"] = INSTANCE
    command = [sys.executable, str(SCRIPTS / "daimon_projection.py"), "apply"]
    raw = projection.canonical_bytes(request)

    def invoke(payload: bytes):
        return subprocess.run(
            command,
            input=payload,
            capture_output=True,
            env=environment,
            timeout=20,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, [raw, raw]))
    assert [result.returncode for result in results] == [0, 0]
    receipts = [json.loads(result.stdout) for result in results]
    assert receipts[0] == receipts[1]
    assert b"library.db" not in results[0].stdout

    pretty = json.dumps(request, indent=2).encode()
    refused = invoke(pretty)
    assert refused.returncode == 1
    assert b"noncanonical_json" in refused.stderr
    changed = copy.deepcopy(request)
    changed["statement"] = _statement("conflicting concurrent bytes")
    conflict = invoke(projection.canonical_bytes(changed))
    assert conflict.returncode == 1
    assert b"projection_idempotency_conflict" in conflict.stderr


def test_schema_reopen_backup_integrity_and_content_drift_detection(runtime, tmp_path):
    memoryctl, projection, api, base = runtime
    request = _request(projection, label="backup")
    api.apply(request)
    backup_path = tmp_path / "backup.db"
    source = sqlite3.connect(base / "library.db")
    destination = sqlite3.connect(backup_path)
    source.backup(destination)
    destination.close()
    source.close()
    backup = sqlite3.connect(backup_path)
    assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert backup.execute("SELECT COUNT(*) FROM daimon_projections").fetchone()[0] == 1
    backup.close()

    con = memoryctl.connect()
    con.execute(
        "UPDATE chapters SET raw='drifted bytes' WHERE id=(SELECT chapter_id FROM daimon_projections LIMIT 1)"
    )
    con.commit()
    con.close()
    with pytest.raises(projection.ProjectionError, match="projection_content_drift"):
        api.verify(_verify(projection))


def test_closed_canonical_boundary_rejects_unknown_versions_and_malformed_input(runtime):
    _memoryctl, projection, api, _base = runtime
    request = _request(projection, label="boundary")
    mutations = []
    unknown_operation = copy.deepcopy(request)
    unknown_operation["operation"] = "replace"
    mutations.append((unknown_operation, "unsupported_projection_operation"))
    unknown_api = copy.deepcopy(request)
    unknown_api["target"]["api_version"] = "2.0.0"
    mutations.append((unknown_api, "unsupported_projection_target"))
    unknown_projector = copy.deepcopy(request)
    unknown_projector["projector"]["version"] = "next"
    mutations.append((unknown_projector, "invalid_projector"))
    oversized = copy.deepcopy(request)
    oversized["statement"]["byte_length"] = projection.MAX_STATEMENT_BYTES + 1
    mutations.append((oversized, "projection_statement_length_mismatch"))
    for value, code in mutations:
        with pytest.raises(projection.ProjectionError, match=code):
            api.apply(value)

    with pytest.raises(projection.ProjectionError, match="duplicate_json_key"):
        projection.load_canonical_document(b'{"a":1,"a":1}')
    with pytest.raises(projection.ProjectionError, match="noncanonical_json"):
        projection.load_canonical_document(b'{"a": 1}')
    with pytest.raises(projection.ProjectionError, match="non_nfc_string"):
        projection.canonical_bytes({"value": "e\u0301"})
    with pytest.raises(projection.ProjectionError, match="floating_point_refused"):
        projection.canonical_bytes({"value": 1.5})
    with pytest.raises(projection.ProjectionError, match="integer_out_of_range"):
        projection.canonical_bytes({"value": projection.MAX_SAFE_INTEGER + 1})
    with pytest.raises(projection.ProjectionError, match="document_size_refused"):
        projection.load_canonical_document(b"x" * (projection.MAX_DOCUMENT_BYTES + 1))


def test_origin_substitution_and_collective_publication_are_refused(runtime):
    memoryctl, projection, api, _base = runtime
    native = memoryctl.add_text(
        "library",
        "Pretender",
        "Native content with a misleading tag.",
        tags=["daimon-projection", "collective:publish"],
    )
    native_hit = memoryctl.search("Pretender")[0]
    assert native_hit["id"] == native
    assert native_hit["origin"] == {"kind": "text"}

    api.apply(_request(projection, label="publication", text="Projected publish bait"))
    projected = memoryctl.search("Projected publish bait")[0]
    assert projected["origin"]["kind"] == "daimon-projection"

    import publish_collective as publication

    chapter = {
        "id": projected["id"],
        "shelf": projected["shelf"],
        "source_kind": "daimon-projection",
        "source_path": None,
        "tags": [publication.PUBLISH_TAG],
    }
    collection = {"allowed_shelves": ["daimon-projection"]}
    with pytest.raises(publication.PublicationError, match="not HMK-native"):
        publication.validate_publishable_chapter(chapter, collection)


def test_projection_schema_migration_is_atomic_recoverable_and_version_guarded(runtime, tmp_path):
    memoryctl, projection, api, _base = runtime
    migration_db = tmp_path / "migration.db"
    con = sqlite3.connect(migration_db)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE shelves(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT);
        CREATE TABLE chapters(id INTEGER PRIMARY KEY);
        """
    )
    with pytest.raises(RuntimeError, match="migration fault"):
        memoryctl.migrate_daimon_projection(
            con,
            lambda index: (_ for _ in ()).throw(RuntimeError("migration fault"))
            if index == 2
            else None,
        )
    names = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'daimon_%'"
        )
    }
    assert names == set()
    memoryctl.migrate_daimon_projection(con)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()

    api.apply(_request(projection, label="preserved-before-future-schema"))
    db = memoryctl.connect()
    db.execute("UPDATE daimon_projection_schema SET schema_version=2 WHERE singleton=1")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="unsupported Daimon projection schema version"):
        api.apply(_request(projection, label="future-schema"))
    db = memoryctl.connect()
    assert db.execute("SELECT COUNT(*) FROM daimon_projections").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM daimon_projection_history").fetchone()[0] == 1
    db.close()


def test_public_vectors_reproduce_and_execute_byte_for_byte(runtime, tmp_path):
    _memoryctl, projection, _api, _base = runtime
    api = projection.ProjectionAPI("hmk:vector-instance")
    vector_root = ROOT / "vectors" / "daimon-projection" / "v1"
    index = json.loads((vector_root / "index.json").read_text())
    for name, expected in index["files"].items():
        assert hashlib.sha256((vector_root / name).read_bytes()).hexdigest() == expected

    project = json.loads((vector_root / "project.request.json").read_text())
    assert api.apply(project) == json.loads((vector_root / "project.receipt.json").read_text())
    advance = json.loads((vector_root / "advance.request.json").read_text())
    assert api.apply(advance) == json.loads((vector_root / "advance.receipt.json").read_text())
    conflicting = json.loads(
        (vector_root / "negative.same-head-different-bytes.request.json").read_text()
    )
    with pytest.raises(projection.ProjectionError, match="projection_head_content_conflict"):
        api.apply(conflicting)
    retract = json.loads((vector_root / "retract.request.json").read_text())
    assert api.apply(retract) == json.loads((vector_root / "retract.receipt.json").read_text())
    rebuild_request = json.loads((vector_root / "rebuild.request.json").read_text())
    plan = api.rebuild_plan(rebuild_request)
    assert plan == json.loads((vector_root / "rebuild.plan.json").read_text())
    rebuild_apply = json.loads((vector_root / "rebuild.apply.json").read_text())
    assert api.rebuild_apply(rebuild_apply) == json.loads(
        (vector_root / "rebuild.receipt.json").read_text()
    )
    assert api.inspect(json.loads((vector_root / "inspect.query.json").read_text())) == json.loads(
        (vector_root / "inspect.result.json").read_text()
    )
    assert api.verify(json.loads((vector_root / "verify.query.json").read_text())) == json.loads(
        (vector_root / "verify.result.json").read_text()
    )

    generated = []
    for seed, timezone in [("1", "UTC"), ("937", "America/Argentina/Cordoba")]:
        destination = tmp_path / f"generated-{seed}"
        environment = os.environ.copy()
        environment.update({"PYTHONHASHSEED": seed, "TZ": timezone, "LC_ALL": "C.UTF-8"})
        subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "generate_daimon_projection_vectors.py"),
                "--output",
                str(destination),
            ],
            check=True,
            capture_output=True,
            env=environment,
            timeout=30,
        )
        generated.append({path.name: path.read_bytes() for path in destination.iterdir()})
    assert generated[0] == generated[1]
    assert generated[0] == {path.name: path.read_bytes() for path in vector_root.iterdir()}


def test_concurrent_conflicting_writers_have_one_winner(runtime):
    _memoryctl, projection, _api, base = runtime
    first = _request(
        projection,
        label="concurrent-conflict",
        memory_id=_event("memory:concurrent-conflict"),
    )
    second = copy.deepcopy(first)
    second["statement"] = _statement("other concurrent bytes")
    environment = os.environ.copy()
    environment["HMK_AGENT_MEMORY_BASE"] = str(base)
    environment["HMK_INSTANCE_ID"] = INSTANCE
    command = [sys.executable, str(SCRIPTS / "daimon_projection.py"), "apply"]

    def invoke(value):
        return subprocess.run(
            command,
            input=projection.canonical_bytes(value),
            capture_output=True,
            env=environment,
            timeout=20,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, [first, second]))
    assert sorted(result.returncode for result in results) == [0, 1]
    failure = next(result for result in results if result.returncode == 1)
    assert b"projection_idempotency_conflict" in failure.stderr


def test_schema_documents_are_closed_and_resolve_local_references():
    schema_root = ROOT / "schemas" / "daimon-projection" / "v1"
    documents = {path.name: json.loads(path.read_text()) for path in schema_root.glob("*.json")}
    expected = {
        "common.schema.json", "request.schema.json", "receipt.schema.json",
        "query.schema.json", "result.schema.json", "rebuild-request.schema.json",
        "rebuild-plan.schema.json", "rebuild-apply.schema.json",
        "rebuild-receipt.schema.json", "diagnostic.schema.json",
    }
    assert set(documents) == expected

    def walk(value):
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#"):
                assert reference.split("#", 1)[0] in documents
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for name, document in documents.items():
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        if name != "common.schema.json":
            assert document.get("additionalProperties") is False or "oneOf" in document
        walk(document)
