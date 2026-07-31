#!/usr/bin/env python3
"""Review-gated HMK publication artifacts for collective-memory.

The default command only plans. Publishing requires an independently supplied
approval bound to the exact plan hash. The HMK database is always opened
read-only; this adapter never calls a network service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from corpus_policy import scan_content_for_secrets


POLICY_SCHEMA = "hmk-collective-publication-policy/v1"
PLAN_SCHEMA = "hmk-collective-publication-plan/v1"
APPROVAL_SCHEMA = "hmk-collective-publication-approval/v1"
STATE_SCHEMA = "hmk-collective-publication-state/v1"
RECEIPT_SCHEMA = "hmk-collective-publication-receipt/v1"
ARTIFACT_SCHEMA = "hmk-collective-publication-artifact/v1"
PUBLISH_TAG = "collective:publish"
SHAREABLE_CLASSIFICATIONS = {"public", "tribe-shared"}

_POLICY_KEYS = {
    "schema",
    "publisher_principal",
    "source_instance",
    "redaction_policy",
    "collections",
    "entries",
}
_COLLECTION_KEYS = {
    "name",
    "path_prefix",
    "allowed_shelves",
    "allowed_classifications",
    "allowed_licenses",
}
_PUBLISH_KEYS = {
    "action",
    "chapter_id",
    "collection",
    "classification",
    "consent",
    "license",
    "author_principal",
}
_REVOKE_KEYS = {
    "action",
    "chapter_id",
    "collection",
    "author_principal",
    "revocation_reason",
}
_APPROVAL_KEYS = {
    "schema",
    "plan_sha256",
    "decision",
    "reviewer_principal",
    "approved_at",
}
_PLAN_KEYS = {
    "schema",
    "generated_at",
    "policy_sha256",
    "publisher_principal",
    "source_instance",
    "actions",
}
_PLAN_PUBLISH_KEYS = {
    "action",
    "chapter_id",
    "source_uri",
    "artifact_id",
    "target",
    "content_sha256",
    "artifact_sha256",
    "artifact_markdown",
    "metadata",
}
_PLAN_REVOKE_KEYS = {
    "action",
    "source_uri",
    "artifact_id",
    "target",
    "collection",
    "author_principal",
    "revocation_reason",
}


class PublicationError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def utc_iso(timestamp: int | float | None = None) -> str:
    instant = (
        datetime.now(timezone.utc)
        if timestamp is None
        else datetime.fromtimestamp(timestamp, timezone.utc)
    )
    return instant.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"cannot read JSON {path}: {exc}") from exc


def write_json_atomic(path: Path, value: Any, mode: int = 0o600) -> None:
    write_bytes_atomic(path, canonical_json(value), mode=mode)


def write_bytes_atomic(path: Path, value: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_relative_path(raw: str, label: str) -> str:
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise PublicationError(f"{label} must be a safe relative path")
    return path.as_posix()


def _closed_mapping(
    value: Any, keys: set[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise PublicationError(f"{label} has unknown or missing fields")
    return value


def load_policy(path: Path) -> dict[str, Any]:
    raw = _closed_mapping(read_json(path), _POLICY_KEYS, "publication policy")
    if raw["schema"] != POLICY_SCHEMA:
        raise PublicationError(f"publication policy must use {POLICY_SCHEMA}")
    for field in ("publisher_principal", "source_instance"):
        if not isinstance(raw[field], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.@-]{1,127}", raw[field]
        ):
            raise PublicationError(f"{field} is not a safe principal/instance")
    if raw["redaction_policy"] != "reject-secrets-no-automatic-redaction":
        raise PublicationError("only fail-closed reject-only redaction is supported")
    if not isinstance(raw["collections"], list) or not raw["collections"]:
        raise PublicationError("at least one allowlisted collection is required")
    collections: dict[str, dict[str, Any]] = {}
    for item in raw["collections"]:
        entry = dict(_closed_mapping(item, _COLLECTION_KEYS, "collection"))
        name = entry["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", name):
            raise PublicationError("collection name must be a lowercase slug")
        if name in collections:
            raise PublicationError("collection names must be unique")
        entry["path_prefix"] = safe_relative_path(
            entry["path_prefix"], "collection path_prefix"
        )
        for field in (
            "allowed_shelves",
            "allowed_classifications",
            "allowed_licenses",
        ):
            values = entry[field]
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise PublicationError(f"collection {field} must be unique strings")
        if not set(entry["allowed_classifications"]) <= SHAREABLE_CLASSIFICATIONS:
            raise PublicationError("private classifications cannot be allowlisted")
        collections[name] = entry
    if not isinstance(raw["entries"], list):
        raise PublicationError("policy entries must be a list")
    seen: set[tuple[int, str]] = set()
    entries = []
    for item in raw["entries"]:
        if not isinstance(item, Mapping):
            raise PublicationError("publication entry must be an object")
        action = item.get("action")
        keys = _PUBLISH_KEYS if action == "publish" else _REVOKE_KEYS
        entry = dict(_closed_mapping(item, keys, f"{action or 'unknown'} entry"))
        if action not in {"publish", "revoke"}:
            raise PublicationError("entry action must be publish or revoke")
        if not isinstance(entry["chapter_id"], int) or entry["chapter_id"] <= 0:
            raise PublicationError("chapter_id must be a positive integer")
        collection = collections.get(entry["collection"])
        if collection is None:
            raise PublicationError("entry references a non-allowlisted collection")
        identity = (entry["chapter_id"], entry["collection"])
        if identity in seen:
            raise PublicationError("chapter/collection entries must be unique")
        seen.add(identity)
        if not isinstance(entry["author_principal"], str) or not entry[
            "author_principal"
        ]:
            raise PublicationError("author_principal is required")
        if action == "publish":
            if entry["consent"] != "explicit":
                raise PublicationError("publication consent must be explicit")
            if entry["classification"] not in collection["allowed_classifications"]:
                raise PublicationError("classification is not allowlisted")
            if entry["license"] not in collection["allowed_licenses"]:
                raise PublicationError("license is not allowlisted")
        elif (
            not isinstance(entry["revocation_reason"], str)
            or not entry["revocation_reason"].strip()
        ):
            raise PublicationError("revocation_reason is required")
        entries.append(entry)
    result = dict(raw)
    result["collections"] = list(collections.values())
    result["entries"] = entries
    return result


def _connect_read_only(database: Path) -> sqlite3.Connection:
    resolved = database.expanduser().resolve()
    if not resolved.is_file():
        raise PublicationError(f"HMK database does not exist: {resolved}")
    connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def read_chapter(database: Path, chapter_id: int) -> dict[str, Any]:
    with _connect_read_only(database) as connection:
        try:
            row = connection.execute(
                """
                SELECT c.id, c.title, c.raw, c.tags_json, c.created_at, c.updated_at,
                       s.name AS shelf, b.source_path, b.source_kind
                FROM chapters c
                JOIN books b ON b.id = c.book_id
                JOIN shelves s ON s.id = b.shelf_id
                WHERE c.id=?
                """,
                (chapter_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise PublicationError(f"cannot read HMK chapter schema: {exc}") from exc
    if row is None:
        raise PublicationError(f"HMK chapter does not exist: {chapter_id}")
    result = dict(row)
    try:
        tags = json.loads(result["tags_json"] or "[]")
    except json.JSONDecodeError as exc:
        raise PublicationError(f"chapter {chapter_id} has invalid tags JSON") from exc
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise PublicationError(f"chapter {chapter_id} tags must be strings")
    result["tags"] = tags
    return result


def source_uri(policy: Mapping[str, Any], chapter_id: int) -> str:
    return f"hmk://{policy['source_instance']}/chapters/{chapter_id}"


def artifact_id(uri: str) -> str:
    return sha256_text(uri)[:24]


def _frontmatter(metadata: Mapping[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(
            f"{key}: "
            + json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    lines.append("---")
    return "\n".join(lines)


def validate_publishable_chapter(
    chapter: Mapping[str, Any],
    collection: Mapping[str, Any],
) -> None:
    chapter_id = chapter["id"]
    if chapter["shelf"] not in collection["allowed_shelves"]:
        raise PublicationError(f"chapter {chapter_id} shelf is not allowlisted")
    if chapter["source_kind"] != "text" or chapter["source_path"]:
        raise PublicationError(
            f"chapter {chapter_id} is not HMK-native; publish its authoritative "
            "filesystem source instead"
        )
    if PUBLISH_TAG not in chapter["tags"]:
        raise PublicationError(
            f"chapter {chapter_id} lacks required dual-opt-in tag {PUBLISH_TAG}"
        )
    secret = scan_content_for_secrets(chapter["raw"], minimum_bytes=0)
    if secret:
        raise PublicationError(
            f"chapter {chapter_id} rejected by redaction policy: {secret}"
        )


def render_artifact(
    policy: Mapping[str, Any],
    entry: Mapping[str, Any],
    chapter: Mapping[str, Any],
    generated_at: str,
) -> tuple[str, dict[str, Any]]:
    uri = source_uri(policy, chapter["id"])
    content_hash = sha256_text(chapter["raw"])
    metadata = {
        "collective_publication_schema": ARTIFACT_SCHEMA,
        "source_uri": uri,
        "source_authority": "hmk",
        "author_principal": entry["author_principal"],
        "publisher_principal": policy["publisher_principal"],
        "source_created_at": utc_iso(chapter["created_at"]),
        "source_updated_at": utc_iso(chapter["updated_at"]),
        "published_plan_at": generated_at,
        "content_sha256": content_hash,
        "classification": entry["classification"],
        "consent": entry["consent"],
        "license": entry["license"],
        "collection": entry["collection"],
        "derivation_chain": [{"uri": uri, "sha256": content_hash}],
    }
    title = (chapter["title"] or f"HMK chapter {chapter['id']}").strip()
    markdown = f"{_frontmatter(metadata)}\n\n# {title}\n\n{chapter['raw'].rstrip()}\n"
    return markdown, metadata


def build_plan(
    database: Path,
    policy_path: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    policy = load_policy(policy_path)
    collections = {item["name"]: item for item in policy["collections"]}
    timestamp = generated_at or utc_iso()
    actions = []
    for entry in policy["entries"]:
        uri = source_uri(policy, entry["chapter_id"])
        aid = artifact_id(uri)
        prefix = collections[entry["collection"]]["path_prefix"]
        target = safe_relative_path(
            f"{prefix}/{aid}.md", "publication artifact target"
        )
        if entry["action"] == "revoke":
            actions.append(
                {
                    "action": "revoke",
                    "source_uri": uri,
                    "artifact_id": aid,
                    "target": target,
                    "collection": entry["collection"],
                    "author_principal": entry["author_principal"],
                    "revocation_reason": entry["revocation_reason"],
                }
            )
            continue
        chapter = read_chapter(database, entry["chapter_id"])
        validate_publishable_chapter(chapter, collections[entry["collection"]])
        markdown, metadata = render_artifact(policy, entry, chapter, timestamp)
        actions.append(
            {
                "action": "publish",
                "chapter_id": entry["chapter_id"],
                "source_uri": uri,
                "artifact_id": aid,
                "target": target,
                "content_sha256": metadata["content_sha256"],
                "artifact_sha256": sha256_text(markdown),
                "artifact_markdown": markdown,
                "metadata": metadata,
            }
        )
    return {
        "schema": PLAN_SCHEMA,
        "generated_at": timestamp,
        "policy_sha256": sha256_bytes(canonical_json(policy)),
        "publisher_principal": policy["publisher_principal"],
        "source_instance": policy["source_instance"],
        "actions": actions,
    }


def plan_sha256(plan: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(plan))


def validate_plan(
    plan: Any,
    policy_path: Path,
    database: Path,
) -> dict[str, Any]:
    if (
        not isinstance(plan, dict)
        or set(plan) != _PLAN_KEYS
        or plan.get("schema") != PLAN_SCHEMA
    ):
        raise PublicationError("unsupported publication plan")
    policy = load_policy(policy_path)
    if plan.get("policy_sha256") != sha256_bytes(canonical_json(policy)):
        raise PublicationError("publication policy changed since planning")
    if plan.get("publisher_principal") != policy["publisher_principal"]:
        raise PublicationError("publisher principal changed since planning")
    if not isinstance(plan.get("generated_at"), str) or not plan["generated_at"]:
        raise PublicationError("publication plan timestamp is invalid")
    if not isinstance(plan.get("actions"), list):
        raise PublicationError("publication plan actions are invalid")
    targets = []
    for item in plan["actions"]:
        if not isinstance(item, dict) or item.get("action") not in {"publish", "revoke"}:
            raise PublicationError("publication plan contains an invalid action")
        expected_keys = (
            _PLAN_PUBLISH_KEYS
            if item["action"] == "publish"
            else _PLAN_REVOKE_KEYS
        )
        if set(item) != expected_keys:
            raise PublicationError("publication plan action has unknown or missing fields")
        targets.append(safe_relative_path(item.get("target", ""), "plan target"))
        try:
            uri_chapter_id = (
                item["chapter_id"]
                if item["action"] == "publish"
                else int(item["source_uri"].rsplit("/", 1)[-1])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicationError("publication plan source URI is invalid") from exc
        if item["source_uri"] != source_uri(policy, uri_chapter_id):
            raise PublicationError("publication plan source URI is invalid")
        if item["artifact_id"] != artifact_id(item["source_uri"]):
            raise PublicationError("publication plan artifact ID is invalid")
        if item["action"] == "publish":
            chapter = read_chapter(database, item["chapter_id"])
            if sha256_text(chapter["raw"]) != item.get("content_sha256"):
                raise PublicationError(
                    f"HMK chapter changed since planning: {item['chapter_id']}"
                )
            if sha256_text(item.get("artifact_markdown", "")) != item.get(
                "artifact_sha256"
            ):
                raise PublicationError("publication artifact changed since planning")
    if len(targets) != len(set(targets)):
        raise PublicationError("publication plan contains duplicate targets")
    expected = build_plan(
        database, policy_path, generated_at=plan["generated_at"]
    )
    if canonical_json(plan) != canonical_json(expected):
        raise PublicationError(
            "publication plan does not match current policy/source state"
        )
    return policy


def validate_approval(
    approval: Any, plan: Mapping[str, Any]
) -> dict[str, Any]:
    raw = dict(_closed_mapping(approval, _APPROVAL_KEYS, "approval"))
    if raw["schema"] != APPROVAL_SCHEMA:
        raise PublicationError(f"approval must use {APPROVAL_SCHEMA}")
    if raw["decision"] != "approved":
        raise PublicationError("publication approval decision is not approved")
    if raw["plan_sha256"] != plan_sha256(plan):
        raise PublicationError("approval does not match the exact publication plan")
    if (
        not isinstance(raw["reviewer_principal"], str)
        or not raw["reviewer_principal"]
        or raw["reviewer_principal"] == plan["publisher_principal"]
    ):
        raise PublicationError("approval requires an independent reviewer principal")
    if not isinstance(raw["approved_at"], str) or not raw["approved_at"]:
        raise PublicationError("approval timestamp is required")
    try:
        datetime.fromisoformat(raw["approved_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationError("approval timestamp must be ISO-8601") from exc
    return raw


def load_approval(path: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    return validate_approval(read_json(path), plan)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": STATE_SCHEMA, "published": {}, "tombstones": {}}
    state = read_json(path)
    if (
        not isinstance(state, dict)
        or state.get("schema") != STATE_SCHEMA
        or not isinstance(state.get("published"), dict)
        or not isinstance(state.get("tombstones"), dict)
    ):
        raise PublicationError("publication state is invalid")
    return state


def _target(root: Path, relative: str) -> Path:
    resolved_root = root.expanduser().resolve()
    target = (resolved_root / relative).resolve()
    if resolved_root not in target.parents:
        raise PublicationError("publication target escapes destination root")
    return target


def apply_plan(
    plan: dict[str, Any],
    approval: dict[str, Any],
    *,
    database: Path,
    policy_path: Path,
    destination: Path,
    state_dir: Path,
) -> dict[str, Any]:
    validate_plan(plan, policy_path, database)
    approval = validate_approval(approval, plan)
    state_path = state_dir.expanduser().resolve() / "state.json"
    state = _load_state(state_path)
    preflight = []
    for item in plan["actions"]:
        target = _target(destination, item["target"])
        previous = state["published"].get(item["source_uri"])
        current_bytes = target.read_bytes() if target.is_file() else None
        current_hash = sha256_bytes(current_bytes) if current_bytes is not None else None
        if target.exists() and not target.is_file():
            raise PublicationError(f"publication target is not a regular file: {target}")
        if item["action"] == "publish":
            if current_hash not in {
                None,
                item["artifact_sha256"],
                previous.get("artifact_sha256") if isinstance(previous, dict) else None,
            }:
                raise PublicationError(f"publication target has drifted: {target}")
            if previous and previous.get("target") != item["target"]:
                raise PublicationError("publication state target conflicts with plan")
        elif previous:
            if previous.get("target") != item["target"]:
                raise PublicationError("revocation target conflicts with publication state")
            if current_hash not in {None, previous.get("artifact_sha256")}:
                raise PublicationError(f"refusing to revoke drifted artifact: {target}")
        preflight.append((item, target, current_hash, current_bytes))

    # Close the window between source validation and target mutation.
    validate_plan(plan, policy_path, database)

    now = utc_iso()
    results = []
    receipt_path = (
        state_dir.expanduser().resolve()
        / "receipts"
        / f"{approval['plan_sha256']}.json"
    )
    state_before = state_path.read_bytes() if state_path.is_file() else None
    receipt_before = receipt_path.read_bytes() if receipt_path.is_file() else None
    try:
        for item, target, current_hash, _ in preflight:
            uri = item["source_uri"]
            if item["action"] == "publish":
                if current_hash != item["artifact_sha256"]:
                    write_bytes_atomic(
                        target, item["artifact_markdown"].encode("utf-8"), mode=0o644
                    )
                    result = "published" if current_hash is None else "updated"
                else:
                    result = "unchanged"
                state["published"][uri] = {
                    "target": item["target"],
                    "artifact_sha256": item["artifact_sha256"],
                    "content_sha256": item["content_sha256"],
                    "plan_sha256": approval["plan_sha256"],
                    "published_at": now,
                }
                state["tombstones"].pop(uri, None)
            else:
                target.unlink(missing_ok=True)
                previous = state["published"].pop(uri, None)
                state["tombstones"][uri] = {
                    "target": item["target"],
                    "last_artifact_sha256": (
                        previous.get("artifact_sha256") if previous else None
                    ),
                    "reason": item["revocation_reason"],
                    "revoked_at": now,
                    "plan_sha256": approval["plan_sha256"],
                }
                result = "revoked" if previous or current_hash else "already-absent"
            results.append(
                {
                    "action": item["action"],
                    "source_uri": uri,
                    "target": item["target"],
                    "result": result,
                }
            )

        # If HMK changed during target writes, restore the prior corpus bytes
        # instead of issuing a receipt for stale provenance.
        validate_plan(plan, policy_path, database)
        write_json_atomic(state_path, state)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "plan_sha256": approval["plan_sha256"],
            "policy_sha256": plan["policy_sha256"],
            "publisher_principal": plan["publisher_principal"],
            "reviewer_principal": approval["reviewer_principal"],
            "completed_at": now,
            "results": results,
        }
        write_json_atomic(receipt_path, receipt)
        return receipt
    except Exception as original:
        rollback_failures = []
        for _, target, _, original_bytes in reversed(preflight):
            try:
                if original_bytes is None:
                    target.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(target, original_bytes, mode=0o644)
            except Exception as exc:
                rollback_failures.append(f"{target}: {exc}")
        for path, original_bytes in (
            (state_path, state_before),
            (receipt_path, receipt_before),
        ):
            try:
                if original_bytes is None:
                    path.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(path, original_bytes)
            except Exception as exc:
                rollback_failures.append(f"{path}: {exc}")
        if rollback_failures:
            raise PublicationError(
                f"publish failed: {original}; rollback failed: "
                + "; ".join(rollback_failures)
            ) from original
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--database", type=Path, required=True)
    plan_parser.add_argument("--policy", type=Path, required=True)
    plan_parser.add_argument("--plan-out", type=Path)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--database", type=Path, required=True)
    publish_parser.add_argument("--policy", type=Path, required=True)
    publish_parser.add_argument("--plan", type=Path, required=True)
    publish_parser.add_argument("--approval", type=Path, required=True)
    publish_parser.add_argument("--destination", type=Path, required=True)
    publish_parser.add_argument("--state-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "plan":
        plan = build_plan(args.database, args.policy)
        digest = plan_sha256(plan)
        if args.plan_out:
            write_json_atomic(args.plan_out.expanduser().resolve(), plan)
            print(f"PLAN: {len(plan['actions'])} actions written; sha256={digest}")
        else:
            print(f"DRY RUN: {len(plan['actions'])} publication actions")
            print(f"Plan sha256: {digest}")
            print("No plan artifact, corpus file, state, or database was changed.")
        return 0

    plan = read_json(args.plan.expanduser().resolve())
    validate_plan(plan, args.policy, args.database)
    approval = load_approval(args.approval.expanduser().resolve(), plan)
    receipt = apply_plan(
        plan,
        approval,
        database=args.database,
        policy_path=args.policy,
        destination=args.destination,
        state_dir=args.state_dir,
    )
    print(
        f"PUBLISH COMPLETE: {len(receipt['results'])} actions; "
        f"plan={receipt['plan_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
