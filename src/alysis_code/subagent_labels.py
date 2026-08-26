from __future__ import annotations

import re

from .safety.subagent_report import sanitize_subagent_report, subagent_report_evidence_text

SUBAGENT_TASK_LABEL_MAX_CHARS = 24

_LABEL_TOKEN_RE = re.compile(r"[\w.-]+", flags=re.UNICODE)


def subagent_task_label(task: object, *, requested_run_id: object = "") -> str:
    """Return a stable, display-safe child label without exposing the generated id."""
    caller_label = str(requested_run_id or "").strip()
    if caller_label:
        return caller_label

    screened = sanitize_subagent_report(str(task or "")).text
    evidence = subagent_report_evidence_text(screened)
    words = [
        token
        for token in _LABEL_TOKEN_RE.findall(evidence)
        if any(character.isalnum() for character in token)
    ]
    selected: list[str] = []
    for word in words:
        candidate = " ".join((*selected, word))
        if len(candidate) > SUBAGENT_TASK_LABEL_MAX_CHARS:
            break
        selected.append(word)
    if selected:
        return " ".join(selected)
    if words:
        return words[0][:SUBAGENT_TASK_LABEL_MAX_CHARS]
    return "task"


def subagent_identity(name: object, label: object) -> str:
    """Combine role and stable task identity for user-facing surfaces."""
    role = str(name or "subagent").strip() or "subagent"
    task_label = str(label or "").strip()
    return f"{role} \u00b7 {task_label}" if task_label else role


__all__ = [
    "SUBAGENT_TASK_LABEL_MAX_CHARS",
    "subagent_identity",
    "subagent_task_label",
]
