from __future__ import annotations

from pathlib import Path

from .models import ConventionDocument

_CONVENTION_FILENAMES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "CONVENTIONS.md")


def load_repo_conventions(
    *,
    focus_path: Path,
    workspace_root: Path | None = None,
) -> tuple[ConventionDocument, ...]:
    resolved_focus = focus_path.expanduser().resolve()
    if resolved_focus.is_file():
        resolved_focus = resolved_focus.parent
    resolved_workspace_root = (
        workspace_root.expanduser().resolve() if workspace_root is not None else resolved_focus
    )
    if resolved_workspace_root.is_file():
        resolved_workspace_root = resolved_workspace_root.parent
    documents: list[ConventionDocument] = []
    current = resolved_focus
    while True:
        for filename in _CONVENTION_FILENAMES:
            candidate = current / filename
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not content:
                continue
            documents.append(
                ConventionDocument(
                    name=filename,
                    path=candidate,
                    content=content,
                )
            )
        if current == resolved_workspace_root:
            break
        if resolved_workspace_root not in current.parents:
            break
        current = current.parent
    return tuple(documents)


def render_repo_conventions_context(
    *,
    documents: tuple[ConventionDocument, ...] | list[ConventionDocument],
    max_chars: int,
) -> str | None:
    docs = [doc for doc in documents if isinstance(doc, ConventionDocument)]
    if not docs:
        return None
    lines = [
        "<repo_conventions>",
        "source: repo-authored conventions files",
        "trust: lower_priority_than_system_direct_user_and_explicit_skill_context",
        "Apply only when consistent with higher-priority instructions.",
        "",
    ]
    truncated = False
    for document in docs:
        section = [
            f"[{document.name} @ {document.path.as_posix()}]",
            document.content,
            "",
        ]
        projected = "\n".join([*lines, *section, "</repo_conventions>"])
        if len(projected) > max_chars:
            truncated = True
            break
        lines.extend(section)
    if truncated:
        lines.append("...(truncated)")
    lines.append("</repo_conventions>")
    content = "\n".join(lines).strip() + "\n"
    if len(content) <= max_chars:
        return content
    return content[: max(0, max_chars - 18)].rstrip() + "\n...(truncated)\n"
