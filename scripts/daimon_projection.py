#!/usr/bin/env python3
"""Versioned, provenance-safe Daimon Matrix projection API for HMK."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Final

import memoryctl

API_VERSION: Final = "1.0.0"
SCHEMA_VERSION: Final = 1
ADAPTER_ID: Final = "hmk-daimon-projection"
REQUEST_SCHEMA: Final = "hmk.daimon-projection.request/v1"
RECEIPT_SCHEMA: Final = "hmk.daimon-projection.receipt/v1"
INSPECT_SCHEMA: Final = "hmk.daimon-projection.inspect/v1"
VERIFY_SCHEMA: Final = "hmk.daimon-projection.verify/v1"
VERIFY_RESULT_SCHEMA: Final = "hmk.daimon-projection.verify-result/v1"
REBUILD_REQUEST_SCHEMA: Final = "hmk.daimon-projection.rebuild-request/v1"
REBUILD_PLAN_SCHEMA: Final = "hmk.daimon-projection.rebuild-plan/v1"
REBUILD_APPLY_SCHEMA: Final = "hmk.daimon-projection.rebuild-apply/v1"
REBUILD_RECEIPT_SCHEMA: Final = "hmk.daimon-projection.rebuild-receipt/v1"

MAX_DOCUMENT_BYTES: Final = 17 * 1024 * 1024
MAX_STATEMENT_BYTES: Final = 16 * 1024 * 1024
MAX_REBUILD_ITEMS: Final = 4096
MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991
CATEGORIES: Final = frozenset(
    {"personal-experience", "personal-insight", "personal-skill"}
)
CLASSIFICATIONS: Final = frozenset({"public", "personal", "private", "protected"})
MEDIA_TYPES: Final = frozenset({"text/plain", "text/markdown"})

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SCOPED = re.compile(r"^[a-z][a-z0-9.-]{1,31}:[A-Za-z0-9._:-]{1,160}$")
_ME_ID = re.compile(r"^dm:being:v1:[A-Za-z0-9_-]{43}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class ProjectionError(RuntimeError):
    """Stable protocol refusal."""


FaultHook = Callable[[str], None]


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ProjectionError("integer_out_of_range")
        return value
    if isinstance(value, float):
        raise ProjectionError("floating_point_refused")
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise ProjectionError("non_nfc_string")
        return value
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ProjectionError("non_string_key")
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    raise ProjectionError("unsupported_json_value")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectionError("duplicate_json_key")
        result[key] = value
    return result


def load_canonical_document(raw: bytes) -> dict[str, Any]:
    if not 1 <= len(raw) <= MAX_DOCUMENT_BYTES:
        raise ProjectionError("document_size_refused")
    wire = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        value = json.loads(wire, object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ProjectionError("invalid_json") from exception
    if not isinstance(value, dict) or canonical_bytes(value) != wire:
        raise ProjectionError("noncanonical_json")
    return value


def _closed(value: Any, fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProjectionError(code)
    return dict(value)


def _text(value: Any, code: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ProjectionError(code)
    return value


def _scoped(value: Any, code: str) -> str:
    text = _text(value, code)
    if not _SCOPED.fullmatch(text) or ".." in text:
        raise ProjectionError(code)
    return text


def _me_id(value: Any, code: str) -> str:
    text = _text(value, code)
    if not _ME_ID.fullmatch(text):
        raise ProjectionError(code)
    return text


def _uuid(value: Any, code: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exception:
        raise ProjectionError(code) from exception
    if parsed.version not in {4, 5} or str(parsed) != value:
        raise ProjectionError(code)
    return str(parsed)


def _hash(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ProjectionError(code)
    return value


def _uint(value: Any, code: str, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= MAX_SAFE_INTEGER
    ):
        raise ProjectionError(code)
    return value


def _token(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ProjectionError(code)
    return value


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _derived(prefix: str, domain: bytes, value: Any) -> str:
    return prefix + _b64url(hashlib.sha256(domain + canonical_bytes(value)).digest())


def namespace_identity(value: Mapping[str, Any]) -> dict[str, str]:
    return {
        "source_instance": str(value["source_instance"]),
        "subject_me_id": str(value["subject_me_id"]),
        "projector_id": str(value["projector"]["id"]),
        "projector_version": str(value["projector"]["version"]),
    }


def namespace_id(identity: Mapping[str, Any]) -> str:
    return _derived(
        "hmk:daimon-namespace:v1:",
        b"hmk/daimon-projection/namespace/v1\x00",
        identity,
    )


def projection_id(namespace: str, memory_id: str) -> str:
    return _derived(
        "hmk:daimon-projection:v1:",
        b"hmk/daimon-projection/identity/v1\x00",
        {"namespace_id": namespace, "memory_id": memory_id},
    )


def _validate_target(value: Any) -> dict[str, Any]:
    target = _closed(
        value,
        {"instance_id", "api_version", "schema_version"},
        "invalid_projection_target",
    )
    _scoped(target["instance_id"], "invalid_projection_target")
    if target["api_version"] != API_VERSION or target["schema_version"] != SCHEMA_VERSION:
        raise ProjectionError("unsupported_projection_target")
    return target


def _validate_projector(value: Any) -> dict[str, str]:
    projector = _closed(value, {"id", "version"}, "invalid_projector")
    identifier = _scoped(projector["id"], "invalid_projector")
    version = _text(projector["version"], "invalid_projector", maximum=32)
    if not _VERSION.fullmatch(version):
        raise ProjectionError("invalid_projector")
    return {"id": identifier, "version": version}


def _validate_checkpoint(value: Any) -> dict[str, Any]:
    checkpoint = _closed(value, {"sequence", "hash"}, "invalid_source_checkpoint")
    return {
        "sequence": _uint(checkpoint["sequence"], "invalid_source_checkpoint"),
        "hash": _hash(checkpoint["hash"], "invalid_source_checkpoint"),
    }


def _validate_head(value: Any, operation: str) -> dict[str, Any]:
    head = _closed(
        value,
        {
            "event_id",
            "event_hash",
            "sequence",
            "predecessor_event_id",
            "predecessor_hash",
        },
        "invalid_memory_head",
    )
    event_id = _uuid(head["event_id"], "invalid_memory_head")
    event_hash = _hash(head["event_hash"], "invalid_memory_head")
    sequence = _uint(head["sequence"], "invalid_memory_head", 1)
    predecessor_event_id = head["predecessor_event_id"]
    predecessor_hash = head["predecessor_hash"]
    if operation == "project":
        if sequence != 1 or predecessor_event_id is not None or predecessor_hash is not None:
            raise ProjectionError("invalid_assert_head")
    else:
        if sequence <= 1:
            raise ProjectionError("invalid_successor_head")
        predecessor_event_id = _uuid(predecessor_event_id, "invalid_successor_head")
        predecessor_hash = _hash(predecessor_hash, "invalid_successor_head")
    return {
        "event_id": event_id,
        "event_hash": event_hash,
        "sequence": sequence,
        "predecessor_event_id": predecessor_event_id,
        "predecessor_hash": predecessor_hash,
    }


def _validate_statement(value: Any) -> dict[str, Any]:
    statement = _closed(
        value,
        {"sha256", "byte_length", "media_type", "classification", "text"},
        "invalid_projection_statement",
    )
    if statement["media_type"] not in MEDIA_TYPES:
        raise ProjectionError("unsupported_projection_media_type")
    if statement["classification"] not in CLASSIFICATIONS:
        raise ProjectionError("invalid_projection_classification")
    if not isinstance(statement["text"], str):
        raise ProjectionError("invalid_projection_statement")
    encoded = statement["text"].encode("utf-8")
    length = _uint(statement["byte_length"], "invalid_projection_statement")
    if not 1 <= length <= MAX_STATEMENT_BYTES or length != len(encoded):
        raise ProjectionError("projection_statement_length_mismatch")
    digest = _hash(statement["sha256"], "invalid_projection_statement")
    if digest != hashlib.sha256(encoded).hexdigest():
        raise ProjectionError("projection_statement_hash_mismatch")
    return deepcopy(statement)


def validate_projection_request(value: Any) -> dict[str, Any]:
    request = _closed(
        value,
        {
            "schema",
            "adapter",
            "request_id",
            "idempotency_key",
            "operation",
            "target",
            "source_instance",
            "subject_me_id",
            "author_me_id",
            "memory_id",
            "category",
            "head",
            "statement",
            "projector",
            "source_checkpoint",
        },
        "invalid_projection_request",
    )
    if request["schema"] != REQUEST_SCHEMA:
        raise ProjectionError("unsupported_projection_request")
    adapter = _closed(request["adapter"], {"id", "version"}, "invalid_adapter")
    if adapter != {"id": ADAPTER_ID, "version": API_VERSION}:
        raise ProjectionError("unsupported_adapter")
    _uuid(request["request_id"], "invalid_projection_request_id")
    _token(request["idempotency_key"], "invalid_projection_idempotency_key")
    operation = request["operation"]
    if operation not in {"project", "advance", "retract"}:
        raise ProjectionError("unsupported_projection_operation")
    request["target"] = _validate_target(request["target"])
    _scoped(request["source_instance"], "invalid_source_instance")
    subject = _me_id(request["subject_me_id"], "invalid_projection_subject")
    author = _me_id(request["author_me_id"], "invalid_projection_author")
    if author != subject:
        raise ProjectionError("projection_author_not_subject")
    _uuid(request["memory_id"], "invalid_memory_id")
    if request["category"] not in CATEGORIES:
        raise ProjectionError("unsupported_projection_category")
    request["head"] = _validate_head(request["head"], operation)
    if operation == "retract":
        if request["statement"] is not None:
            raise ProjectionError("retraction_statement_must_be_null")
    else:
        request["statement"] = _validate_statement(request["statement"])
    request["projector"] = _validate_projector(request["projector"])
    request["source_checkpoint"] = _validate_checkpoint(
        request["source_checkpoint"]
    )
    if request["source_checkpoint"]["sequence"] < request["head"]["sequence"]:
        raise ProjectionError("source_checkpoint_precedes_head")
    return deepcopy(request)


def _validate_namespace_query(value: Any, schema: str) -> dict[str, Any]:
    fields = {
        "schema",
        "target",
        "source_instance",
        "subject_me_id",
        "projector",
    }
    if schema == INSPECT_SCHEMA:
        fields.add("memory_id")
    query = _closed(value, fields, "invalid_projection_query")
    if query["schema"] != schema:
        raise ProjectionError("unsupported_projection_query")
    query["target"] = _validate_target(query["target"])
    _scoped(query["source_instance"], "invalid_source_instance")
    _me_id(query["subject_me_id"], "invalid_projection_subject")
    query["projector"] = _validate_projector(query["projector"])
    if schema == INSPECT_SCHEMA:
        _uuid(query["memory_id"], "invalid_memory_id")
    return query


def _namespace_from_query(value: Mapping[str, Any]) -> dict[str, str]:
    return {
        "source_instance": str(value["source_instance"]),
        "subject_me_id": str(value["subject_me_id"]),
        "projector_id": str(value["projector"]["id"]),
        "projector_version": str(value["projector"]["version"]),
    }


def _validate_rebuild_entry(value: Any, subject_me_id: str) -> dict[str, Any]:
    entry = _closed(
        value,
        {"memory_id", "author_me_id", "category", "head", "statement"},
        "invalid_rebuild_entry",
    )
    _uuid(entry["memory_id"], "invalid_memory_id")
    author = _me_id(entry["author_me_id"], "invalid_projection_author")
    if author != subject_me_id:
        raise ProjectionError("projection_author_not_subject")
    if entry["category"] not in CATEGORIES:
        raise ProjectionError("unsupported_projection_category")
    head = _closed(
        entry["head"],
        {"event_id", "event_hash", "sequence"},
        "invalid_memory_head",
    )
    entry["head"] = {
        "event_id": _uuid(head["event_id"], "invalid_memory_head"),
        "event_hash": _hash(head["event_hash"], "invalid_memory_head"),
        "sequence": _uint(head["sequence"], "invalid_memory_head", 1),
    }
    entry["statement"] = _validate_statement(entry["statement"])
    return entry


def validate_rebuild_request(value: Any) -> dict[str, Any]:
    request = _closed(
        value,
        {
            "schema",
            "request_id",
            "idempotency_key",
            "target",
            "source_instance",
            "subject_me_id",
            "projector",
            "source_checkpoint",
            "entries",
        },
        "invalid_rebuild_request",
    )
    if request["schema"] != REBUILD_REQUEST_SCHEMA:
        raise ProjectionError("unsupported_rebuild_request")
    _uuid(request["request_id"], "invalid_projection_request_id")
    _token(request["idempotency_key"], "invalid_projection_idempotency_key")
    request["target"] = _validate_target(request["target"])
    _scoped(request["source_instance"], "invalid_source_instance")
    subject = _me_id(request["subject_me_id"], "invalid_projection_subject")
    request["projector"] = _validate_projector(request["projector"])
    request["source_checkpoint"] = _validate_checkpoint(
        request["source_checkpoint"]
    )
    if (
        not isinstance(request["entries"], list)
        or len(request["entries"]) > MAX_REBUILD_ITEMS
    ):
        raise ProjectionError("invalid_rebuild_entries")
    entries = [_validate_rebuild_entry(item, subject) for item in request["entries"]]
    memory_ids = [item["memory_id"] for item in entries]
    if memory_ids != sorted(set(memory_ids)):
        raise ProjectionError("rebuild_entries_not_canonical")
    if entries and request["source_checkpoint"]["sequence"] < max(
        item["head"]["sequence"] for item in entries
    ):
        raise ProjectionError("source_checkpoint_precedes_head")
    request["entries"] = entries
    return deepcopy(request)


def _manifest_entry(
    namespace_identifier: str, entry: Mapping[str, Any]
) -> dict[str, Any]:
    statement = entry["statement"]
    return {
        "projection_id": projection_id(namespace_identifier, entry["memory_id"]),
        "memory_id": entry["memory_id"],
        "author_me_id": entry["author_me_id"],
        "category": entry["category"],
        "head": deepcopy(entry["head"]),
        "statement": {
            "sha256": statement["sha256"],
            "byte_length": statement["byte_length"],
            "media_type": statement["media_type"],
            "classification": statement["classification"],
        },
        "active": True,
    }


def _manifest_hash(entries: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_bytes(list(entries))).hexdigest()


def validate_rebuild_plan(value: Any) -> dict[str, Any]:
    plan = _closed(
        value,
        {
            "schema",
            "plan_id",
            "request_id",
            "idempotency_key",
            "target",
            "namespace",
            "namespace_id",
            "source_checkpoint",
            "entries",
            "manifest_hash",
            "prior",
        },
        "invalid_rebuild_plan",
    )
    if plan["schema"] != REBUILD_PLAN_SCHEMA:
        raise ProjectionError("unsupported_rebuild_plan")
    _uuid(plan["request_id"], "invalid_projection_request_id")
    _token(plan["idempotency_key"], "invalid_projection_idempotency_key")
    plan["target"] = _validate_target(plan["target"])
    namespace = _closed(
        plan["namespace"],
        {"source_instance", "subject_me_id", "projector_id", "projector_version"},
        "invalid_rebuild_namespace",
    )
    _scoped(namespace["source_instance"], "invalid_source_instance")
    _me_id(namespace["subject_me_id"], "invalid_projection_subject")
    _scoped(namespace["projector_id"], "invalid_projector")
    if not _VERSION.fullmatch(str(namespace["projector_version"])):
        raise ProjectionError("invalid_projector")
    expected_namespace_id = namespace_id(namespace)
    if plan["namespace_id"] != expected_namespace_id:
        raise ProjectionError("rebuild_namespace_id_mismatch")
    plan["source_checkpoint"] = _validate_checkpoint(plan["source_checkpoint"])
    if not isinstance(plan["entries"], list) or len(plan["entries"]) > MAX_REBUILD_ITEMS:
        raise ProjectionError("invalid_rebuild_entries")
    entries = [
        _validate_rebuild_entry(item, namespace["subject_me_id"])
        for item in plan["entries"]
    ]
    memory_ids = [item["memory_id"] for item in entries]
    if memory_ids != sorted(set(memory_ids)):
        raise ProjectionError("rebuild_entries_not_canonical")
    if entries and plan["source_checkpoint"]["sequence"] < max(
        item["head"]["sequence"] for item in entries
    ):
        raise ProjectionError("source_checkpoint_precedes_head")
    manifest = [_manifest_entry(expected_namespace_id, item) for item in entries]
    if plan["manifest_hash"] != _manifest_hash(manifest):
        raise ProjectionError("rebuild_manifest_hash_mismatch")
    if plan["prior"] is not None:
        prior = _closed(
            plan["prior"],
            {"generation", "manifest_hash", "source_checkpoint"},
            "invalid_rebuild_prior",
        )
        _uint(prior["generation"], "invalid_rebuild_prior")
        _hash(prior["manifest_hash"], "invalid_rebuild_prior")
        prior["source_checkpoint"] = _validate_checkpoint(prior["source_checkpoint"])
    body = {key: deepcopy(plan[key]) for key in plan if key != "plan_id"}
    expected_plan_id = _derived(
        "hmk:daimon-rebuild-plan:v1:",
        b"hmk/daimon-projection/rebuild-plan/v1\x00",
        body,
    )
    if plan["plan_id"] != expected_plan_id:
        raise ProjectionError("rebuild_plan_id_mismatch")
    return deepcopy(plan)


def validate_rebuild_apply(value: Any) -> dict[str, Any]:
    request = _closed(value, {"schema", "plan"}, "invalid_rebuild_apply")
    if request["schema"] != REBUILD_APPLY_SCHEMA:
        raise ProjectionError("unsupported_rebuild_apply")
    request["plan"] = validate_rebuild_plan(request["plan"])
    return request


def _state(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "projection_id": row["projection_id"],
        "namespace_id": row["namespace_id"],
        "memory_id": row["memory_id"],
        "author_me_id": row["author_me_id"],
        "category": row["category"],
        "head": {
            "event_id": row["head_event_id"],
            "event_hash": row["head_event_hash"],
            "sequence": row["head_sequence"],
        },
        "statement": {
            "sha256": row["statement_hash"],
            "byte_length": row["statement_length"],
            "media_type": row["statement_media_type"],
            "classification": row["classification"],
        },
        "source_checkpoint": {
            "sequence": row["source_checkpoint_sequence"],
            "hash": row["source_checkpoint_hash"],
        },
        "active": bool(row["active"]),
    }


def _row_manifest_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "projection_id": row["projection_id"],
        "memory_id": row["memory_id"],
        "author_me_id": row["author_me_id"],
        "category": row["category"],
        "head": {
            "event_id": row["head_event_id"],
            "event_hash": row["head_event_hash"],
            "sequence": row["head_sequence"],
        },
        "statement": {
            "sha256": row["statement_hash"],
            "byte_length": row["statement_length"],
            "media_type": row["statement_media_type"],
            "classification": row["classification"],
        },
        "active": True,
    }


class ProjectionAPI:
    def __init__(self, instance_id: str, fault_hook: FaultHook | None = None):
        self.instance_id = _scoped(instance_id, "invalid_hmk_instance")
        self.fault_hook = fault_hook

    def _fault(self, stage: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(stage)

    def _connect(self) -> sqlite3.Connection:
        memoryctl.init_db()
        con = memoryctl.connect()
        schema = con.execute(
            "SELECT schema_version FROM daimon_projection_schema WHERE singleton=1"
        ).fetchone()
        if schema is None or schema["schema_version"] != SCHEMA_VERSION:
            con.close()
            raise ProjectionError("unsupported_projection_schema")
        return con

    def _check_target(self, target: Mapping[str, Any]) -> None:
        if target["instance_id"] != self.instance_id:
            raise ProjectionError("projection_target_instance_mismatch")

    @staticmethod
    def _namespace_row(
        con: sqlite3.Connection, identifier: str
    ) -> sqlite3.Row | None:
        return con.execute(
            "SELECT * FROM daimon_projection_namespaces WHERE namespace_id=?",
            (identifier,),
        ).fetchone()

    @staticmethod
    def _projection_row(
        con: sqlite3.Connection, identifier: str
    ) -> sqlite3.Row | None:
        return con.execute(
            "SELECT * FROM daimon_projections WHERE projection_id=?",
            (identifier,),
        ).fetchone()

    @classmethod
    def _verified_text(cls, con: sqlite3.Connection, projection: Mapping[str, Any]) -> str:
        chapter_id = projection["chapter_id"]
        materialized = con.execute(
            """
            SELECT c.raw, c.embed_disabled, b.source_kind, s.name AS shelf
            FROM chapters c
            JOIN books b ON b.id=c.book_id
            JOIN shelves s ON s.id=b.shelf_id
            WHERE c.id=?
            """,
            (chapter_id,),
        ).fetchone()
        if (
            materialized is None
            or materialized["source_kind"] != "daimon-projection"
            or materialized["shelf"] != "daimon-projection"
            or materialized["embed_disabled"] != 1
        ):
            raise ProjectionError("projection_materialization_drift")
        text = str(materialized["raw"])
        raw = text.encode("utf-8")
        if (
            len(raw) != projection["statement_length"]
            or hashlib.sha256(raw).hexdigest() != projection["statement_hash"]
        ):
            raise ProjectionError("projection_content_drift")
        visible = con.execute(
            "SELECT 1 FROM chapters_fts WHERE rowid=?",
            (chapter_id,),
        ).fetchone() is not None
        if visible != bool(projection["active"]):
            raise ProjectionError("projection_retrieval_drift")
        if con.execute(
            "SELECT 1 FROM chapter_embeddings WHERE chapter_id=? LIMIT 1",
            (chapter_id,),
        ).fetchone() is not None:
            raise ProjectionError("projection_embedding_drift")
        return text

    @staticmethod
    def _checkpoint_guard(namespace: sqlite3.Row, checkpoint: Mapping[str, Any]) -> None:
        current_sequence = int(namespace["accepted_checkpoint_sequence"])
        current_hash = str(namespace["accepted_checkpoint_hash"])
        if checkpoint["sequence"] < current_sequence:
            raise ProjectionError("projection_checkpoint_regression")
        if checkpoint["sequence"] == current_sequence and checkpoint["hash"] != current_hash:
            raise ProjectionError("projection_checkpoint_fork")

    @staticmethod
    def _receipt(
        request: Mapping[str, Any],
        request_hash: str,
        projection: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
        namespace: Mapping[str, Any],
        outcome: str,
    ) -> dict[str, Any]:
        body = {
            "schema": RECEIPT_SCHEMA,
            "request_id": request["request_id"],
            "idempotency_key": request["idempotency_key"],
            "request_hash": request_hash,
            "operation": request["operation"],
            "outcome": outcome,
            "target": deepcopy(request["target"]),
            "namespace": deepcopy(dict(namespace)),
            "previous": None if previous is None else deepcopy(dict(previous)),
            "current": deepcopy(dict(projection)),
        }
        return {
            **body,
            "receipt_id": _derived(
                "hmk:daimon-receipt:v1:",
                b"hmk/daimon-projection/receipt/v1\x00",
                body,
            ),
        }

    @staticmethod
    def _store_idempotency(
        con: sqlite3.Connection,
        key: str,
        request_hash: str,
        receipt: Mapping[str, Any],
    ) -> None:
        con.execute(
            """
            INSERT INTO daimon_projection_idempotency(
                idempotency_key, request_hash, receipt_json
            ) VALUES(?, ?, ?)
            """,
            (key, request_hash, canonical_bytes(receipt).decode("utf-8")),
        )

    @staticmethod
    def _existing_idempotency(
        con: sqlite3.Connection, key: str, request_hash: str
    ) -> dict[str, Any] | None:
        row = con.execute(
            "SELECT request_hash, receipt_json FROM daimon_projection_idempotency WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ProjectionError("projection_idempotency_conflict")
        return json.loads(row["receipt_json"])

    @staticmethod
    def _semantic_receipt(
        con: sqlite3.Connection,
        projection: sqlite3.Row,
        request: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if (
            projection["head_event_id"] != request["head"]["event_id"]
            or projection["head_event_hash"] != request["head"]["event_hash"]
            or projection["head_sequence"] != request["head"]["sequence"]
        ):
            return None
        if (
            projection["author_me_id"] != request["author_me_id"]
            or projection["category"] != request["category"]
            or projection["source_checkpoint_sequence"]
            != request["source_checkpoint"]["sequence"]
            or projection["source_checkpoint_hash"]
            != request["source_checkpoint"]["hash"]
        ):
            raise ProjectionError("projection_replay_binding_mismatch")
        if request["operation"] == "retract":
            same = not bool(projection["active"])
        else:
            statement = request["statement"]
            same = (
                bool(projection["active"])
                and projection["statement_hash"] == statement["sha256"]
                and projection["statement_length"] == statement["byte_length"]
                and projection["statement_media_type"] == statement["media_type"]
                and projection["classification"] == statement["classification"]
            )
        if not same:
            raise ProjectionError("projection_head_content_conflict")
        row = con.execute(
            """
            SELECT receipt_json FROM daimon_projection_history
            WHERE projection_id=? ORDER BY rowid DESC LIMIT 1
            """,
            (projection["projection_id"],),
        ).fetchone()
        return None if row is None else json.loads(row["receipt_json"])

    @staticmethod
    def _insert_chapter(
        con: sqlite3.Connection,
        identifier: str,
        memory_id: str,
        category: str,
        classification: str,
        text: str,
    ) -> int:
        shelf = con.execute(
            "SELECT id FROM shelves WHERE name='daimon-projection'"
        ).fetchone()
        if shelf is None:
            raise ProjectionError("projection_shelf_unavailable")
        title = f"Daimon memory {memory_id}"
        slug = "daimon-" + identifier.rsplit(":", 1)[-1].lower()
        now = memoryctl.now_ts()
        try:
            cursor = con.execute(
                """
                INSERT INTO books(
                    shelf_id, slug, title, source_path, source_kind, created_at, updated_at
                ) VALUES(?, ?, ?, NULL, 'daimon-projection', ?, ?)
                """,
                (shelf["id"], slug, title, now, now),
            )
        except sqlite3.IntegrityError as exception:
            raise ProjectionError("projection_destination_collision") from exception
        book_id = int(cursor.lastrowid)
        spr = memoryctl.simple_spr(text)
        tags = json.dumps(
            ["daimon-projection", category, classification], separators=(",", ":")
        )
        cursor = con.execute(
            """
            INSERT INTO chapters(
                book_id, ordinal, title, spr, raw, tokens, importance,
                created_at, updated_at, tags_json, embed_disabled,
                embed_disable_reason
            ) VALUES(?, 1, ?, ?, ?, ?, 0.5, ?, ?, ?, 1, 'source_kind=daimon-projection')
            """,
            (
                book_id,
                title,
                spr,
                text,
                memoryctl.token_estimate(text),
                now,
                now,
                tags,
            ),
        )
        chapter_id = int(cursor.lastrowid)
        memoryctl.insert_chapter_fts(con, chapter_id, title, spr, text, tags)
        return chapter_id

    @staticmethod
    def _advance_chapter(
        con: sqlite3.Connection,
        chapter_id: int,
        category: str,
        classification: str,
        text: str,
    ) -> None:
        row = con.execute(
            "SELECT id, book_id, title, spr, raw, tags_json FROM chapters WHERE id=?",
            (chapter_id,),
        ).fetchone()
        if row is None:
            raise ProjectionError("projection_chapter_missing")
        memoryctl.delete_chapter_fts(con, row)
        spr = memoryctl.simple_spr(text)
        tags = json.dumps(
            ["daimon-projection", category, classification], separators=(",", ":")
        )
        now = memoryctl.now_ts()
        con.execute(
            """
            UPDATE chapters SET spr=?, raw=?, tokens=?, tags_json=?, updated_at=?
            WHERE id=?
            """,
            (spr, text, memoryctl.token_estimate(text), tags, now, chapter_id),
        )
        con.execute("DELETE FROM chapter_embeddings WHERE chapter_id=?", (chapter_id,))
        con.execute("UPDATE books SET updated_at=? WHERE id=?", (now, row["book_id"]))
        memoryctl.insert_chapter_fts(con, chapter_id, row["title"], spr, text, tags)

    @staticmethod
    def _retract_chapter(con: sqlite3.Connection, chapter_id: int) -> None:
        row = con.execute(
            "SELECT id, title, spr, raw, tags_json FROM chapters WHERE id=?",
            (chapter_id,),
        ).fetchone()
        if row is None:
            raise ProjectionError("projection_chapter_missing")
        memoryctl.delete_chapter_fts(con, row)
        con.execute("DELETE FROM chapter_embeddings WHERE chapter_id=?", (chapter_id,))

    @staticmethod
    def _current_manifest(
        con: sqlite3.Connection, namespace_identifier: str
    ) -> tuple[list[dict[str, Any]], str]:
        rows = con.execute(
            """
            SELECT * FROM daimon_projections
            WHERE namespace_id=? AND active=1
            ORDER BY memory_id
            """,
            (namespace_identifier,),
        ).fetchall()
        manifest = [_row_manifest_entry(row) for row in rows]
        return manifest, _manifest_hash(manifest)

    @staticmethod
    def _clear_namespace(
        con: sqlite3.Connection, namespace_identifier: str
    ) -> None:
        rows = con.execute(
            """
            SELECT p.active, c.id, c.book_id, c.title, c.spr, c.raw, c.tags_json
            FROM daimon_projections p
            JOIN chapters c ON c.id=p.chapter_id
            WHERE p.namespace_id=?
            """,
            (namespace_identifier,),
        ).fetchall()
        con.execute(
            "DELETE FROM daimon_projections WHERE namespace_id=?",
            (namespace_identifier,),
        )
        for row in rows:
            if row["active"]:
                memoryctl.delete_chapter_fts(con, row)
            con.execute("DELETE FROM chapters WHERE id=?", (row["id"],))
            con.execute(
                "DELETE FROM books WHERE id=? AND NOT EXISTS (SELECT 1 FROM chapters WHERE book_id=?)",
                (row["book_id"], row["book_id"]),
            )

    def apply(self, value: Any) -> dict[str, Any]:
        request = validate_projection_request(value)
        self._check_target(request["target"])
        request_hash = hashlib.sha256(canonical_bytes(request)).hexdigest()
        identity = namespace_identity(request)
        ns_id = namespace_id(identity)
        proj_id = projection_id(ns_id, request["memory_id"])
        self._fault("before_begin")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = self._existing_idempotency(
                con, request["idempotency_key"], request_hash
            )
            if existing is not None:
                con.commit()
                return existing
            namespace = self._namespace_row(con, ns_id)
            projection = self._projection_row(con, proj_id)
            if namespace is None:
                if request["operation"] != "project" or projection is not None:
                    raise ProjectionError("projection_namespace_unknown")
                checkpoint = request["source_checkpoint"]
                con.execute(
                    """
                    INSERT INTO daimon_projection_namespaces(
                        namespace_id, source_instance, subject_me_id, projector_id,
                        projector_version, accepted_checkpoint_sequence,
                        accepted_checkpoint_hash, generation, current_manifest_hash
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        ns_id,
                        identity["source_instance"],
                        identity["subject_me_id"],
                        identity["projector_id"],
                        identity["projector_version"],
                        checkpoint["sequence"],
                        checkpoint["hash"],
                        "0" * 64,
                    ),
                )
            else:
                self._checkpoint_guard(namespace, request["source_checkpoint"])
            if projection is not None:
                semantic = self._semantic_receipt(con, projection, request)
                if semantic is not None:
                    self._store_idempotency(
                        con,
                        request["idempotency_key"],
                        request_hash,
                        semantic,
                    )
                    con.commit()
                    return semantic
            previous = None if projection is None else _state(projection)
            if request["operation"] == "project":
                if projection is not None:
                    raise ProjectionError("projection_already_exists")
                statement = request["statement"]
                chapter_id = self._insert_chapter(
                    con,
                    proj_id,
                    request["memory_id"],
                    request["category"],
                    statement["classification"],
                    statement["text"],
                )
                self._fault("after_chapter_before_projection")
                con.execute(
                    """
                    INSERT INTO daimon_projections(
                        projection_id, namespace_id, memory_id, author_me_id,
                        category, head_event_id, head_event_hash, head_sequence,
                        statement_hash, statement_length, statement_media_type,
                        classification,
                        source_checkpoint_sequence, source_checkpoint_hash,
                        active, chapter_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        proj_id,
                        ns_id,
                        request["memory_id"],
                        request["author_me_id"],
                        request["category"],
                        request["head"]["event_id"],
                        request["head"]["event_hash"],
                        request["head"]["sequence"],
                        statement["sha256"],
                        statement["byte_length"],
                        statement["media_type"],
                        statement["classification"],
                        request["source_checkpoint"]["sequence"],
                        request["source_checkpoint"]["hash"],
                        chapter_id,
                    ),
                )
            else:
                if projection is None:
                    raise ProjectionError("projection_unknown")
                if not bool(projection["active"]):
                    raise ProjectionError("projection_inactive")
                head = request["head"]
                if (
                    head["predecessor_event_id"] != projection["head_event_id"]
                    or head["predecessor_hash"] != projection["head_event_hash"]
                ):
                    raise ProjectionError("projection_predecessor_mismatch")
                if head["sequence"] != projection["head_sequence"] + 1:
                    raise ProjectionError("projection_sequence_not_contiguous")
                if request["category"] != projection["category"]:
                    raise ProjectionError("projection_category_changed")
                if request["author_me_id"] != projection["author_me_id"]:
                    raise ProjectionError("projection_author_changed")
                if request["operation"] == "advance":
                    statement = request["statement"]
                    self._advance_chapter(
                        con,
                        int(projection["chapter_id"]),
                        request["category"],
                        statement["classification"],
                        statement["text"],
                    )
                    statement_hash = statement["sha256"]
                    statement_length = statement["byte_length"]
                    statement_media_type = statement["media_type"]
                    classification = statement["classification"]
                    active = 1
                else:
                    self._retract_chapter(con, int(projection["chapter_id"]))
                    statement_hash = projection["statement_hash"]
                    statement_length = projection["statement_length"]
                    statement_media_type = projection["statement_media_type"]
                    classification = projection["classification"]
                    active = 0
                self._fault("after_chapter_before_projection")
                con.execute(
                    """
                    UPDATE daimon_projections SET
                        head_event_id=?, head_event_hash=?, head_sequence=?,
                        statement_hash=?, statement_length=?, statement_media_type=?,
                        classification=?,
                        source_checkpoint_sequence=?, source_checkpoint_hash=?,
                        active=?
                    WHERE projection_id=?
                    """,
                    (
                        head["event_id"],
                        head["event_hash"],
                        head["sequence"],
                        statement_hash,
                        statement_length,
                        statement_media_type,
                        classification,
                        request["source_checkpoint"]["sequence"],
                        request["source_checkpoint"]["hash"],
                        active,
                        proj_id,
                    ),
                )
            _manifest, manifest_hash = self._current_manifest(con, ns_id)
            con.execute(
                """
                UPDATE daimon_projection_namespaces SET
                    accepted_checkpoint_sequence=?, accepted_checkpoint_hash=?,
                    generation=generation+1, current_manifest_hash=?
                WHERE namespace_id=?
                """,
                (
                    request["source_checkpoint"]["sequence"],
                    request["source_checkpoint"]["hash"],
                    manifest_hash,
                    ns_id,
                ),
            )
            current_row = self._projection_row(con, proj_id)
            if current_row is None:
                raise ProjectionError("projection_commit_missing")
            current = _state(current_row)
            namespace_row = self._namespace_row(con, ns_id)
            if namespace_row is None:
                raise ProjectionError("projection_namespace_missing")
            namespace_state = {
                "namespace_id": ns_id,
                "generation": namespace_row["generation"],
                "manifest_hash": namespace_row["current_manifest_hash"],
                "source_checkpoint": {
                    "sequence": namespace_row["accepted_checkpoint_sequence"],
                    "hash": namespace_row["accepted_checkpoint_hash"],
                },
            }
            receipt = self._receipt(
                request,
                request_hash,
                current,
                previous,
                namespace_state,
                "applied",
            )
            receipt_json = canonical_bytes(receipt).decode("utf-8")
            con.execute(
                """
                INSERT INTO daimon_projection_history(
                    receipt_id, projection_id, request_hash, operation, receipt_json
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    receipt["receipt_id"],
                    proj_id,
                    request_hash,
                    request["operation"],
                    receipt_json,
                ),
            )
            self._store_idempotency(
                con, request["idempotency_key"], request_hash, receipt
            )
            self._fault("before_commit")
            con.commit()
            self._fault("after_commit_before_response")
            return receipt
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def rebuild_plan(self, value: Any) -> dict[str, Any]:
        request = validate_rebuild_request(value)
        self._check_target(request["target"])
        identity = {
            "source_instance": request["source_instance"],
            "subject_me_id": request["subject_me_id"],
            "projector_id": request["projector"]["id"],
            "projector_version": request["projector"]["version"],
        }
        ns_id = namespace_id(identity)
        manifest = [_manifest_entry(ns_id, item) for item in request["entries"]]
        con = self._connect()
        try:
            namespace = self._namespace_row(con, ns_id)
            prior = None
            if namespace is not None:
                self._checkpoint_guard(namespace, request["source_checkpoint"])
                prior = {
                    "generation": namespace["generation"],
                    "manifest_hash": namespace["current_manifest_hash"],
                    "source_checkpoint": {
                        "sequence": namespace["accepted_checkpoint_sequence"],
                        "hash": namespace["accepted_checkpoint_hash"],
                    },
                }
            shelf = con.execute(
                "SELECT id FROM shelves WHERE name='daimon-projection'"
            ).fetchone()
            if shelf is None:
                raise ProjectionError("projection_shelf_unavailable")
            for entry in manifest:
                slug = "daimon-" + entry["projection_id"].rsplit(":", 1)[-1].lower()
                collision = con.execute(
                    "SELECT source_kind FROM books WHERE shelf_id=? AND slug=?",
                    (shelf["id"], slug),
                ).fetchone()
                if collision is not None and collision["source_kind"] != "daimon-projection":
                    raise ProjectionError("projection_destination_collision")
        finally:
            con.close()
        body = {
            "schema": REBUILD_PLAN_SCHEMA,
            "request_id": request["request_id"],
            "idempotency_key": request["idempotency_key"],
            "target": deepcopy(request["target"]),
            "namespace": identity,
            "namespace_id": ns_id,
            "source_checkpoint": deepcopy(request["source_checkpoint"]),
            "entries": deepcopy(request["entries"]),
            "manifest_hash": _manifest_hash(manifest),
            "prior": prior,
        }
        plan = {
            **body,
            "plan_id": _derived(
                "hmk:daimon-rebuild-plan:v1:",
                b"hmk/daimon-projection/rebuild-plan/v1\x00",
                body,
            ),
        }
        return validate_rebuild_plan(plan)

    def rebuild_apply(self, value: Any) -> dict[str, Any]:
        request = validate_rebuild_apply(value)
        plan = request["plan"]
        self._check_target(plan["target"])
        plan_hash = hashlib.sha256(canonical_bytes(plan)).hexdigest()
        idempotency_key = "rebuild:" + plan["idempotency_key"]
        self._fault("before_begin")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            existing = self._existing_idempotency(con, idempotency_key, plan_hash)
            if existing is not None:
                con.commit()
                return existing
            stored = con.execute(
                "SELECT plan_hash, receipt_json FROM daimon_projection_rebuilds WHERE plan_id=?",
                (plan["plan_id"],),
            ).fetchone()
            if stored is not None:
                if stored["plan_hash"] != plan_hash:
                    raise ProjectionError("rebuild_plan_conflict")
                receipt = json.loads(stored["receipt_json"])
                self._store_idempotency(con, idempotency_key, plan_hash, receipt)
                con.commit()
                return receipt
            namespace = self._namespace_row(con, plan["namespace_id"])
            prior = plan["prior"]
            if (namespace is None) != (prior is None):
                raise ProjectionError("rebuild_target_drift")
            if namespace is not None:
                actual_prior = {
                    "generation": namespace["generation"],
                    "manifest_hash": namespace["current_manifest_hash"],
                    "source_checkpoint": {
                        "sequence": namespace["accepted_checkpoint_sequence"],
                        "hash": namespace["accepted_checkpoint_hash"],
                    },
                }
                if actual_prior != prior:
                    raise ProjectionError("rebuild_target_drift")
                self._checkpoint_guard(namespace, plan["source_checkpoint"])
            self._clear_namespace(con, plan["namespace_id"])
            self._fault("after_clear_before_rebuild")
            if namespace is None:
                con.execute(
                    """
                    INSERT INTO daimon_projection_namespaces(
                        namespace_id, source_instance, subject_me_id, projector_id,
                        projector_version, accepted_checkpoint_sequence,
                        accepted_checkpoint_hash, generation, current_manifest_hash
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        plan["namespace_id"],
                        plan["namespace"]["source_instance"],
                        plan["namespace"]["subject_me_id"],
                        plan["namespace"]["projector_id"],
                        plan["namespace"]["projector_version"],
                        plan["source_checkpoint"]["sequence"],
                        plan["source_checkpoint"]["hash"],
                        plan["manifest_hash"],
                    ),
                )
                generation = 1
            else:
                generation = int(namespace["generation"]) + 1
                con.execute(
                    """
                    UPDATE daimon_projection_namespaces SET
                        accepted_checkpoint_sequence=?, accepted_checkpoint_hash=?,
                        generation=?, current_manifest_hash=?
                    WHERE namespace_id=?
                    """,
                    (
                        plan["source_checkpoint"]["sequence"],
                        plan["source_checkpoint"]["hash"],
                        generation,
                        plan["manifest_hash"],
                        plan["namespace_id"],
                    ),
                )
            projection_ids = []
            for entry in plan["entries"]:
                identifier = projection_id(plan["namespace_id"], entry["memory_id"])
                statement = entry["statement"]
                chapter_id = self._insert_chapter(
                    con,
                    identifier,
                    entry["memory_id"],
                    entry["category"],
                    statement["classification"],
                    statement["text"],
                )
                con.execute(
                    """
                    INSERT INTO daimon_projections(
                        projection_id, namespace_id, memory_id, author_me_id,
                        category, head_event_id, head_event_hash, head_sequence,
                        statement_hash, statement_length, statement_media_type,
                        classification,
                        source_checkpoint_sequence, source_checkpoint_hash,
                        active, chapter_id
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        identifier,
                        plan["namespace_id"],
                        entry["memory_id"],
                        entry["author_me_id"],
                        entry["category"],
                        entry["head"]["event_id"],
                        entry["head"]["event_hash"],
                        entry["head"]["sequence"],
                        statement["sha256"],
                        statement["byte_length"],
                        statement["media_type"],
                        statement["classification"],
                        plan["source_checkpoint"]["sequence"],
                        plan["source_checkpoint"]["hash"],
                        chapter_id,
                    ),
                )
                projection_ids.append(identifier)
            _manifest, actual_manifest_hash = self._current_manifest(
                con, plan["namespace_id"]
            )
            if actual_manifest_hash != plan["manifest_hash"]:
                raise ProjectionError("rebuild_result_mismatch")
            receipt_body = {
                "schema": REBUILD_RECEIPT_SCHEMA,
                "plan_id": plan["plan_id"],
                "plan_hash": plan_hash,
                "target": deepcopy(plan["target"]),
                "namespace_id": plan["namespace_id"],
                "generation": generation,
                "source_checkpoint": deepcopy(plan["source_checkpoint"]),
                "manifest_hash": actual_manifest_hash,
                "projection_ids": sorted(projection_ids),
                "outcome": "rebuilt",
            }
            receipt = {
                **receipt_body,
                "receipt_id": _derived(
                    "hmk:daimon-rebuild-receipt:v1:",
                    b"hmk/daimon-projection/rebuild-receipt/v1\x00",
                    receipt_body,
                ),
            }
            receipt_json = canonical_bytes(receipt).decode("utf-8")
            con.execute(
                "INSERT INTO daimon_projection_rebuilds(plan_id, plan_hash, receipt_json) VALUES(?, ?, ?)",
                (plan["plan_id"], plan_hash, receipt_json),
            )
            self._store_idempotency(con, idempotency_key, plan_hash, receipt)
            self._fault("before_commit")
            con.commit()
            self._fault("after_commit_before_response")
            return receipt
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        finally:
            con.close()

    def inspect(self, value: Any) -> dict[str, Any]:
        query = _validate_namespace_query(value, INSPECT_SCHEMA)
        self._check_target(query["target"])
        identity = _namespace_from_query(query)
        ns_id = namespace_id(identity)
        proj_id = projection_id(ns_id, query["memory_id"])
        con = self._connect()
        try:
            namespace = self._namespace_row(con, ns_id)
            projection = self._projection_row(con, proj_id)
            if namespace is None or projection is None:
                raise ProjectionError("projection_unknown")
            result = _state(projection)
            text = self._verified_text(con, projection)
            result["statement"]["text"] = text
            return {
                "schema": "hmk.daimon-projection.inspect-result/v1",
                "target": deepcopy(query["target"]),
                "namespace": identity,
                "projection": result,
            }
        finally:
            con.close()

    def verify(self, value: Any) -> dict[str, Any]:
        query = _validate_namespace_query(value, VERIFY_SCHEMA)
        self._check_target(query["target"])
        identity = _namespace_from_query(query)
        ns_id = namespace_id(identity)
        con = self._connect()
        try:
            namespace = self._namespace_row(con, ns_id)
            if namespace is None:
                raise ProjectionError("projection_namespace_unknown")
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ProjectionError("hmk_integrity_failed")
            rows = con.execute(
                "SELECT * FROM daimon_projections WHERE namespace_id=? ORDER BY memory_id",
                (ns_id,),
            ).fetchall()
            projections = []
            manifest = []
            for row in rows:
                state = _state(row)
                self._verified_text(con, row)
                projections.append(state)
                if row["active"]:
                    manifest.append(_row_manifest_entry(row))
            logical_hash = _manifest_hash(manifest)
            if logical_hash != namespace["current_manifest_hash"]:
                raise ProjectionError("projection_manifest_drift")
            return {
                "schema": VERIFY_RESULT_SCHEMA,
                "target": deepcopy(query["target"]),
                "namespace": identity,
                "namespace_id": ns_id,
                "generation": namespace["generation"],
                "source_checkpoint": {
                    "sequence": namespace["accepted_checkpoint_sequence"],
                    "hash": namespace["accepted_checkpoint_hash"],
                },
                "manifest_hash": namespace["current_manifest_hash"],
                "logical_hash": logical_hash,
                "projections": projections,
            }
        finally:
            con.close()


