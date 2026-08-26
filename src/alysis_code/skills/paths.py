from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from ..branding import canonical_user_config_dir

ProjectSkillFamily = Literal["native", "agents", "claude", "github"]

_PROJECT_FAMILY_ROOTS: dict[ProjectSkillFamily, tuple[str, ...]] = {
    "native": (".alysis_skills",),
    "agents": (".agents", "skills"),
    "claude": (".claude", "skills"),
    "github": (".github", "skills"),
}


def global_managed_skill_root(*, user_config_dir: Path | None = None) -> Path:
    base = (
        user_config_dir.expanduser().resolve() if user_config_dir else canonical_user_config_dir()
    )
    return base / "skills"


def global_skills_state_path(*, user_config_dir: Path | None = None) -> Path:
    base = (
        user_config_dir.expanduser().resolve() if user_config_dir else canonical_user_config_dir()
    )
    return base / "skills.json"


def project_managed_skill_root(workspace_root: Path) -> Path:
    return workspace_root.expanduser().resolve() / ".alysis_skills"


def project_skills_state_path(workspace_root: Path) -> Path:
    return workspace_root.expanduser().resolve() / ".alysis" / "skills.json"


def project_skill_root_for_family(workspace_root: Path, *, family: ProjectSkillFamily) -> Path:
    return workspace_root.expanduser().resolve().joinpath(*_PROJECT_FAMILY_ROOTS[family])


def normalize_project_skill_family(raw: str | None) -> ProjectSkillFamily:
    normalized = str(raw or "native").strip().lower()
    if normalized in _PROJECT_FAMILY_ROOTS:
        return normalized  # type: ignore[return-value]
    raise ValueError("Unsupported skill family. Expected one of: native, agents, claude, github.")


def skill_bundle_dir_name(raw_name: str) -> str:
    candidate = re.sub(r"[^a-z0-9._-]+", "-", str(raw_name or "").strip().casefold()).strip("-")
    if not candidate:
        raise ValueError("Skill name must contain at least one letter or digit.")
    return candidate
