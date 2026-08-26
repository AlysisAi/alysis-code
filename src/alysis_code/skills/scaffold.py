from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from time import strftime

from .paths import (
    global_managed_skill_root,
    normalize_project_skill_family,
    project_managed_skill_root,
    project_skill_root_for_family,
    skill_bundle_dir_name,
)
from .state import (
    ManagedSkillRecord,
    load_global_skill_state,
    load_project_skill_state,
    save_global_skill_state,
    save_project_skill_state,
    with_managed_install,
)
from .transactions import commit_managed_bundle_update


@dataclass(frozen=True)
class SkillScaffoldResult:
    bundle_path: Path
    managed: bool
    scope: str
    family: str


def scaffold_skill_bundle(
    *,
    name: str,
    description: str = "",
    workspace_root: Path,
    project: bool = True,
    family: str = "native",
    force: bool = False,
    user_config_dir: Path | None = None,
) -> SkillScaffoldResult:
    normalized_family = normalize_project_skill_family(family) if project else "native"
    bundle_dir = skill_bundle_dir_name(name)
    if project:
        root = (
            project_managed_skill_root(workspace_root)
            if normalized_family == "native"
            else project_skill_root_for_family(workspace_root, family=normalized_family)
        )
    else:
        root = global_managed_skill_root(user_config_dir=user_config_dir)
    bundle_path = root / bundle_dir
    rendered_description = description.strip() or f"Describe when and why to use the {name} skill."

    managed = normalized_family == "native"
    if managed:
        installed_at = strftime("%Y-%m-%dT%H:%M:%SZ")
        record = ManagedSkillRecord(
            name=name,
            bundle_dir=bundle_dir,
            scope=("project" if project else "user"),
            source_kind="scaffold",
            source=str(bundle_path),
            installed_at=installed_at,
        )
        if project:
            state = with_managed_install(load_project_skill_state(workspace_root), record)
            commit_managed_bundle_update(
                target_path=bundle_path,
                force=force,
                stage_bundle=lambda staged: _write_scaffold_bundle(
                    staged,
                    name=name,
                    description=rendered_description,
                ),
                persist_state=lambda: save_project_skill_state(workspace_root, state),
            )
        else:
            state = with_managed_install(
                load_global_skill_state(user_config_dir=user_config_dir),
                record,
            )
            commit_managed_bundle_update(
                target_path=bundle_path,
                force=force,
                stage_bundle=lambda staged: _write_scaffold_bundle(
                    staged,
                    name=name,
                    description=rendered_description,
                ),
                persist_state=lambda: save_global_skill_state(
                    state,
                    user_config_dir=user_config_dir,
                ),
            )
    else:
        if bundle_path.exists():
            if not force:
                raise RuntimeError(f"Skill bundle already exists: {bundle_path}")
            shutil.rmtree(bundle_path)
        _write_scaffold_bundle(
            bundle_path,
            name=name,
            description=rendered_description,
        )
    return SkillScaffoldResult(
        bundle_path=bundle_path,
        managed=managed,
        scope=("project" if project else "user"),
        family=normalized_family,
    )


def _write_scaffold_bundle(bundle_path: Path, *, name: str, description: str) -> None:
    bundle_path.mkdir(parents=True, exist_ok=True)
    for child in ("references", "scripts", "assets"):
        (bundle_path / child).mkdir(parents=True, exist_ok=True)
    (bundle_path / "SKILL.md").write_text(
        _skill_template(name=name, description=description),
        encoding="utf-8",
    )


def _skill_template(*, name: str, description: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        "# Purpose\n"
        "- Explain when this skill should be used.\n"
        "- Call out the strongest signals that the skill applies.\n\n"
        "# Workflow\n"
        "- Describe the preferred steps the agent should follow.\n"
        "- Keep the guidance specific, short, and actionable.\n\n"
        "# Bundle Notes\n"
        "- Put deeper references in `references/`.\n"
        "- Put runnable helpers in `scripts/`.\n"
        "- Put templates or supporting files in `assets/`.\n"
    )
