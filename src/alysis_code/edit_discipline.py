"""Edit discipline: wholesale-rewrite detection, thrash detection, scratch files.

Three warn-only guardrails against three observed failure modes. None of them
blocks or rewrites a model action; each one states a fact the model could not
otherwise see and then gets out of the way.

*Wholesale rewrites.* A full-file write that reproduces a file's meaning but not
its bytes passes every semantic test and fails every byte-comparison one. The
observed case re-serialized HTML it was only meant to filter -- indentation
stripped, attributes reordered, ``<br>`` become ``<br/>``, ``&copy;`` become
``(c)`` -- so a checker asserting that untouched files were untouched failed on
files the task never asked it to change. The same disease shows up on patch
benchmarks as pure regressions: the target test passes, unrelated ones break.
Two signals catch it cheaply: what fraction of the original lines did not
survive, and whether the surviving difference is *only* serialization.

*Thrashing.* A run that keeps re-attempting one variation of one idea -- 45
material actions, 52 scratch files, no answer -- has stopped learning from its
own results. Counting near-identical failures per action *family* (a path with
its numeric tail removed, so ``analyze_final.py`` / ``analyze_final2.py`` /
``analyze_final3.py`` are one thing) turns that into a number worth reporting
back once.

*Scratch files.* The same run left its whole working set on disk. Naming them at
finalization costs nothing and is the difference between a tree-state verifier
passing and failing. This module only *identifies* them; deleting is a
deliberate non-goal here.

Stdlib only, no package imports: the agent pulls this in from the tool layer and
the turn controller, and the tests load it straight from this file path in a
bare interpreter.

Cost note: nothing here walks the workspace. The rewrite guard is handed the
pre-write content that the write tool has already read for its own diff preview,
so the guard adds no I/O at all; every other input is something the caller
already had in hand.
"""

from __future__ import annotations

import difflib
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Line-change fraction
# ---------------------------------------------------------------------------


def changed_line_fraction(old: str, new: str) -> float:
    """Fraction of lines that a rewrite did not carry over unchanged.

    The denominator is the longer of the two line counts, so both directions of
    size change are penalised: deleting half a file and doubling it are each a
    large fraction. The numerator is everything ``difflib`` could not place in a
    common block, which means a line that merely *moved* still counts as
    changed. That is the intent -- a reordering serializer is exactly the thing
    this signal exists to notice.

    Returns 0.0 for two empty inputs, and 1.0 when one side is empty and the
    other is not.
    """

    old_lines = old.splitlines()
    new_lines = new.splitlines()
    denominator = max(len(old_lines), len(new_lines))
    if denominator == 0:
        return 0.0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return (denominator - matched) / denominator


def changed_line_percent(old: str, new: str) -> int:
    """``changed_line_fraction`` as a whole percent, for display."""

    return int(round(changed_line_fraction(old, new) * 100))


# ---------------------------------------------------------------------------
# Serialization-only detection
# ---------------------------------------------------------------------------

_WHITESPACE_RUN = re.compile(r"\s+")

# A tag with a name and optional attributes. Deliberately not an HTML parser:
# this only has to recognise the shapes a re-serializer produces, and anything
# it fails to recognise is left byte-for-byte alone (a miss costs a warning that
# is not emitted, never a wrong claim about semantics).
_TAG = re.compile(r"<(/?)([A-Za-z][-A-Za-z0-9:]*)((?:\s+[^<>]*?)?)\s*(/?)>")
_ATTR = re.compile(
    r"""([-A-Za-z_:][-A-Za-z0-9_:.]*)   # name
        (?:\s*=\s*                       # optional value
            (?:"([^"]*)"|'([^']*)'|([^\s"'<>`]+))
        )?""",
    re.VERBOSE,
)

# Only entities that cannot be confused with markup. Normalising the structural
# five (&amp; &lt; &gt; &quot; &#39;) would let a document containing a literal
# escaped tag compare equal to one containing a real tag, which is a genuine
# semantic difference -- so they are deliberately left alone.
_NON_STRUCTURAL_ENTITIES: dict[str, str] = {
    "&copy;": "©",
    "&reg;": "®",
    "&trade;": "™",
    "&nbsp;": " ",
    "&mdash;": "—",
    "&ndash;": "–",
    "&hellip;": "…",
    "&laquo;": "«",
    "&raquo;": "»",
    "&deg;": "°",
}
_NUMERIC_ENTITY = re.compile(r"&#(?:[xX]([0-9A-Fa-f]+)|([0-9]+));")

