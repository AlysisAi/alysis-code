from __future__ import annotations

import json
from importlib import resources

from .models import RegistryEntry, RegistryFile, normalize_extension_id


def load_registry() -> RegistryFile:
    try:
        text = (
            resources.files("alysis_code.extensions")
            .joinpath("registry.json")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError as e:
        raise RuntimeError("Bundled extensions registry is missing.") from e

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError("Bundled extensions registry is not valid JSON.") from e

    if not isinstance(raw, dict):
        raise RuntimeError("Bundled extensions registry must be a JSON object.")
    return RegistryFile.model_validate(raw)


def find_by_id(registry: RegistryFile, ext_id: str) -> RegistryEntry | None:
    target = normalize_extension_id(ext_id)
    for entry in registry.extensions:
        if normalize_extension_id(entry.id) == target:
            return entry
    return None


def search(registry: RegistryFile, query: str) -> list[RegistryEntry]:
    needle = query.strip().casefold()
    if not needle:
        return list(registry.extensions)

    matches: list[RegistryEntry] = []
    for entry in registry.extensions:
        haystack = [
            entry.id,
            entry.name,
            entry.description,
            *entry.tags,
        ]
        if any(needle in str(field).casefold() for field in haystack):
            matches.append(entry)
    return matches
