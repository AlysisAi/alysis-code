from __future__ import annotations

from alysis_code.extensions.models import RegistryEntry, RegistryFile
from alysis_code.extensions.registry import find_by_id, load_registry, search


def test_load_registry_returns_registry_file() -> None:
    registry = load_registry()
    assert isinstance(registry, RegistryFile)
    assert registry.schema_version == 1
    assert isinstance(registry.extensions, list)


def test_find_by_id_matches_case_insensitive() -> None:
    registry = RegistryFile(
        extensions=[
            RegistryEntry(
                id="acme.jira",
                name="Acme Jira",
                description="Jira integration",
                repo="https://github.com/acme/jira-ext",
                commit="abc123",
                tags=["jira"],
            )
        ]
    )
    assert find_by_id(registry, "ACME.JIRA") is not None
    assert find_by_id(registry, "missing.ext") is None


def test_search_matches_id_name_description_and_tags_case_insensitive() -> None:
    registry = RegistryFile(
        extensions=[
            RegistryEntry(
                id="acme.jira",
                name="Acme Jira",
                description="Track Jira tickets",
                repo="https://github.com/acme/jira-ext",
                commit="abc123",
                tags=["issue-tracking", "atlassian"],
            ),
            RegistryEntry(
                id="acme.notes",
                name="Acme Notes",
                description="Simple note taking",
                repo="https://github.com/acme/notes-ext",
                commit="def456",
                tags=["notes"],
            ),
        ]
    )

    by_id = search(registry, "JIRA")
    assert [entry.id for entry in by_id] == ["acme.jira"]

    by_desc = search(registry, "note")
    assert [entry.id for entry in by_desc] == ["acme.notes"]

    by_tag = search(registry, "ISSUE-TRACKING")
    assert [entry.id for entry in by_tag] == ["acme.jira"]