# Above this, the markup pass is skipped and only whitespace normalisation
# applies. A re-serializer that touched a file this large is already going to
# trip the line-fraction signal, so the cheaper check loses nothing.
MAX_MARKUP_NORMALIZE_CHARS = 2_000_000


def _normalize_entities(text: str) -> str:
    for entity, replacement in _NON_STRUCTURAL_ENTITIES.items():
        if entity in text:
            text = text.replace(entity, replacement)

    def _numeric(match: re.Match[str]) -> str:
        raw_hex, raw_dec = match.group(1), match.group(2)
        try:
            code = int(raw_hex, 16) if raw_hex else int(raw_dec or "", 10)
        except ValueError:  # pragma: no cover - regex guarantees digits
            return match.group(0)
        # Below 160 lives the structural range (&#38; &#60; &#39; ...), left as-is
        # for the same reason as the named structural entities.
        if code < 160 or code > 0x10FFFF:
            return match.group(0)
        return chr(code)

    return _NUMERIC_ENTITY.sub(_numeric, text)


def _normalize_tag(match: re.Match[str]) -> str:
    closing, name, raw_attrs, self_closing = match.groups()
    attrs: list[str] = []
    for attr in _ATTR.finditer(raw_attrs or ""):
        attr_name = attr.group(1).lower()
        value = attr.group(2)
        if value is None:
            value = attr.group(3)
        if value is None:
            value = attr.group(4)
        if value is None:
            attrs.append(attr_name)
        else:
            attrs.append(f'{attr_name}="{value}"')
    # Sorted, so `<a href=".." class="..">` and `<a class=".." href="..">` agree.
    # Order is not semantic in HTML/XML attributes; a serializer routinely
    # changes it and nothing downstream should care.
    rendered = " ".join(sorted(attrs))
    body = f"{closing}{name.lower()}"
    if rendered:
        body = f"{body} {rendered}"
    # `<br>` and `<br/>` are the same element; the trailing slash is dropped so
    # the two spellings compare equal.
    del self_closing
    return f"<{body}>"


