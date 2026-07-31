from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import publish_collective as publication


def create_database(
    path: Path,
    *,
    raw: str = "Shareable operational lesson.",
    tags: list[str] | None = None,
    shelf: str = "library",
    source_kind: str = "text",
    source_path: str | None = None,
) -> None:
    tags = [publication.PUBLISH_TAG] if tags is None else tags
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE shelves (
              id INTEGER PRIMARY KEY,
              name TEXT UNIQUE NOT NULL
            );
            CREATE TABLE books (
              id INTEGER PRIMARY KEY,
              shelf_id INTEGER NOT NULL,
              title TEXT NOT NULL,
              source_path TEXT,
              source_kind TEXT NOT NULL
            );
            CREATE TABLE chapters (
              id INTEGER PRIMARY KEY,
              book_id INTEGER NOT NULL,
              title TEXT,
              raw TEXT NOT NULL,
              tags_json TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )
        connection.execute("INSERT INTO shelves VALUES(1, ?)", (shelf,))
        connection.execute(
            "INSERT INTO books VALUES(1, 1, 'Book', ?, ?)",
            (source_path, source_kind),
        )
        connection.execute(
            "INSERT INTO chapters VALUES(1, 1, 'Lesson', ?, ?, 10, 20)",
            (raw, json.dumps(tags)),
        )


def policy_data(action: str = "publish") -> dict:
    entry = {
        "action": "publish",
        "chapter_id": 1,
        "collection": "shared-knowledge",
        "classification": "tribe-shared",
        "consent": "explicit",
        "license": "CC-BY-SA-4.0",
        "author_principal": "author@example",
    }
    if action == "revoke":
        entry = {
            "action": "revoke",
            "chapter_id": 1,
            "collection": "shared-knowledge",
            "author_principal": "author@example",
            "revocation_reason": "source owner withdrew consent",
        }
    return {
        "schema": publication.POLICY_SCHEMA,
        "publisher_principal": "publisher@example",
        "source_instance": "agent-example",
        "redaction_policy": "reject-secrets-no-automatic-redaction",
        "collections": [
            {
                "name": "shared-knowledge",
                "path_prefix": "shared-knowledge/hmk",
                "allowed_shelves": ["library", "evidence"],
                "allowed_classifications": ["public", "tribe-shared"],
                "allowed_licenses": ["CC-BY-SA-4.0"],
            }
        ],
        "entries": [entry],
    }


def write_policy(path: Path, action: str = "publish") -> None:
    path.write_text(json.dumps(policy_data(action)), encoding="utf-8")


def approved(
    root: Path, plan: dict, reviewer: str = "reviewer@example"
) -> dict:
    value = {
        "schema": publication.APPROVAL_SCHEMA,
        "plan_sha256": publication.plan_sha256(plan),
        "decision": "approved",
        "reviewer_principal": reviewer,
        "approved_at": "2026-07-31T00:00:00Z",
    }
    path = root / "approval.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return publication.load_approval(path, plan)


@pytest.fixture
def fixture(tmp_path):
    database = tmp_path / "library.db"
    policy = tmp_path / "policy.json"
    create_database(database)
    write_policy(policy)
    return database, policy


def test_plan_is_dry_and_contains_reviewable_provenance(fixture, tmp_path):
    database, policy = fixture

    plan = publication.build_plan(
        database, policy, generated_at="2026-07-31T00:00:00Z"
    )

    assert len(plan["actions"]) == 1
    action = plan["actions"][0]
    assert action["source_uri"] == "hmk://agent-example/chapters/1"
    assert action["metadata"]["classification"] == "tribe-shared"
    assert action["metadata"]["consent"] == "explicit"
    assert action["metadata"]["license"] == "CC-BY-SA-4.0"
    assert action["metadata"]["derivation_chain"][0]["sha256"]
    assert not (tmp_path / "published-hmk").exists()


def test_private_classification_cannot_be_allowlisted(tmp_path):
    database = tmp_path / "library.db"
    policy = tmp_path / "policy.json"
    create_database(database)
    value = policy_data()
    value["collections"][0]["allowed_classifications"].append("private")
    policy.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(publication.PublicationError, match="private"):
        publication.build_plan(database, policy)


def test_non_native_wiki_index_is_rejected(tmp_path):
    database = tmp_path / "library.db"
    policy = tmp_path / "policy.json"
    create_database(
        database,
        source_kind="file",
        source_path="/authoritative/wiki/note.md",
    )
    write_policy(policy)

    with pytest.raises(publication.PublicationError, match="not HMK-native"):
        publication.build_plan(database, policy)


def test_dual_opt_in_tag_is_required(tmp_path):
    database = tmp_path / "library.db"
    policy = tmp_path / "policy.json"
    create_database(database, tags=["ordinary"])
    write_policy(policy)

    with pytest.raises(publication.PublicationError, match="dual-opt-in"):
        publication.build_plan(database, policy)


