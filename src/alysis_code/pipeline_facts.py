"""Observed pipeline execution facts.

The completion-evidence gate judges *observed execution facts* — which program
ran and its per-stage exit status — instead of regexing the command string for
pipes or control-flow. The dominant masking hazard is a shell pipeline: in
``cmd1 | cmd2`` the shell reports ``cmd2``'s exit code, so ``pytest ... | tail``
exits ``0`` even when ``pytest`` failed. Bash exposes the truth via
``PIPESTATUS`` (per-stage exit codes of the most recent pipeline).

This module is pure and side-effect free. It provides:

* top-level, quote-aware splitting of a command into pipeline stages so the
  meaningful first-stage program can be identified;
* a wrapper that observes ``PIPESTATUS`` for the shell tool without enabling
  ``pipefail`` or otherwise changing the command's user-visible semantics
  (stdout, stderr, and the exit code are all preserved);
* extraction of the recorded per-stage status from captured stderr.

No regex/keyword matching is done on user or command text beyond the minimum
lexical scan needed to split pipeline stages for fact capture.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable

# Unique marker the capture wrapper prints to stderr, immediately after the
# command's own stderr, followed by the space-joined PIPESTATUS and a newline.
# The token is deliberately obscure so a real command emitting it is negligible;
# it is a fixed constant so the wrapper and the parser always agree.
PIPELINE_STATUS_SENTINEL = "__ALYSIS_PIPESTATUS_5b2e9f7c__ "


def _split_top_level(command: str) -> tuple[list[str], list[str]] | None:
    """Split ``command`` on top-level shell control operators.

    Returns ``(segments, separators)`` where ``separators[i]`` joins
    ``segments[i]`` and ``segments[i + 1]``; recognized operators are ``||``,
    ``&&``, ``;``, ``|`` and ``&``. Quoting (single/double) and backslash
    escaping are respected so operators inside quotes never split. Returns
    ``None`` when quotes are unbalanced (the command is not safely splittable).
    """
    text = str(command or "")
    segments: list[str] = []
    separators: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            segments.append(text[start:index])
            separators.append(text[index : index + 2])
            index += 2
            start = index
            continue
        if char in {";", "|", "&"}:
            segments.append(text[start:index])
            separators.append(char)
            index += 1
            start = index
            continue
        index += 1
    if quote is not None:
        return None
    segments.append(text[start:])
    return segments, separators


def command_has_top_level_pipe(command: str) -> bool:
    """True when ``command`` contains a top-level ``|`` (not ``||``)."""
    split = _split_top_level(command)
    if split is None:
        return False
    _segments, separators = split
    return "|" in separators


def split_top_level_pipeline(command: str) -> list[str] | None:
    """Return the stages of a *pure* top-level pipeline, else ``None``.

    A pure pipeline uses only ``|`` between stages (no ``&&``/``||``/``;``/``&``).
    ``"pytest -x foo | tail -40"`` -> ``["pytest -x foo", "tail -40"]``.
    """
    split = _split_top_level(command)
    if split is None:
        return None
    segments, separators = split
    if not separators or any(separator != "|" for separator in separators):
        return None
    stages = [segment.strip() for segment in segments]
    if any(not stage for stage in stages):
        return None
    return stages


def pipeline_meaningful_stage(command: str) -> str | None:
    """Return the meaningful first-stage program of ``command``'s last pipeline.

    ``PIPESTATUS`` reflects the *most recent* pipeline, so we take the trailing
    ``&&``/``;`` group and, when it is a pipeline, return its first stage:

    * ``"pytest -x foo | tail -40"`` -> ``"pytest -x foo"``
    * ``"cd repo && pytest | tail -40"`` -> ``"pytest"`` (first stage of the
      trailing pipeline, aligned with the captured PIPESTATUS)
    * ``"pytest -q || true"`` -> ``None`` (``||`` short-circuits; not a
      trustworthy first-stage mapping)
    * ``"make build && echo done"`` -> ``None`` (trailing group is not a pipeline)

    Returns ``None`` when there is no top-level pipeline whose first stage can be
    mapped to captured per-stage status.
    """
    split = _split_top_level(command)
    if split is None:
        return None
    segments, separators = split
    if not separators or "||" in separators:
        return None
    # Group pipe-joined segments; ``&&``/``;``/``&`` end a group.
    groups: list[list[str]] = []
    current = [segments[0]]
    for separator, segment in zip(separators, segments[1:], strict=True):
        if separator == "|":
            current.append(segment)
        else:
            groups.append(current)
            current = [segment]
    groups.append(current)
    last = groups[-1]
    if len(last) < 2:
        return None
    first_stage = last[0].strip()
    return first_stage or None


def build_pipeline_status_capture_command(command: str) -> str:
    """Wrap ``command`` so the shell records ``PIPESTATUS`` on stderr.

    The returned command is observation-only: ``pipefail`` is never enabled, and
    stdout, stderr, and the exit code are preserved byte-for-byte (the sentinel
    line is stripped by :func:`extract_pipeline_status`). When ``bash`` is
    available the command runs under it and its per-stage ``PIPESTATUS`` is
    printed after the command's own stderr; otherwise the command runs unchanged
    under ``sh`` and no status is recorded (the classifier then falls back to a
    single unpiped re-execution). ``PIPESTATUS`` is captured *before* any other
    command touches it, and the wrapper re-``exit``s with the pipeline's real
    final status so the exit code is identical to running the command directly.
    """
    inner = (
        f"{command}\n"
        f'__sy_ps="${{PIPESTATUS[*]}}"\n'
        f"printf '{PIPELINE_STATUS_SENTINEL}%s\\n' \"$__sy_ps\" >&2\n"
        f'exit "${{__sy_ps##* }}"'
    )
    return (
        "if command -v bash >/dev/null 2>&1; then "
        f"bash -c {shlex.quote(inner)}; "
        f"else sh -c {shlex.quote(command)}; fi"
    )


def resolve_pipeline_stage_status(
    command: str,
    current_status: list[int] | None,
    *,
    reexec: Callable[[str], int | None] | None = None,
) -> list[int] | None:
    """Return observed per-stage status, obtaining ground truth if needed.

    When ``current_status`` is already known it is returned unchanged. Otherwise,
    for a pipeline whose status could not be captured, the meaningful first stage
    is re-run unpiped **exactly once** (via ``reexec``) to obtain its true exit
    code, yielding a one-element status list. The re-execution is bounded to a
    single attempt (``reexec`` is called at most once) and is never retried; it
    obtains ground truth, it is not a way to re-run a failing test until it
    passes. Returns ``None`` when no ground truth is available.
    """
    if current_status:
        return list(current_status)
    if reexec is None:
        return None
    first_stage = pipeline_meaningful_stage(command)
    if first_stage is None:
        return None
    exit_code = reexec(first_stage)
    if exit_code is None:
        return None
    return [int(exit_code)]


def extract_pipeline_status(stderr: str) -> tuple[list[int] | None, str]:
    """Extract per-stage status from wrapped stderr and strip the sentinel.

    Returns ``(stage_status, cleaned_stderr)``. ``stage_status`` is ``None`` when
    no sentinel is present (e.g. ``bash`` was unavailable and the command ran
    unchanged). ``cleaned_stderr`` is the command's original stderr with the
    sentinel and everything after it removed, byte-for-byte.
    """
    text = str(stderr or "")
    idx = text.rfind(PIPELINE_STATUS_SENTINEL)
    if idx == -1:
        return None, text
    cleaned = text[:idx]
    after = text[idx + len(PIPELINE_STATUS_SENTINEL) :]
    line = after.split("\n", 1)[0].strip()
    tokens = line.split()
    if not tokens:
        return None, cleaned
    try:
        status = [int(token) for token in tokens]
    except ValueError:
        return None, cleaned
    return status, cleaned
