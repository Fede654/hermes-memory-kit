#!/usr/bin/env python3
"""Generate deterministic public contract vectors for the Daimon projection API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any


INSTANCE = "hmk:vector-instance"
SOURCE = "matrix:vector-instance"
ME = "dm:being:v1:" + "A" * 43
PROJECTOR = {"id": "matrix:personal-memory-projector", "version": "1.0.0"}


def event(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "hmk-projection-vector:" + label))


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def statement(text: str, media_type: str = "text/plain") -> dict[str, Any]:
    raw = text.encode("utf-8")
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "media_type": media_type,
        "classification": "protected",
        "text": text,
    }


def target(api: Any) -> dict[str, Any]:
    return {
        "instance_id": INSTANCE,
        "api_version": api.API_VERSION,
        "schema_version": api.SCHEMA_VERSION,
    }


def request(
    api: Any,
    *,
    label: str,
    operation: str,
    memory_id: str,
    sequence: int,
    checkpoint_sequence: int,
    predecessor: str | None,
    text: str | None,
) -> dict[str, Any]:
    return {
        "schema": api.REQUEST_SCHEMA,
        "adapter": {"id": api.ADAPTER_ID, "version": api.API_VERSION},
        "request_id": event("request:" + label),
        "idempotency_key": "vector:" + label,
        "operation": operation,
        "target": target(api),
        "source_instance": SOURCE,
        "subject_me_id": ME,
        "author_me_id": ME,
        "memory_id": memory_id,
        "category": "personal-insight",
        "head": {
            "event_id": event("head:" + label),
            "event_hash": digest("head:" + label),
            "sequence": sequence,
            "predecessor_event_id": None if predecessor is None else event("head:" + predecessor),
            "predecessor_hash": None if predecessor is None else digest("head:" + predecessor),
        },
        "statement": None if text is None else statement(text),
        "projector": deepcopy(PROJECTOR),
        "source_checkpoint": {
            "sequence": checkpoint_sequence,
            "hash": digest(f"checkpoint:{checkpoint_sequence}"),
        },
    }


def rebuild_entry(label: str, sequence: int, text: str) -> dict[str, Any]:
    return {
        "memory_id": event("memory:" + label),
        "author_me_id": ME,
        "category": "personal-insight",
        "head": {
            "event_id": event("rebuild-head:" + label),
            "event_hash": digest("rebuild-head:" + label),
            "sequence": sequence,
        },
        "statement": statement(text, "text/markdown"),
    }


def generate(output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hmk-projection-vectors-") as temporary:
        os.environ["HMK_AGENT_MEMORY_BASE"] = temporary
        os.environ["HMK_INSTANCE_ID"] = INSTANCE
        os.environ.pop("HMK_DB_PATH", None)

        import daimon_projection as api_module

        api = api_module.ProjectionAPI(INSTANCE)
        memory_id = event("memory:primary")
        project = request(
            api_module,
            label="project",
            operation="project",
            memory_id=memory_id,
            sequence=1,
            checkpoint_sequence=1,
            predecessor=None,
            text="The orchard gate opens at dawn.",
        )
        project_receipt = api.apply(project)
        advance = request(
            api_module,
            label="advance",
            operation="advance",
            memory_id=memory_id,
            sequence=2,
            checkpoint_sequence=2,
            predecessor="project",
            text="The orchard gate now opens at sunrise.",
        )
        advance_receipt = api.apply(advance)
        conflicting = deepcopy(advance)
        conflicting["request_id"] = event("request:conflicting-head")
        conflicting["idempotency_key"] = "vector:conflicting-head"
        conflicting["statement"] = statement("Contradictory bytes at the same head.")
        retract = request(
            api_module,
            label="retract",
            operation="retract",
            memory_id=memory_id,
            sequence=3,
            checkpoint_sequence=3,
            predecessor="advance",
            text=None,
        )
        retract_receipt = api.apply(retract)
        rebuild = {
            "schema": api_module.REBUILD_REQUEST_SCHEMA,
            "request_id": event("request:rebuild"),
            "idempotency_key": "vector:rebuild",
            "target": target(api_module),
            "source_instance": SOURCE,
            "subject_me_id": ME,
            "projector": deepcopy(PROJECTOR),
            "source_checkpoint": {
                "sequence": 10,
                "hash": digest("checkpoint:10"),
            },
            "entries": sorted(
                [
                    rebuild_entry("alpha", 4, "# Alpha\n\nA rebuilt personal insight."),
                    rebuild_entry("beta", 7, "# Beta\n\nAnother rebuilt personal insight."),
                ],
                key=lambda entry: entry["memory_id"],
            ),
        }
        rebuild_plan = api.rebuild_plan(rebuild)
        rebuild_apply = {"schema": api_module.REBUILD_APPLY_SCHEMA, "plan": rebuild_plan}
        rebuild_receipt = api.rebuild_apply(rebuild_apply)
        inspect_query = {
            "schema": api_module.INSPECT_SCHEMA,
            "target": target(api_module),
            "source_instance": SOURCE,
            "subject_me_id": ME,
            "memory_id": rebuild["entries"][0]["memory_id"],
            "projector": deepcopy(PROJECTOR),
        }
        inspect_result = api.inspect(inspect_query)
        verify_query = {
            "schema": api_module.VERIFY_SCHEMA,
            "target": target(api_module),
            "source_instance": SOURCE,
            "subject_me_id": ME,
            "projector": deepcopy(PROJECTOR),
        }
        verify_result = api.verify(verify_query)

        documents = {
            "project.request.json": project,
            "project.receipt.json": project_receipt,
            "advance.request.json": advance,
            "advance.receipt.json": advance_receipt,
            "negative.same-head-different-bytes.request.json": conflicting,
            "retract.request.json": retract,
            "retract.receipt.json": retract_receipt,
            "rebuild.request.json": rebuild,
            "rebuild.plan.json": rebuild_plan,
            "rebuild.apply.json": rebuild_apply,
            "rebuild.receipt.json": rebuild_receipt,
            "inspect.query.json": inspect_query,
            "inspect.result.json": inspect_result,
            "verify.query.json": verify_query,
            "verify.result.json": verify_result,
        }
        output.mkdir(parents=True, exist_ok=True)
        hashes: dict[str, str] = {}
        for name, document in sorted(documents.items()):
            wire = api_module.canonical_bytes(document) + b"\n"
            (output / name).write_bytes(wire)
            hashes[name] = hashlib.sha256(wire).hexdigest()
        index = {
            "schema": "hmk.daimon-projection.vector-index/v1",
            "api_version": api_module.API_VERSION,
            "schema_version": api_module.SCHEMA_VERSION,
            "files": hashes,
            "ordered_scenario": [
                "project.request.json",
                "advance.request.json",
                "negative.same-head-different-bytes.request.json",
                "retract.request.json",
                "rebuild.request.json",
                "rebuild.apply.json",
                "inspect.query.json",
                "verify.query.json",
            ],
            "expected_negative_diagnostics": {
                "negative.same-head-different-bytes.request.json": "projection_head_content_conflict"
            },
        }
        (output / "index.json").write_bytes(api_module.canonical_bytes(index) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "vectors" / "daimon-projection" / "v1",
    )
    args = parser.parse_args()
    generate(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