def test_secret_content_fails_closed(tmp_path):
    database = tmp_path / "library.db"
    policy = tmp_path / "policy.json"
    create_database(
        database,
        raw="Do not share: OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
    )
    write_policy(policy)

    with pytest.raises(publication.PublicationError, match="redaction policy"):
        publication.build_plan(database, policy)


def test_short_secret_content_also_fails_closed(tmp_path):
    database = tmp_path / "library.db"
    policy = tmp_path / "policy.json"
    create_database(database, raw="sk-abcdefghijklmnop")
    write_policy(policy)

    with pytest.raises(publication.PublicationError, match="redaction policy"):
        publication.build_plan(database, policy)


def test_approval_is_exact_and_independent(fixture, tmp_path):
    database, policy = fixture
    plan = publication.build_plan(database, policy)

    with pytest.raises(publication.PublicationError, match="independent"):
        approved(tmp_path, plan, reviewer="publisher@example")

    wrong = dict(plan)
    wrong["generated_at"] = "changed"
    approval = {
        "schema": publication.APPROVAL_SCHEMA,
        "plan_sha256": publication.plan_sha256(plan),
        "decision": "approved",
        "reviewer_principal": "reviewer@example",
        "approved_at": "2026-07-31T00:00:00Z",
    }
    path = tmp_path / "wrong-approval.json"
    path.write_text(json.dumps(approval), encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="exact"):
        publication.load_approval(path, wrong)


def test_publish_is_idempotent_and_writes_receipt(fixture, tmp_path):
    database, policy = fixture
    destination = tmp_path / "corpus"
    state = tmp_path / "state"
    plan = publication.build_plan(database, policy)
    approval = approved(tmp_path, plan)

    first = publication.apply_plan(
        plan,
        approval,
        database=database,
        policy_path=policy,
        destination=destination,
        state_dir=state,
    )
    second = publication.apply_plan(
        plan,
        approval,
        database=database,
        policy_path=policy,
        destination=destination,
        state_dir=state,
    )

    target = destination / plan["actions"][0]["target"]
    assert target.is_file()
    assert first["results"][0]["result"] == "published"
    assert second["results"][0]["result"] == "unchanged"
    assert (state / "receipts" / f"{approval['plan_sha256']}.json").is_file()


def test_target_drift_is_a_hard_conflict(fixture, tmp_path):
    database, policy = fixture
    destination = tmp_path / "corpus"
    state = tmp_path / "state"
    plan = publication.build_plan(database, policy)
    approval = approved(tmp_path, plan)
    publication.apply_plan(
        plan,
        approval,
        database=database,
        policy_path=policy,
        destination=destination,
        state_dir=state,
    )
    target = destination / plan["actions"][0]["target"]
    target.write_text("unreviewed drift", encoding="utf-8")

    with pytest.raises(publication.PublicationError, match="drifted"):
        publication.apply_plan(
            plan,
            approval,
            database=database,
            policy_path=policy,
            destination=destination,
            state_dir=state,
        )


def test_state_failure_rolls_back_published_artifact(
    fixture, tmp_path, monkeypatch
):
    database, policy = fixture
    destination = tmp_path / "corpus"
    state = tmp_path / "state"
    plan = publication.build_plan(database, policy)
    approval = approved(tmp_path, plan)

    def fail_state(*_args, **_kwargs):
        raise OSError("simulated state failure")

    monkeypatch.setattr(publication, "write_json_atomic", fail_state)
    with pytest.raises(OSError, match="simulated state failure"):
        publication.apply_plan(
            plan,
            approval,
            database=database,
            policy_path=policy,
            destination=destination,
            state_dir=state,
        )

    assert not (destination / plan["actions"][0]["target"]).exists()


def test_database_change_after_plan_is_rejected(fixture, tmp_path):
    database, policy = fixture
    plan = publication.build_plan(database, policy)
    approval = approved(tmp_path, plan)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE chapters SET raw='changed' WHERE id=1")

    with pytest.raises(publication.PublicationError, match="changed since planning"):
        publication.apply_plan(
            plan,
            approval,
            database=database,
            policy_path=policy,
            destination=tmp_path / "corpus",
            state_dir=tmp_path / "state",
        )


def test_revocation_removes_artifact_and_records_tombstone(fixture, tmp_path):
    database, policy = fixture
    destination = tmp_path / "corpus"
    state_dir = tmp_path / "state"
    plan = publication.build_plan(database, policy)
    approval = approved(tmp_path, plan)
    publication.apply_plan(
        plan,
        approval,
        database=database,
        policy_path=policy,
        destination=destination,
        state_dir=state_dir,
    )
    target = destination / plan["actions"][0]["target"]

    write_policy(policy, action="revoke")
    revoke_plan = publication.build_plan(database, policy)
    revoke_approval = approved(tmp_path, revoke_plan)
    receipt = publication.apply_plan(
        revoke_plan,
        revoke_approval,
        database=database,
        policy_path=policy,
        destination=destination,
        state_dir=state_dir,
    )

    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    uri = "hmk://agent-example/chapters/1"
    assert not target.exists()
    assert uri in state["tombstones"]
    assert uri not in state["published"]
    assert receipt["results"][0]["result"] == "revoked"
