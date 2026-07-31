from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "memory-ownership.v1.json"
EXPORT = ROOT / "scripts" / "export_obsidian.py"


def load_export_module():
    spec = importlib.util.spec_from_file_location("hmk_export_under_test", EXPORT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_policy_is_closed_and_has_one_authority_per_artifact() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert set(policy) == {
        "schema",
        "artifact_classes",
        "forbidden_overlaps",
    }
    assert policy["schema"] == "memory-ownership/v1"
    classes = policy["artifact_classes"]
    assert classes
    assert len({item["id"] for item in classes}) == len(classes)
    for item in classes:
        assert set(item) == {"id", "selector", "authority", "replicas"}
        assert item["authority"]["role"] == "source"
        assert all(replica["role"] != "source" for replica in item["replicas"])
        assert all(replica["rebuildable"] is True for replica in item["replicas"])


def test_policy_distinguishes_llm_wiki_from_hmk_projection() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in policy["artifact_classes"]}
    assert by_id["wiki-curated-knowledge"]["authority"]["store"] == "llm-wiki"
    assert by_id["hmk-native-memory"]["authority"]["store"] == "hmk"
    assert by_id["hmk-projection-output"]["authority"]["store"] == "hmk"
    assert policy["forbidden_overlaps"] == [
        {
            "left": "${HMK_VAULT_DIR}",
            "right": "${WIKI_PATH}",
            "reason": (
                "Generated HMK projections must never write into or contain "
                "the authoritative LLM Wiki."
            ),
        }
    ]


@pytest.mark.parametrize(
    ("projection", "wiki"),
    [
        ("/srv/wiki", "/srv/wiki"),
        ("/srv/wiki/generated", "/srv/wiki"),
        ("/srv", "/srv/wiki"),
    ],
)
def test_projection_rejects_any_overlap(projection: str, wiki: str) -> None:
    module = load_export_module()
    with pytest.raises(SystemExit, match="authoritative LLM Wiki"):
        module.validate_projection_target(Path(projection), Path(wiki))


def test_projection_accepts_disjoint_roots() -> None:
    module = load_export_module()
    module.validate_projection_target(
        Path("/srv/hmk-projection"),
        Path("/srv/llm-wiki"),
    )
