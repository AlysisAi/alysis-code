"""Structured rendering for the Forge planner's plan-update aside.

While Forge mode chats with the planner, the TUI captures the turn's console
output (the ``│ ``-prefixed meta/warning bar lines that ``_print_forge_meta``
and ``_print_forge_warning_messages`` emit) and shows it beneath the reply as
a collapsible aside. This module turns that captured text back into logical
notes and renders them as a structured block instead of a flat dim dump:

* the canonical status line becomes an honest headline (``plan updated`` /
  ``plan unchanged`` / ``planner error``) instead of the first note,
* notes about the same task are grouped under one bright title (the single
  biggest source of the old wall-of-text was the task title repeated in
  every warning),
* planner warnings carry an amber ``⚠`` mark, info notes a dim ``·``, and the
  ``▸``/``▾`` chevrons take the Forge violet so the aside reads as Forge UI.

The note grammar matched here is produced by this repo's own emit sites
(``forge_helpers`` / ``plan_assistant`` / ``task_readiness`` /
``plan_reconciliation``); a source-contract test keeps the literals in sync.
Unrecognized notes degrade gracefully to plain dim bullets — parsing can only
ever *improve* the presentation, never lose a note.

Every rendered row is at most ``width`` columns (the transcript's cursor-pin
scroll math depends on logical rows matching screen rows).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Style classes (declared in ``app._STYLE``).
_S_MARK = "class:tui.planmeta.mark"
_S_HEAD = "class:tui.planmeta.head"
_S_DIM = "class:tui.planmeta.dim"
_S_SUBJECT = "class:tui.planmeta.subject"
_S_TAG = "class:tui.planmeta.tag"
_S_WARN = "class:tui.planmeta.warn"
_S_HINT = "class:tui.planmeta.hint"

# The bar prefix ``_forge_bar_text`` puts in front of every console meta /
# warning line (the rail marks where each logical note starts in a capture).
_BAR = "│"

# Warning-group labels used on the planner turn path (``emit_warning_group``
# call sites in ``forge_helpers``).
_WARN_LABELS = ("Planner", "Plan reconciliation")

# Canonical status lines the planner turn controller emits, matched by prefix
# so a small wording tweak degrades to a generic note rather than a wrong
# headline. The matching note folds into the headline instead of repeating —
# except the "ignored" line, whose tail carries the actionable reason and is
# kept as an info note (minus the "; plan unchanged." suffix the headline
# already states).
_STATUS_HEADLINES = (
    ("Applied planner update", "plan updated", False),
    ("Planner update contained no applicable changes", "plan unchanged", False),
    ("Planner update ignored", "plan unchanged", True),
)
_ERROR_NOTE_PREFIX = "Final planner error"
_DEFAULT_HEADLINE = "planner notes"
_ERROR_HEADLINE = "planner error"

# Subject extraction: pull the task a note talks about out of the note text so
# repeated titles collapse into one group header. Subject captures are LAZY
# (``.+?``) so the FIRST ``': `` boundary closes the title: the emit sites
# write ``'<title>': `` with the quote-colon immediately after the title,
# while everything AFTER that boundary is verbatim model output (echoed
# dependency ids, invalid path entries) that can itself contain quotes and
# colons — a greedy capture would let such junk swallow the subject and the
# actionable detail. Titles with plain apostrophes still parse whole (an
# apostrophe alone is not a boundary); the pathological title containing a
# literal ``': `` truncates at its first boundary — still grouped, with the
# tail kept in the detail, never lost.
_DROPPED_RES = (
    re.compile(
        r"^Dropped (?P<what>[a-z -]+) dependencies for (?P<target>.+?) "
        r"'(?P<subject>.+?)':\s*(?P<rest>.*)$"
    ),
    re.compile(r"^Dropped (?P<what>[a-z -]+) dependencies for (?P<target>.+?) '(?P<subject>.+)'$"),
)
_SUBJECT_PREFIX_RES = (
    (re.compile(r"^New task '(?P<subject>.+?)':\s*(?P<rest>.+)$"), "new"),
    (re.compile(r"^Updated task '(?P<subject>.+?)':\s*(?P<rest>.+)$"), "updated"),
    (re.compile(r"^Task '(?P<subject>.+?)':\s*(?P<rest>.+)$"), "task"),
    (re.compile(r"^Task (?P<subject>[\w.-]+):\s*(?P<rest>.+)$"), "task"),
)

_SUBJECT_GLYPHS = {"new": "+", "follow-up": "+", "updated": "~", "task": "○"}
_SUBJECT_TAGS = {
    "new": "new task",
    "follow-up": "follow-up task",
    "updated": "updated task",
    "task": "task",
}

# UI decoration on rendered rows (stripped again on clipboard copy).
_EXPAND_HINT = "  ·  click to expand"
_GLYPH_STRIP_RE = re.compile(r"^(\s*)[▸▾+~○·⚠] (.*)$")
# Shortest hint fragment worth stripping from a sweep released mid-hint: the
# distinctive "  ·  " lead-in (a rendered note never ends with it).
_HINT_MIN_FRAGMENT = 5


def strip_plan_meta_copy_chrome(text: str, *, starts_row: bool = True) -> str:
    """Return a rendered plan-meta row's text without its UI decoration — the
    chevron/bullet/warning glyph and the expand hint — keeping the indentation
    so a copied block preserves its note hierarchy.

    ``starts_row`` is False for a selection sweep that begins mid-row: its
    first character is note content, never a lead glyph, so only the hint
    suffix is stripped. A sweep released inside the hint drops the trailing
    hint fragment too (the fragment is still chrome). Wrap-continuation rows
    must NOT be routed here at all — their first character is content (see
    ``ROW_KIND_CONT``)."""
    text = str(text or "")
    for length in range(len(_EXPAND_HINT), _HINT_MIN_FRAGMENT - 1, -1):
        if text.endswith(_EXPAND_HINT[:length]):
            text = text[:-length]
            break
    if not starts_row:
        return text
    match = _GLYPH_STRIP_RE.match(text)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return text


@dataclass(frozen=True)
class PlanMetaNote:
    """One logical planner note, classified for display."""

    kind: str = "info"  # "info" | "warn"
    label: str = ""  # warning-group label ("Planner", "Plan reconciliation")
    subject: str = ""  # task title/id the note is about ("" = none)
    subject_kind: str = ""  # "new" | "updated" | "follow-up" | "task"
    detail: str = ""  # note text with the label + subject phrase folded out


@dataclass
class _SubjectGroup:
    subject: str
    subject_kind: str
    details: list[PlanMetaNote] = field(default_factory=list)


@dataclass
class PlanMetaView:
    headline: str
    notes: list[PlanMetaNote]
    entries: list[tuple[str, PlanMetaNote | _SubjectGroup]]


def _is_rule(line: str) -> bool:
    return len(line) >= 3 and not set(line) - set("─-–—═ ")


def _logical_lines(text: str) -> list[str]:
    """Reassemble logical notes from captured console text.

    Bar-prefixed lines start a note; bare lines are wrap continuations from a
    narrow capture console and re-join the note above them. Text with no bar
    rail at all (already-clean note lines) keeps one note per line.
    """
    raw = [line.strip() for line in str(text or "").split("\n")]
    raw = [line for line in raw if line and not _is_rule(line)]
    has_bar = any(line.startswith(_BAR) for line in raw)
    notes: list[str] = []
    for line in raw:
        if not has_bar:
            notes.append(line)
        elif line.startswith(_BAR):
            content = line[len(_BAR) :].strip()
            if not _is_rule(content):  # a bar-prefixed rule is still chrome
                notes.append(content)
        elif notes:
            notes[-1] = f"{notes[-1]} {line}"
        else:
            notes.append(line)
    return [note for note in (" ".join(n.split()) for n in notes) if note]


def _dropped_target_kind(target: str) -> str:
    """Map a dropped-dependencies target phrase to a subject kind — e.g.
    ``new task`` / ``planner tasks_add`` → new, ``synthesized follow-up task``
    → follow-up, ``planner tasks_update`` → updated."""
    lowered = target.lower()
    if "follow-up" in lowered:
        return "follow-up"
    if "new task" in lowered or "tasks_add" in lowered:
        return "new"
    if "updated task" in lowered or "tasks_update" in lowered:
        return "updated"
    return "task"


def _classify(text: str) -> PlanMetaNote:
    kind, label, body = "info", "", text
    for warn_label in _WARN_LABELS:
        prefix = f"{warn_label}: "
        if body.startswith(prefix):
            kind, label, body = "warn", warn_label, body[len(prefix) :].strip()
            break
    subject = subject_kind = ""
    detail = body
    match = next((m for pattern in _DROPPED_RES if (m := pattern.match(body))), None)
    if match:
        subject = match.group("subject")
        subject_kind = _dropped_target_kind(match.group("target"))
        what = match.group("what").strip()
        rest = (match.groupdict().get("rest") or "").strip()
        detail = f"dropped {what} dependencies"
        if rest:
            detail = f"{detail}: {rest}"
    else:
        for pattern, pat_kind in _SUBJECT_PREFIX_RES:
            match = pattern.match(body)
            if match:
                subject = match.group("subject")
                subject_kind = pat_kind
                detail = match.group("rest").strip()
                break
    return PlanMetaNote(
        kind=kind, label=label, subject=subject, subject_kind=subject_kind, detail=detail
    )


def parse_plan_meta(text: str) -> PlanMetaView:
    """Parse captured planner console text into a headline + grouped notes."""
    headline = ""
    notes: list[PlanMetaNote] = []
    for line in _logical_lines(text):
        if not headline:
            matched = next(
                (
                    (title, keep_reason)
                    for prefix, title, keep_reason in _STATUS_HEADLINES
                    if line.startswith(prefix)
                ),
                None,
            )
            if matched is not None:
                headline, keep_reason = matched
                if keep_reason:
                    # Keep the actionable tail (e.g. WHY the update was
                    # ignored); the headline already says "plan unchanged".
                    reason = line.removesuffix("; plan unchanged.").strip()
                    if reason:
                        notes.append(_classify(reason))
                continue  # the status line IS the headline; don't repeat it
        notes.append(_classify(line))
    if not headline:
        error_note = any(
            note.kind == "warn" and note.detail.startswith(_ERROR_NOTE_PREFIX) for note in notes
        )
        headline = _ERROR_HEADLINE if error_note else _DEFAULT_HEADLINE

    entries: list[tuple[str, PlanMetaNote | _SubjectGroup]] = []
    groups: dict[str, _SubjectGroup] = {}
    for note in notes:
        if not note.subject:
            entries.append(("note", note))
            continue
        group = groups.get(note.subject)
        if group is None:
            group = _SubjectGroup(subject=note.subject, subject_kind=note.subject_kind)
            groups[note.subject] = group
            entries.append(("group", group))
        elif group.subject_kind in {"", "task"} and note.subject_kind not in {"", "task"}:
            # A later note names the touched task more precisely (new/updated).
            group.subject_kind = note.subject_kind
        group.details.append(note)
    return PlanMetaView(headline=headline, notes=notes, entries=entries)


# ------------------------------------------------------------------ rendering


def _iter_words(segments: list[tuple[str, str]]):
    for style, text in segments:
        for word in text.split(" "):
            if word:
                yield style, word


def _wrap_segments(
    lead: list[tuple[str, str]],
    body: list[tuple[str, str]],
    width: int,
    *,
    cont_indent: int,
) -> list[list[tuple[str, str]]]:
    """Flow styled ``body`` words after the fixed ``lead``, hanging-indenting
    continuation rows. Every produced row is at most ``width`` columns."""
    width = max(1, int(width))
    cont_indent = max(0, min(int(cont_indent), width - 8)) if width > 8 else 0
    rows: list[list[tuple[str, str]]] = []
    # Clip the lead too, so the row-width contract holds even for a lead wider
    # than a pathologically narrow panel.
    cur: list[tuple[str, str]] = _fit_segments([(s, t) for s, t in lead if t], width)
    used = sum(len(text) for _style, text in cur)
    has_word = False

    def push(style: str, text: str) -> None:
        nonlocal used
        if cur and cur[-1][0] == style:
            cur[-1] = (style, cur[-1][1] + text)
        else:
            cur.append((style, text))
        used += len(text)

    def break_row() -> None:
        nonlocal cur, used, has_word
        rows.append(cur)
        cur = [(_S_DIM, " " * cont_indent)] if cont_indent else []
        used = cont_indent
        has_word = False

    for style, word in _iter_words(body):
        while word:
            room = width - used - (1 if has_word else 0)
            if len(word) <= room:
                if has_word:
                    push(style, " ")
                push(style, word)
                has_word = True
                word = ""
            elif not has_word and room > 0:
                push(style, word[:room])  # hard-break a word longer than the row
                word = word[room:]
                break_row()
            else:
                break_row()
    rows.append(cur)
    return rows


def _fit_segments(segments: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    """Clip a single styled row to ``width`` columns, ellipsizing the cut."""
    out: list[tuple[str, str]] = []
    used = 0
    for style, text in segments:
        if not text:
            continue
        room = width - used
        if room <= 0:
            break
        if len(text) <= room:
            out.append((style, text))
            used += len(text)
        else:
            clipped = text[: room - 1].rstrip() + "…" if room >= 2 else "…"
            out.append((style, clipped[:room]))
            used += len(clipped)
            break
    return out


def _summary_counts(view: PlanMetaView) -> tuple[int, int, int]:
    new_n = sum(
        1
        for kind, entry in view.entries
        if kind == "group" and entry.subject_kind in {"new", "follow-up"}
    )
    updated_n = sum(
        1 for kind, entry in view.entries if kind == "group" and entry.subject_kind == "updated"
    )
    warn_n = sum(1 for note in view.notes if note.kind == "warn")
    return new_n, updated_n, warn_n


def _collapsed_row(view: PlanMetaView, width: int) -> list[tuple[str, str]]:
    n = len(view.notes)
    if n == 0:
        return _fit_segments([(_S_MARK, "▸ "), (_S_HEAD, view.headline)], width)
    only = view.notes[0]
    if view.headline == _DEFAULT_HEADLINE and n == 1 and only.kind == "info" and not only.subject:
        # A single free-form note (e.g. "planner unavailable"): show it verbatim,
        # exactly like the old aside did — a synthetic headline would hide it.
        # When the note is clipped, reserve room for the expand hint so the
        # full text stays reachable.
        segments = [(_S_MARK, "▸ "), (_S_DIM, only.detail)]
        if 2 + len(only.detail) <= width or width < len(_EXPAND_HINT) + 12:
            return _fit_segments(segments, width)
        row = _fit_segments(segments, width - len(_EXPAND_HINT))
        row.append((_S_HINT, _EXPAND_HINT))
        return row
    new_n, updated_n, warn_n = _summary_counts(view)
    # (priority, segment): lowest priority sheds first when the panel is narrow.
    candidates: list[tuple[int, tuple[str, str]]] = [
        (100, (_S_MARK, "▸ ")),
        (90, (_S_HEAD, view.headline)),
        (60, (_S_DIM, f" · {n} note{'s' if n != 1 else ''}")),
    ]
    if new_n:
        candidates.append((50, (_S_DIM, f" · {new_n} new task{'s' if new_n != 1 else ''}")))
    if updated_n:
        candidates.append((40, (_S_DIM, f" · {updated_n} updated")))
    if warn_n:
        candidates.append((70, (_S_WARN, f" · {warn_n} warning{'s' if warn_n != 1 else ''}")))
    # The expand affordance outlives the task-count chips on a narrow panel —
    # discoverability beats detail.
    candidates.append((55, (_S_HINT, _EXPAND_HINT)))
    while (
        sum(len(text) for _p, (_s, text) in candidates) > width
        and min(p for p, _seg in candidates) < 90
    ):
        drop = min(range(len(candidates)), key=lambda i: candidates[i][0])
        candidates.pop(drop)
    return _fit_segments([seg for _p, seg in candidates], width)


def _note_body(note: PlanMetaNote) -> list[tuple[str, str]]:
    body: list[tuple[str, str]] = []
    if note.kind == "warn" and note.label and not note.subject:
        body.append((_S_TAG, f"{note.label} · "))
    body.append((_S_DIM, note.detail))
    return body


def _note_lead(note: PlanMetaNote, *, indent: str) -> list[tuple[str, str]]:
    if note.kind == "warn":
        return [(_S_DIM, indent), (_S_WARN, "⚠ ")]
    return [(_S_DIM, f"{indent}· ")]


# Per-row kinds for ``plan_meta_rows_with_kinds``: a "planmeta" row starts a
# note (its first glyph is UI chrome the copy path strips), a "planmetacont"
# row is a wrap continuation (its first character is note CONTENT — stripping
# a leading '+'/'~'/'·' there would silently delete it from copies).
ROW_KIND_LEAD = "planmeta"
ROW_KIND_CONT = "planmetacont"
PLAN_META_ROW_ROLES = frozenset({ROW_KIND_LEAD, ROW_KIND_CONT})


def plan_meta_rows_with_kinds(
    text: str, width: int, *, expanded: bool
) -> list[tuple[list[tuple[str, str]], str]]:
    """Render the plan-update aside as ``(row, kind)`` pairs. Collapsed: one
    summary row. Expanded: a header plus grouped, styled notes. Rows never
    exceed ``width`` columns."""
    width = max(20, int(width))
    view = parse_plan_meta(text)
    if not view.notes and view.headline == _DEFAULT_HEADLINE:
        return []
    if not expanded:
        return [(_collapsed_row(view, width), ROW_KIND_LEAD)]
    n = len(view.notes)
    header = [(_S_MARK, "▾ "), (_S_HEAD, view.headline)]
    if n:
        header.append((_S_DIM, f" · {n} note{'s' if n != 1 else ''}"))
    rows: list[tuple[list[tuple[str, str]], str]] = [(_fit_segments(header, width), ROW_KIND_LEAD)]

    def _extend(lead: list[tuple[str, str]], body: list[tuple[str, str]], cont_indent: int) -> None:
        wrapped = _wrap_segments(lead, body, width, cont_indent=cont_indent)
        rows.append((wrapped[0], ROW_KIND_LEAD))
        rows.extend((row, ROW_KIND_CONT) for row in wrapped[1:])

    for kind, entry in view.entries:
        if kind == "note":
            _extend(_note_lead(entry, indent="  "), _note_body(entry), 4)
            continue
        glyph = _SUBJECT_GLYPHS.get(entry.subject_kind, "○")
        tag = _SUBJECT_TAGS.get(entry.subject_kind, "task")
        _extend(
            [(_S_MARK, f"  {glyph} ")],
            [(_S_TAG, f"{tag} · "), (_S_SUBJECT, entry.subject)],
            4,
        )
        for note in entry.details:
            _extend(_note_lead(note, indent="    "), [(_S_DIM, note.detail)], 6)
    return rows


def plan_meta_rows(text: str, width: int, *, expanded: bool) -> list[list[tuple[str, str]]]:
    """Render the plan-update aside (rows only — see ``plan_meta_rows_with_kinds``)."""
    return [row for row, _kind in plan_meta_rows_with_kinds(text, width, expanded=expanded)]