def normalize_for_serialization(text: str) -> str:
    """Strip the differences a re-serializer is allowed to introduce.

    Two layers. The first always runs: collapse every whitespace run to one
    space, strip each line, and drop blank lines -- so re-indentation and
    re-wrapping vanish. The second runs only when the text looks like markup:
    lowercase tag and attribute names, sort attributes, drop the trailing slash
    on self-closing tags, and decode non-structural character entities.

    Known limits, all in the safe direction (a missed normalisation suppresses a
    warning; it never invents one):

    * Not a parser. Tags inside comments, CDATA, ``<script>`` or ``<style>``
      bodies are normalised like any other tag, and malformed markup is left
      alone rather than repaired.
    * Whitespace collapsing is content-blind, so it also flattens the interior
      of ``<pre>`` and of triple-quoted strings in source files. For the
      *serialization-only* question that is the desired reading -- but it means
      a change that only altered whitespace inside a ``<pre>`` block is reported
      as serialization-only even though it is visible.
    * Attribute *values* are compared verbatim: no URL, case, or numeric
      normalisation.
    * The structural entities are left encoded on purpose (see
      ``_NON_STRUCTURAL_ENTITIES``).
    """

    body = text
    if "<" in body and len(body) <= MAX_MARKUP_NORMALIZE_CHARS and _TAG.search(body):
        body = _TAG.sub(_normalize_tag, body)
        body = _normalize_entities(body)
    lines = []
    for line in body.splitlines():
        collapsed = _WHITESPACE_RUN.sub(" ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


def is_serialization_only(old: str, new: str) -> bool:
    """True when the bytes changed but nothing survives normalisation.

    This is the filter-js signature exactly: a file whose meaning is identical
    and whose bytes are not.
    """

    if old == new:
        return False
    return normalize_for_serialization(old) == normalize_for_serialization(new)


# ---------------------------------------------------------------------------
# Wholesale-rewrite assessment
# ---------------------------------------------------------------------------

# The single model-visible string this guard introduces. Kept as one constant so
# it is greppable and cannot drift.
REWRITE_WARNING_TEMPLATE = (
    "warning: full-file rewrite of {path}: {pct}% of lines changed. "
    "If the task requires preserving formatting of untouched regions, "
    "prefer a targeted edit."
)

# Strictly greater-than: a write that changes exactly 80% of the lines does not
# warn.
REWRITE_LINE_FRACTION_THRESHOLD = 0.80


@dataclass(frozen=True)
class RewriteAssessment:
    """What a full-file write did to a file that already existed."""

    path: str
    changed_fraction: float
    changed_percent: int
    serialization_only: bool
    should_warn: bool

    def warning_text(self) -> str | None:
        if not self.should_warn:
            return None
        return REWRITE_WARNING_TEMPLATE.format(path=self.path, pct=self.changed_percent)


def assess_full_file_rewrite(*, path: str, original: str, updated: str) -> RewriteAssessment:
    """Score one overwrite of an existing file.

    Warns on either signal. A large line fraction says the write replaced the
    file rather than edited it; serialization-only says the write changed bytes
    it had no reason to change. The second matters even when the fraction is
    small, because a checker comparing bytes does not care how few lines moved.
    """

    fraction = changed_line_fraction(original, updated)
    serialization_only = is_serialization_only(original, updated)
    should_warn = original != updated and (
        fraction > REWRITE_LINE_FRACTION_THRESHOLD or serialization_only
    )
    return RewriteAssessment(
        path=str(path),
        changed_fraction=fraction,
        changed_percent=int(round(fraction * 100)),
        serialization_only=serialization_only,
        should_warn=should_warn,
    )


# How many already-warned paths to remember. Past this the oldest entry is
# dropped, which can at worst let one path warn a second time in a very long
# run -- the bound on memory is worth more than the bound on repeats.
MAX_TRACKED_REWRITE_PATHS = 512


class RewriteGuard:
    """Per-run, at-most-once-per-file rewrite warnings.

    Holds no file content. The caller passes the pre-write bytes it already
    read, so nothing here is cached between calls except the set of paths that
    have already been warned about.
    """

    def __init__(self, *, max_paths: int = MAX_TRACKED_REWRITE_PATHS) -> None:
        self.max_paths = max(1, int(max_paths))
        self._warned: dict[str, None] = {}
        self._lock = threading.Lock()

    def warn_for_write(self, *, path: str, original: str, updated: str) -> str | None:
        """The warning for this write, or None.

        Returns None for a first-time create (no original), for an unchanged
        write, for a change too small to be interesting, and for every write to
        a path already warned about in this run.
        """

        key = str(path)
        if not original:
            # Nothing existed to preserve; a create cannot be a rewrite.
            return None
        with self._lock:
            if key in self._warned:
                return None
        assessment = assess_full_file_rewrite(path=key, original=original, updated=updated)
        text = assessment.warning_text()
        if text is None:
            return None
        with self._lock:
            if key in self._warned:
                return None
            self._warned[key] = None
            while len(self._warned) > self.max_paths:
                self._warned.pop(next(iter(self._warned)), None)
        return text

    def warned_paths(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._warned)


# ---------------------------------------------------------------------------
# Action families
# ---------------------------------------------------------------------------

# A stem ending in digits, where something non-numeric precedes them. Requiring
# a preceding non-digit keeps `2024` intact instead of collapsing it to nothing.
_NUMERIC_TAIL = re.compile(r"^(?P<base>.*[^0-9])(?P<digits>[0-9]+)$")
_PATHISH = re.compile(r"[/\\.]")


def _strip_numeric_tail(stem: str) -> str:
    match = _NUMERIC_TAIL.match(stem)
    if match is None:
        return stem
    base = match.group("base")
    # `foo_2` and `foo-2` are variations of `foo`, not of `foo_`.
    return base.rstrip("_-") or base


def path_family(path: str) -> str:
    """A path with its extension and numeric tail removed.

    ``analyze_final.py``, ``analyze_final2.py`` and ``analyze_final3.py`` all
    reduce to ``analyze_final``: three names for one idea the run keeps
    re-attempting. Directory structure is kept, so the same basename in two
    different directories stays two families.
    """

    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    text = text.rstrip("/")
    head, _, tail = text.rpartition("/")
    stem, dot, _ext = tail.rpartition(".")
    if not dot:
        stem = tail
    stem = _strip_numeric_tail(stem)
    return f"{head}/{stem}" if head else stem


def command_family(command: str) -> str:
    """A command line with whitespace collapsed and path arguments reduced.

    ``python analyze_final2.py --check`` and ``python analyze_final3.py --check``
    are the same attempt, so the tokens that look like paths go through
    ``path_family`` and everything else is kept verbatim.
    """

    tokens = str(command or "").split()
    if not tokens:
        return ""
    reduced = [path_family(token) if _PATHISH.search(token) else token for token in tokens]
    return " ".join(token for token in reduced if token)


def action_family(tool: str, target: str) -> str:
    """The family key for one mutation attempt.

    Keyed by tool as well as target so a file being written and the same file
    being run are not conflated.
    """

    name = str(tool or "").strip() or "action"
    text = str(target or "").strip()
    if not text:
        return name
    reduced = command_family(text) if " " in text else path_family(text)
    return f"{name}:{reduced}" if reduced else name


# ---------------------------------------------------------------------------
# Repetition counter
# ---------------------------------------------------------------------------

# The single model-visible string this guard introduces.
THRASH_NOTICE_TEMPLATE = (
    "Progress check: {n} similar attempts on {family} without a passing result. "
    "Synthesize what you know into a final answer or a concrete blocker report now."
)

THRASH_REPETITION_THRESHOLD = 8
MAX_THRASH_NOTICES_PER_RUN = 2
# Families past this are dropped oldest-first. A run cycling through hundreds of
# distinct targets is not thrashing on any one of them, so the eviction cannot
# hide the pattern this counts.
MAX_TRACKED_FAMILIES = 256


class ThrashCounter:
    """Per-run count of failed, near-identical mutation attempts.

    Success is the reset: a family that produces a passing result starts over,
    because the notice claims ``without a passing result`` and must be true when
    it fires. Fires at most once per family and at most
    ``MAX_THRASH_NOTICES_PER_RUN`` times per run -- past that the model has been
    told, and repeating it is the same failure the guard is warning about.
    """

    def __init__(
        self,
        *,
        threshold: int = THRASH_REPETITION_THRESHOLD,
        max_notices: int = MAX_THRASH_NOTICES_PER_RUN,
        max_families: int = MAX_TRACKED_FAMILIES,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.max_notices = max(0, int(max_notices))
        self.max_families = max(1, int(max_families))
        self._failures: dict[str, int] = {}
        self._notified: set[str] = set()
        self._notices_sent = 0
        self._lock = threading.Lock()

    @property
    def notices_sent(self) -> int:
        with self._lock:
            return self._notices_sent

    def failure_count(self, family: str) -> int:
        with self._lock:
            return self._failures.get(str(family), 0)

    def record(self, *, tool: str, target: str, failed: bool) -> str | None:
        """Record one mutation attempt; return a notice when it should fire."""

        family = action_family(tool, target)
        if not family:
            return None
        with self._lock:
            if not failed:
                self._failures.pop(family, None)
                return None
            count = self._failures.pop(family, 0) + 1
            # Re-inserted at the end, so the family just touched is never the
            # eviction candidate and the dict cannot grow past its cap.
            self._failures[family] = count
            while len(self._failures) > self.max_families:
                self._failures.pop(next(iter(self._failures)), None)
            if count < self.threshold:
                return None
            if family in self._notified or self._notices_sent >= self.max_notices:
                return None
            self._notified.add(family)
            self._notices_sent += 1
        return THRASH_NOTICE_TEMPLATE.format(n=count, family=family)


# ---------------------------------------------------------------------------
# Scratch files
# ---------------------------------------------------------------------------

# The one line added to the local runtime finalization summary. That summary is
# not model-visible (it is a runtime artifact printed and logged locally), so
# this is not a model-facing string -- it is kept here beside the others so the
# whole vocabulary of this PR lives in one file.
SCRATCH_FILES_SUMMARY_LINE = "Scratch files left in tree: {list}"

# `analyze_`, `analysis3_`, `scratch-`, `tmp_` ... at a word boundary. The
# optional digits are what let `analysis3_output.txt` match the same rule as
# `analyze_final.py`.
_SCRATCH_NAME = re.compile(
    r"(?:^|[_\-.])(?:analyze|analysis|scratch|tmp|temp)[0-9]*[_\-]",
    re.IGNORECASE,
)

MAX_TRACKED_CREATED_PATHS = 2048


def _split_numbered(name: str) -> tuple[str, str] | None:
    """``('analyze_final', '.py')`` for ``analyze_final2.py``, else None."""

    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    match = _NUMERIC_TAIL.match(stem)
    if match is None:
        return None
    base = match.group("base")
    if not base or base.rstrip("_-") == "":
        return None
    suffix = f".{ext}" if ext else ""
    return base, suffix


def looks_like_scratch_name(name: str) -> bool:
    """True for names that announce themselves as working files."""

    return bool(_SCRATCH_NAME.search(str(name or "")))


def find_scratch_files(
    created_paths: Iterable[str],
    *,
    existing_paths: Iterable[str] = (),
    referenced_paths: Iterable[str] = (),
) -> tuple[str, ...]:
    """Files created this run that look like leftovers, in creation order.

    Two rules. A path is scratch when its name carries a numeric tail and a
    sibling without that tail exists (``analyze_final2.py`` beside
    ``analyze_final.py`` -- the second attempt at one thing), or when its name
    matches a scratch word.

    ``existing_paths`` widens the sibling search to files the run did not
    create; ``referenced_paths`` is a caller-supplied allowlist of paths that
    are part of the deliverable and must never be reported. Reference analysis
    itself is out of scope: deciding whether a file is referenced means reading
    the workspace, and these guards are not allowed to scan it.

    No deletion, here or anywhere: this reports, and the run decides.
    """

    created = [str(path).replace("\\", "/") for path in created_paths if str(path or "").strip()]
    keep = {str(path).replace("\\", "/") for path in referenced_paths}
    universe = set(created)
    universe.update(str(path).replace("\\", "/") for path in existing_paths)

    found: list[str] = []
    seen: set[str] = set()
    for path in created:
        if path in keep or path in seen:
            continue
        head, _, name = path.rpartition("/")
        scratch = looks_like_scratch_name(name)
        if not scratch:
            split = _split_numbered(name)
            if split is not None:
                base, suffix = split
                for sibling_stem in (base, base.rstrip("_-")):
                    sibling = f"{sibling_stem}{suffix}"
                    candidate = f"{head}/{sibling}" if head else sibling
                    if candidate != path and candidate in universe:
                        scratch = True
                        break
        if scratch:
            seen.add(path)
            found.append(path)
    return tuple(found)


def scratch_summary_line(paths: Sequence[str], *, limit: int = 10) -> str | None:
    """The one-line finalization-summary addition, or None when the tree is clean."""

    if not paths:
        return None
    shown = ", ".join(paths[:limit])
    if len(paths) > limit:
        shown += f", ... (+{len(paths) - limit} more)"
    return SCRATCH_FILES_SUMMARY_LINE.format(list=shown)


# ---------------------------------------------------------------------------
# Per-run state
# ---------------------------------------------------------------------------


class EditDisciplineState:
    """Every per-run edit-discipline counter, in one bounded, thread-safe object.

    One object because the tool layer and the turn controller each need part of
    it and neither should have to know about the other's. Bounded because a long
    run must not grow it without limit: at most
    ``MAX_TRACKED_REWRITE_PATHS`` warned paths, ``MAX_TRACKED_FAMILIES``
    families and ``MAX_TRACKED_CREATED_PATHS`` created paths, each evicting
    oldest-first. Thread-safe because tool calls can be dispatched
    concurrently.
    """

    def __init__(self, *, max_created_paths: int = MAX_TRACKED_CREATED_PATHS) -> None:
        self.rewrite_guard = RewriteGuard()
        self.thrash = ThrashCounter()
        self.max_created_paths = max(1, int(max_created_paths))
        self._created: dict[str, None] = {}
        self._pending_notices: list[str] = []
        self._lock = threading.Lock()

    # -- rewrite guard ----------------------------------------------------

    def warn_for_write(self, *, path: str, original: str, updated: str) -> str | None:
        return self.rewrite_guard.warn_for_write(path=path, original=original, updated=updated)

    # -- thrash guard -----------------------------------------------------

    def record_attempt(self, *, tool: str, target: str, failed: bool) -> str | None:
        """Record an attempt and queue any notice it triggers.

        The queue exists because the two halves run at different moments: an
        attempt is observed while tool results come back, and a notice can only
        be delivered when the next request's system messages are assembled.
        Queueing per run rather than per turn means a notice earned on the last
        tool call of a turn is still delivered on the next one instead of being
        silently dropped -- and dropped notices would be doubly wrong here,
        since the counter has already spent one of its two per-run slots.
        """

        notice = self.thrash.record(tool=tool, target=target, failed=failed)
        if notice is not None:
            with self._lock:
                self._pending_notices.append(notice)
        return notice

    def take_notice(self) -> str | None:
        """Pop the next queued notice, or None. Delivering is the caller's job."""

        with self._lock:
            if not self._pending_notices:
                return None
            return self._pending_notices.pop(0)

    # -- scratch files ----------------------------------------------------

    def record_created(self, path: str) -> None:
        key = str(path or "").strip().replace("\\", "/")
        if not key:
            return
        with self._lock:
            if key in self._created:
                return
            self._created[key] = None
            while len(self._created) > self.max_created_paths:
                self._created.pop(next(iter(self._created)), None)

    def created_paths(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._created)

    def scratch_files(self, *, referenced_paths: Iterable[str] = ()) -> tuple[str, ...]:
        return find_scratch_files(self.created_paths(), referenced_paths=referenced_paths)

    def scratch_summary_line(self) -> str | None:
        return scratch_summary_line(self.scratch_files())