def _read_stdin() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
    return load_canonical_document(raw)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="daimon_projection.py",
        description="Provenance-safe local Daimon projection boundary",
    )
    result.add_argument(
        "--instance-id",
        default=os.environ.get("HMK_INSTANCE_ID"),
        help="logical HMK instance ID (or HMK_INSTANCE_ID)",
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("apply", help="apply project/advance/retract request from stdin")
    commands.add_parser("inspect", help="inspect one projection from stdin")
    commands.add_parser("verify", help="verify one namespace from stdin")
    commands.add_parser("rebuild-plan", help="plan one exact namespace rebuild")
    commands.add_parser("rebuild-apply", help="atomically apply an exact rebuild plan")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.instance_id is None:
            raise ProjectionError("hmk_instance_required")
        api = ProjectionAPI(args.instance_id)
        document = _read_stdin()
        if args.command == "apply":
            output = api.apply(document)
        elif args.command == "inspect":
            output = api.inspect(document)
        elif args.command == "verify":
            output = api.verify(document)
        elif args.command == "rebuild-plan":
            output = api.rebuild_plan(document)
        elif args.command == "rebuild-apply":
            output = api.rebuild_apply(document)
        else:  # pragma: no cover
            raise ProjectionError("unsupported_projection_command")
        sys.stdout.buffer.write(canonical_bytes(output) + b"\n")
        return 0
    except ProjectionError as exception:
        sys.stderr.buffer.write(
            canonical_bytes(
                {
                    "schema": "hmk.daimon-projection.diagnostic/v1",
                    "code": str(exception),
                }
            )
            + b"\n"
        )
        return 1
    except Exception:
        sys.stderr.buffer.write(
            canonical_bytes(
                {
                    "schema": "hmk.daimon-projection.diagnostic/v1",
                    "code": "projection_boundary_failed",
                }
            )
            + b"\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
