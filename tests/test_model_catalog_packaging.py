from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from alysis_code.litellm_static_provider import get_bundled_model_catalog_provenance


def test_pyproject_does_not_depend_on_litellm() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")

    assert '"litellm>=' not in pyproject
    assert '"litellm"' not in pyproject


def test_bundled_model_catalog_resource_is_readable() -> None:
    catalog_pkg = "alysis_code.model_catalog"
    catalog_text = (
        resources.files(catalog_pkg)
        .joinpath("litellm_model_prices_snapshot.json")
        .read_text(encoding="utf-8")
    )
    meta_text = (
        resources.files(catalog_pkg)
        .joinpath("litellm_model_prices_snapshot.meta.json")
        .read_text(encoding="utf-8")
    )

    catalog = json.loads(catalog_text)
    meta = json.loads(meta_text)

    assert isinstance(catalog, dict)
    assert isinstance(catalog.get("sample_spec"), dict)
    assert meta["schema_version"] == 1
    assert meta["source"] == "bundled_litellm_snapshot"
    assert meta["refresh_policy"] == "manual_reviewed_only"
    assert meta["snapshot"]["bundled_json_sha256"]
    assert meta["upstream"]["commit_sha"]
    assert meta["upstream"]["blob_url"]
    assert "ref" not in meta["upstream"]


def test_chatgpt_subscription_catalog_resource_is_readable_and_not_entitlement() -> None:
    catalog_text = (
        resources.files("alysis_code.model_catalog")
        .joinpath("chatgpt_codex_subscription_snapshot.json")
        .read_text(encoding="utf-8")
    )

    catalog = json.loads(catalog_text)

    assert catalog["schema_version"] == 1
    assert catalog["source"] == "bundled_chatgpt_codex_subscription_snapshot"
    assert catalog["refresh_policy"] == "manual_reviewed_only"
    assert catalog["usage"] == "capacity_and_capability_fallback_only_not_entitlement"
    assert catalog["client_version"]
    assert len(catalog["input_sha256"]) == 64
    assert catalog["models"]


def test_bundled_model_catalog_provenance_helper_reads_packaged_meta() -> None:
    provenance = get_bundled_model_catalog_provenance()
    assert provenance.error is None
    assert provenance.source == "bundled_litellm_snapshot"
    assert provenance.refresh_policy == "manual_reviewed_only"
    assert provenance.upstream_commit_sha
    assert provenance.fetched_at_utc and provenance.fetched_at_utc.endswith("Z")
    assert (
        provenance.upstream_blob_url
        and provenance.upstream_commit_sha in provenance.upstream_blob_url
    )
