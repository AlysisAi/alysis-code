"""Blast-radius regression gate (verification protocol, step 6).

Steps 2-5 all ask "is the change correct?". This step asks the question the
benchmark failures actually turned on: *what else did the change break?* A patch
that is correct in itself but edits shared code can take out hundreds of existing
tests, and the agent never notices because everything it chose to run still
passes.

The protocol adds a scope the agent did not choose:

1. from the paths the change touches, select the tests likely affected -- the
   name-mirror test, tests that statically import the touched modules, and tests
   in/near the touched package;
2. that scope must be run on the CLEAN tree (no pre-existing file modified yet).
   Its failures are the baseline: they are not the patch's fault and are never
   attributed to it;
3. after the fix verifies, the same scope runs again. Failures present now but
   not in the baseline are regressions the change introduced;
4. regressions are repaired with the fix preserved -- and when a change breaks a
   large number of baseline tests, the approach itself is over-broad, so the
   directive escalates from "fix each failure" to "rewrite the patch narrowly";
5. regressions that survive the repair budget are stated in the summary. A known
   regression is never shipped silently.

Design invariants (identical in spirit to steps 2-5):

* Everything here is pure except ``build_repo_test_index``, the single bounded
  filesystem walk (mirroring ``reproduction_first.surviving_repro_artifacts`` and
  ``acceptance_contract``'s bounded probes). It never raises.
* The host never runs anything. It selects the scope, tells the agent the scope,
  and observes what the agent actually ran -- the same observational contract the
  rest of the verification protocol keeps.
* Baselines come only from runs that happened on the unpatched tree. No git state
  is ever mutated to reconstruct one (the step-3 rule), and a run made after
  product code changed is never graced into a baseline: the agent may already have
  finished its fix by then, and crediting that run would mask exactly the breakage
  this step exists to catch.
* Comparability is decided by what a run actually selected, not by command
  equality: a clean whole-suite run is a universal baseline, and a failure whose
  file the baseline never covered is reported ``unattributed`` rather than guessed
  either way.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from ..branding import env_get
from .regression_baseline import TestReport, node_id_file_path

# ---------------------------------------------------------------------------
# Kill-switch + policy (mirrors the evidence-v2 / regression / repro idiom)
# ---------------------------------------------------------------------------


def _blast_radius_gate_enabled(cfg: Any | None) -> bool:
    """Kill-switch for the blast-radius regression gate (step 6).

    ``ALYSIS_BLAST_RADIUS`` (off/0/false/no/disabled) wins over the config
    value; default is on. When off, scope runs are still captured for telemetry
    but the turn directives and the completion-gate policy revert to legacy.
    """
    env_value = env_get("ALYSIS_BLAST_RADIUS")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "blast_radius_gate_enabled", True))


#: Default ceiling on how many test files one scope may name. The scope is a
#: safety net, not a full suite run; past this the runtime cost stops paying for
#: itself and the nearest tiers already carry the signal.
DEFAULT_MAX_SCOPE_FILES = 40
#: Default wall-clock ceiling for one scope run. Exceeding it shrinks the scope
#: (nearest tests kept) for the next run -- it never disables the gate.
DEFAULT_SCOPE_SECONDS_CAP = 300.0
#: Default count of newly-broken baseline tests past which the change is treated
#: as over-broad: the patch is rewritten narrowly instead of patched up per test.
DEFAULT_OVER_BROAD_THRESHOLD = 20
#: A shrunk scope never drops below this; shrinking must not become skipping.
MIN_SCOPE_FILES = 1
#: Ceilings that keep a long turn's recorded state small.
MAX_SCOPE_RUNS = 40
MAX_LISTED_IDS = 12


@dataclass(frozen=True)
class BlastRadiusPolicy:
    """Resolved, clamped knobs for one turn."""

    max_scope_files: int = DEFAULT_MAX_SCOPE_FILES
    scope_seconds_cap: float = DEFAULT_SCOPE_SECONDS_CAP
    over_broad_threshold: int = DEFAULT_OVER_BROAD_THRESHOLD

    def as_payload(self) -> dict[str, Any]:
        return {
            "max_scope_files": self.max_scope_files,
            "scope_seconds_cap": self.scope_seconds_cap,
            "over_broad_threshold": self.over_broad_threshold,
        }


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed) or parsed <= 0:
        return fallback
    return parsed


def resolve_blast_radius_policy(cfg: Any | None) -> BlastRadiusPolicy:
    """Read the knobs off the config, falling back to the defaults on anything odd."""
    return BlastRadiusPolicy(
        max_scope_files=_positive_int(
            getattr(cfg, "blast_radius_max_scope_files", None), DEFAULT_MAX_SCOPE_FILES
        ),
        scope_seconds_cap=_positive_float(
            getattr(cfg, "blast_radius_scope_seconds_cap", None), DEFAULT_SCOPE_SECONDS_CAP
        ),
        over_broad_threshold=_positive_int(
            getattr(cfg, "blast_radius_over_broad_threshold", None),
            DEFAULT_OVER_BROAD_THRESHOLD,
        ),
    )


# ---------------------------------------------------------------------------
# Paths, languages, test-file conventions
# ---------------------------------------------------------------------------

_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        ".eggs",
        "site-packages",
        "dist",
        "build",
        "target",
        "vendor",
        "htmlcov",
        ".idea",
        ".vscode",
        ".alysis",
    }
)

_PY_EXTENSIONS = frozenset({".py"})
_JS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_GO_EXTENSIONS = frozenset({".go"})
#: Source roots stripped when synthesizing a fallback dotted module name for a
#: namespace package (one with no ``__init__.py`` to anchor the real name).
_SOURCE_ROOT_NAMES = frozenset({"src", "lib", "source", "app", "packages"})


class ScopeLanguage(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    GO = "go"


_EXTENSION_LANGUAGES = {
    **{ext: ScopeLanguage.PYTHON for ext in _PY_EXTENSIONS},
    **{ext: ScopeLanguage.JAVASCRIPT for ext in _JS_EXTENSIONS},
    **{ext: ScopeLanguage.GO for ext in _GO_EXTENSIONS},
}
#: Only the pytest/unittest family produces the per-test ids the step-3 parsers
#: read, so only a Python scope can be diffed test-by-test. Other languages still
#: get a scope (the advisory is useful) but never block the gate: a gate that
#: cannot read its own evidence must not pretend to have any.
_DIFFABLE_LANGUAGES = frozenset({ScopeLanguage.PYTHON})


def normalize_repo_path(path: str) -> str:
    """Repo-relative, forward-slashed, leading ``./`` and trailing ``/`` removed."""
    cleaned = str(path or "").strip().strip("`'\"").replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def path_language(path: str) -> ScopeLanguage | None:
    suffix = PurePosixPath(normalize_repo_path(path)).suffix.casefold()
    return _EXTENSION_LANGUAGES.get(suffix)


def is_test_file(path: str) -> bool:
    """True when a path follows a test-file naming convention of a known runner.

    Deliberately conventional rather than clever: ``test_x.py`` / ``x_test.py``
    for Python, ``x.test.ts`` / ``x.spec.js`` (and ``__tests__/``) for JS/TS, and
    ``x_test.go`` for Go. These are the shapes the runners themselves collect by.

    Kept to plain string operations: the index walk calls this once per file in the
    repo, and building a ``PurePosixPath`` per call is the difference between a
    tolerable index and a visible stall.
    """
    normalized = normalize_repo_path(path).casefold()
    if not normalized:
        return False
    head, _, name = normalized.rpartition("/")
    dot = name.rfind(".")
    if dot <= 0:
        return False
    suffix = name[dot:]
    if suffix in _PY_EXTENSIONS:
        return name.startswith("test_") or name.endswith("_test.py")
    if suffix in _JS_EXTENSIONS:
        if ".test." in name or ".spec." in name:
            return True
        return "__tests__" in head.split("/")
    if suffix in _GO_EXTENSIONS:
        return name.endswith("_test.go")
    return False


def _parent_dir(path: str) -> str:
    parent = PurePosixPath(normalize_repo_path(path)).parent.as_posix()
    return "" if parent == "." else parent


def _shared_prefix_length(left: str, right: str) -> int:
    left_parts = left.split("/")
    right_parts = right.split("/")
    shared = 0
    for a, b in zip(left_parts, right_parts, strict=False):
        if a != b:
            break
        shared += 1
    return shared


# ---------------------------------------------------------------------------
# Static import scan
# ---------------------------------------------------------------------------

_PY_FROM_IMPORT_RE = re.compile(r"^[ \t]*from[ \t]+([.\w]+)[ \t]+import[ \t]+(.+)$", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([.\w]+(?:[ \t]*,[ \t]*[.\w]+)*)", re.MULTILINE)
_PY_IMPORTED_NAME_RE = re.compile(r"[A-Za-z_]\w*")
_JS_IMPORT_RE = re.compile(
    r"""(?:from|require|import)[ \t]*\(?[ \t]*['"]([^'"\n]+)['"]""",
)


def extract_python_import_tokens(text: str) -> frozenset[str]:
    """Dotted module tokens a Python source text imports.

    ``from a.b import c, d`` yields ``a.b``, ``a.b.c`` and ``a.b.d`` so a test that
    imports a symbol *out of* the touched module still matches it. Relative imports
    (``from . import x``) carry no absolute name and are skipped rather than guessed
    at -- a wrong guess would put an unrelated test in the scope.
    """
    tokens: set[str] = set()
    body = str(text or "")
    for module, names in _PY_FROM_IMPORT_RE.findall(body):
        if module.startswith("."):
            continue
        tokens.add(module)
        head = names.split("#", 1)[0]
        for name in _PY_IMPORTED_NAME_RE.findall(head):
            if name not in {"as", "import"}:
                tokens.add(f"{module}.{name}")
    for group in _PY_IMPORT_RE.findall(body):
        for raw in group.split(","):
            module = raw.strip()
            if module and not module.startswith("."):
                tokens.add(module)
    return frozenset(tokens)


def extract_js_import_tokens(text: str) -> frozenset[str]:
    """Module specifiers a JS/TS source text imports (``import``/``require``)."""
    return frozenset(
        specifier.strip()
        for specifier in _JS_IMPORT_RE.findall(str(text or ""))
        if specifier.strip()
    )


def extract_import_tokens(path: str, text: str) -> frozenset[str]:
    language = path_language(path)
    if language == ScopeLanguage.PYTHON:
        return extract_python_import_tokens(text)
    if language == ScopeLanguage.JAVASCRIPT:
        return extract_js_import_tokens(text)
    return frozenset()


def python_module_names(path: str, package_dirs: Iterable[str] = ()) -> tuple[str, ...]:
    """Dotted names under which a Python file can be imported.

    The authoritative name comes from walking up while each ancestor directory is
    a package (holds ``__init__.py``), which is what makes a ``src/`` layout resolve
    to ``pkg.mod`` rather than ``src.pkg.mod``. Two fallbacks are added for
    namespace packages, which have no ``__init__.py`` to anchor the walk: the full
    path dotted, and the path with a leading source-root component stripped.
    """
    normalized = normalize_repo_path(path)
    pure = PurePosixPath(normalized)
    if pure.suffix.casefold() != ".py":
        return ()
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return ()
    packages = {normalize_repo_path(item) for item in package_dirs}
    dirs = parts[:-1]
    root_index = len(dirs)
    index = len(dirs)
    while index > 0 and "/".join(dirs[:index]) in packages:
        root_index = index - 1
        index -= 1

    candidates: list[list[str]] = [parts[root_index:], list(parts)]
    stripped = list(parts)
    while stripped and stripped[0] in _SOURCE_ROOT_NAMES:
        stripped = stripped[1:]
    if stripped:
        candidates.append(stripped)

    names: list[str] = []
    for candidate in candidates:
        if candidate and all(part.isidentifier() for part in candidate):
            names.append(".".join(candidate))
    return tuple(dict.fromkeys(names))


def python_import_matches(tokens: Iterable[str], module_names: Sequence[str]) -> bool:
    """True when any import token refers to one of ``module_names``.

    Equality and prefix matching are exact. The suffix fallback (an import token
    ending in ``.<name>``) is what catches a namespace-package layout whose real
    dotted root we could not anchor -- restricted to multi-component names, since a
    bare ``utils`` would otherwise match every ``anything.utils`` in the repo.
    """
    token_set = {str(token).strip() for token in tokens if str(token).strip()}
    if not token_set:
        return False
    for name in module_names:
        if not name:
            continue
        suffix_ok = "." in name
        for token in token_set:
            if token == name or token.startswith(f"{name}."):
                return True
            if suffix_ok and token.endswith(f".{name}"):
                return True
    return False


def js_import_matches(*, importer: str, tokens: Iterable[str], target: str) -> bool:
    """True when a JS/TS specifier resolves to ``target``.

    Relative specifiers are resolved against the importing file's directory, which
    is exact. Bare/aliased specifiers (``@/utils/foo``) fall back to a path-suffix
    match after dropping the alias component.
    """
    target_path = normalize_repo_path(target)
    if not target_path:
        return False
    target_stem = target_path.rsplit(".", 1)[0]
    importer_dir = _parent_dir(importer)
    for raw in tokens:
        specifier = str(raw or "").strip()
        if not specifier:
            continue
        if specifier.startswith("."):
            base = f"{importer_dir}/{specifier}" if importer_dir else specifier
            try:
                resolved = normalize_repo_path(PurePosixPath(base).as_posix())
            except ValueError:  # pragma: no cover - defensive
                continue
            resolved = _collapse_relative(resolved)
            if resolved and (resolved == target_stem or resolved == target_path):
                return True
            continue
        cleaned = specifier.lstrip("@~").lstrip("/")
        cleaned = cleaned.split("?", 1)[0]
        if not cleaned or "/" not in cleaned:
            continue
        stem = cleaned.rsplit(".", 1)[0] if "." in cleaned.rsplit("/", 1)[-1] else cleaned
        if target_stem.endswith(f"/{stem}") or target_stem == stem:
            return True
    return False


def _collapse_relative(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Repo index (the one filesystem-touching helper)
# ---------------------------------------------------------------------------

#: Bounds on the index walk. A repo larger than these is indexed partially and
#: says so (``truncated``) rather than silently selecting from half a repo.
MAX_INDEXED_FILES = 40_000
MAX_INDEXED_TEST_FILES = 4_000
MAX_IMPORT_SCANS = 1_500
MAX_IMPORT_SCAN_BYTES = 16_384


@dataclass(frozen=True)
class RepoTestIndex:
    """A bounded snapshot of the repo's test surface, taken once per turn."""

    test_files: tuple[str, ...] = ()
    imports: Mapping[str, frozenset[str]] = field(default_factory=dict)
    package_dirs: frozenset[str] = frozenset()
    truncated: bool = False
    import_scan_truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.test_files

    def as_payload(self) -> dict[str, Any]:
        return {
            "test_file_count": len(self.test_files),
            "package_dir_count": len(self.package_dirs),
            "truncated": self.truncated,
            "import_scan_truncated": self.import_scan_truncated,
        }


EMPTY_REPO_TEST_INDEX = RepoTestIndex()


def build_repo_test_index(
    root: Path,
    *,
    max_files: int = MAX_INDEXED_FILES,
    max_test_files: int = MAX_INDEXED_TEST_FILES,
    max_import_scans: int = MAX_IMPORT_SCANS,
) -> RepoTestIndex:
    """Walk ``root`` once, collecting test files, their imports and package dirs.

    Ignored trees are pruned rather than filtered afterwards: a ``.venv`` or
    ``node_modules`` can hold more files than the whole repo, and descending into one
    before discarding it would put a multi-second stall in the middle of a turn.

    Bounded on every axis (files walked, test files kept, files whose imports are
    read, bytes read per file) and never raises: an unreadable tree yields an empty
    index and the gate simply does not apply. Imports are read from the head of each
    file, where import statements live. Directory order is sorted, so the same repo
    always yields the same index.
    """
    root_path = Path(root)
    test_files: list[str] = []
    package_dirs: set[str] = set()
    scanned = 0
    truncated = False
    try:
        walker = os.walk(root_path, onerror=None)
        for current, dirnames, filenames in walker:
            dirnames[:] = sorted(name for name in dirnames if name not in _IGNORED_DIR_NAMES)
            try:
                prefix = Path(current).relative_to(root_path).as_posix()
            except ValueError:
                dirnames[:] = []
                continue
            prefix = "" if prefix == "." else prefix
            if scanned >= max_files:
                truncated = True
                break
            for name in sorted(filenames):
                if scanned >= max_files:
                    truncated = True
                    break
                scanned += 1
                relative = f"{prefix}/{name}" if prefix else name
                if name == "__init__.py":
                    package_dirs.add(prefix)
                if is_test_file(relative):
                    if len(test_files) >= max_test_files:
                        truncated = True
                        continue
                    test_files.append(relative)
    except (OSError, ValueError):
        return EMPTY_REPO_TEST_INDEX

    imports: dict[str, frozenset[str]] = {}
    import_scan_truncated = False
    for relative in test_files:
        if len(imports) >= max_import_scans:
            import_scan_truncated = True
            break
        language = path_language(relative)
        if language not in {ScopeLanguage.PYTHON, ScopeLanguage.JAVASCRIPT}:
            continue
        try:
            with (root_path / relative).open("rb") as handle:
                head = handle.read(MAX_IMPORT_SCAN_BYTES).decode("utf-8", "ignore")
        except OSError:
            continue
        imports[relative] = extract_import_tokens(relative, head)

    return RepoTestIndex(
        test_files=tuple(test_files),
        imports=imports,
        package_dirs=frozenset(package_dirs),
        truncated=truncated,
        import_scan_truncated=import_scan_truncated,
    )


# ---------------------------------------------------------------------------
# Scope selection (pure)
# ---------------------------------------------------------------------------


class ScopeTier(IntEnum):
    """Proximity of a test file to the change. Lower is nearer."""

    #: The touched file *is* a test file, or a test file named after it.
    MIRROR = 0
    #: The test file statically imports a touched module.
    IMPORTER = 1
    #: The test file sits in the same directory as a touched file.
    SIBLING = 2
    #: The test file is under the touched package, or its mirrored test package.
    PACKAGE = 3


_TIER_REASONS = {
    ScopeTier.MIRROR: "test file mirrors a touched source file",
    ScopeTier.IMPORTER: "test file imports a touched module",
    ScopeTier.SIBLING: "test file sits beside a touched file",
    ScopeTier.PACKAGE: "test file is under a touched package",
}


@dataclass(frozen=True)
class ScopeEntry:
    path: str
    tier: ScopeTier
    anchor: str = ""

    @property
    def reason(self) -> str:
        return _TIER_REASONS.get(self.tier, "selected by proximity")

    def as_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "tier": int(self.tier),
            "tier_name": self.tier.name.casefold(),
            "anchor": self.anchor,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BlastRadiusScope:
    """The test scope selected for one set of touched paths."""

    entries: tuple[ScopeEntry, ...] = ()
    language: ScopeLanguage | None = None
    touched_paths: tuple[str, ...] = ()
    dropped_for_cap: tuple[str, ...] = ()
    dropped_for_runtime: tuple[str, ...] = ()
    shrink_rounds: int = 0
    index_truncated: bool = False

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)

    @property
    def empty(self) -> bool:
        return not self.entries

    @property
    def diffable(self) -> bool:
        """True when a run of this scope produces per-test ids the gate can diff."""
        return bool(self.entries) and self.language in _DIFFABLE_LANGUAGES

    def suggested_command(self) -> str:
        """A concrete command for the scope, for the advisory text.

        Advisory only: coverage is judged by which test files a run actually
        selected, never by matching this string, so a project whose runner differs
        can run the same files any way it likes.
        """
        if self.empty:
            return ""
        joined = " ".join(self.paths)
        if self.language == ScopeLanguage.PYTHON:
            return f"python -m pytest {joined} -q"
        if self.language == ScopeLanguage.GO:
            packages = sorted({f"./{_parent_dir(path)}".rstrip("/") or "." for path in self.paths})
            return f"go test {' '.join(packages)}"
        return joined

    def as_payload(self) -> dict[str, Any]:
        return {
            "entries": [entry.as_payload() for entry in self.entries],
            "paths": list(self.paths),
            "language": self.language.value if self.language is not None else "",
            "diffable": self.diffable,
            "touched_paths": list(self.touched_paths),
            "dropped_for_cap": list(self.dropped_for_cap),
            "dropped_for_runtime": list(self.dropped_for_runtime),
            "shrink_rounds": self.shrink_rounds,
            "index_truncated": self.index_truncated,
            "suggested_command": self.suggested_command(),
        }


EMPTY_SCOPE = BlastRadiusScope()


def _mirror_names(touched: str) -> frozenset[str]:
    pure = PurePosixPath(touched)
    stem = pure.stem
    suffix = pure.suffix.casefold()
    if suffix in _PY_EXTENSIONS:
        return frozenset({f"test_{stem}.py", f"{stem}_test.py"})
    if suffix in _GO_EXTENSIONS:
        return frozenset({f"{stem}_test.go"})
    if suffix in _JS_EXTENSIONS:
        return frozenset(
            {f"{stem}.test{ext}" for ext in _JS_EXTENSIONS}
            | {f"{stem}.spec{ext}" for ext in _JS_EXTENSIONS}
        )
    return frozenset()


def _package_relative_dir(touched: str, package_dirs: frozenset[str]) -> str:
    """The touched file's directory, relative to the top of its package.

    ``src/pkg/sub/mod.py`` -> ``pkg/sub``, so a mirrored ``tests/pkg/sub`` matches by
    suffix without needing to know the project's test-directory convention.
    """
    directory = _parent_dir(touched)
    if not directory:
        return ""
    parts = directory.split("/")
    index = len(parts)
    root_index = len(parts)
    while index > 0 and "/".join(parts[:index]) in package_dirs:
        root_index = index - 1
        index -= 1
    return "/".join(parts[root_index:])


def _tier_for(
    *,
    test_path: str,
    touched: str,
    index: RepoTestIndex,
    module_names: Sequence[str],
    mirror_names: frozenset[str],
    package_relative_dir: str,
) -> ScopeTier | None:
    if test_path == touched:
        return ScopeTier.MIRROR
    test_name = PurePosixPath(test_path).name
    if test_name in mirror_names:
        return ScopeTier.MIRROR
    tokens = index.imports.get(test_path)
    if tokens:
        language = path_language(touched)
        if language == ScopeLanguage.PYTHON and module_names:
            if python_import_matches(tokens, module_names):
                return ScopeTier.IMPORTER
        elif language == ScopeLanguage.JAVASCRIPT and js_import_matches(
            importer=test_path, tokens=tokens, target=touched
        ):
            return ScopeTier.IMPORTER
    touched_dir = _parent_dir(touched)
    test_dir = _parent_dir(test_path)
    if touched_dir and test_dir == touched_dir:
        return ScopeTier.SIBLING
    if touched_dir and test_dir.startswith(f"{touched_dir}/"):
        return ScopeTier.PACKAGE
    if package_relative_dir and (
        test_dir == package_relative_dir or test_dir.endswith(f"/{package_relative_dir}")
    ):
        return ScopeTier.PACKAGE
    return None


def _dominant_language(
    ranked: Sequence[tuple[ScopeTier, str, str]],
) -> ScopeLanguage | None:
    """The language the scope is run as: most-represented, Python winning ties.

    A scope has to be runnable by one runner, so a change spanning languages picks
    one. Python wins ties because it is the only language whose runs the gate can
    diff test-by-test.
    """
    counts: dict[ScopeLanguage, int] = {}
    for _tier, path, _anchor in ranked:
        language = path_language(path)
        if language is not None:
            counts[language] = counts.get(language, 0) + 1
    if not counts:
        return None
    return min(
        counts,
        key=lambda language: (-counts[language], language != ScopeLanguage.PYTHON, language.value),
    )


def select_blast_radius_scope(
    *,
    touched_paths: Iterable[str],
    index: RepoTestIndex,
    policy: BlastRadiusPolicy | None = None,
) -> BlastRadiusScope:
    """Select the tests likely affected by ``touched_paths``. Pure and deterministic.

    Each candidate takes its *best* (nearest) tier over all touched paths, and the
    scope is ordered nearest-first so the runtime cap and any later shrink both drop
    the weakest evidence first.
    """
    resolved_policy = policy or BlastRadiusPolicy()
    # Only source files in a language we understand have a blast radius we can
    # reason about. A README or a data fixture would otherwise drag in every test
    # sharing its directory on proximity alone, which is noise, not evidence.
    touched = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_repo_path(item) for item in touched_paths)
            if normalized and path_language(normalized) is not None
        )
    )
    if not touched or index.empty:
        return BlastRadiusScope(touched_paths=touched, index_truncated=index.truncated)

    best: dict[str, tuple[ScopeTier, str]] = {}
    for item in touched:
        module_names = python_module_names(item, index.package_dirs)
        mirror_names = _mirror_names(item)
        package_relative_dir = _package_relative_dir(item, index.package_dirs)
        for test_path in index.test_files:
            tier = _tier_for(
                test_path=test_path,
                touched=item,
                index=index,
                module_names=module_names,
                mirror_names=mirror_names,
                package_relative_dir=package_relative_dir,
            )
            if tier is None:
                continue
            current = best.get(test_path)
            if current is None or tier < current[0]:
                best[test_path] = (tier, item)

    ranked = sorted(
        ((tier, path, anchor) for path, (tier, anchor) in best.items()),
        key=lambda item: (
            int(item[0]),
            -max((_shared_prefix_length(item[1], touch) for touch in touched), default=0),
            item[1],
        ),
    )
    language = _dominant_language(ranked)
    ranked = [item for item in ranked if language is None or path_language(item[1]) == language]

    kept = ranked[: resolved_policy.max_scope_files]
    dropped = [path for _tier, path, _anchor in ranked[resolved_policy.max_scope_files :]]
    return BlastRadiusScope(
        entries=tuple(
            ScopeEntry(path=path, tier=tier, anchor=anchor) for tier, path, anchor in kept
        ),
        language=language,
        touched_paths=touched,
        dropped_for_cap=tuple(dropped),
        index_truncated=index.truncated,
    )


def shrink_scope_once(scope: BlastRadiusScope) -> BlastRadiusScope | None:
    """One shrink step, nearest tests kept. ``None`` when it cannot shrink further.

    Shrinking drops the widest proximity tier present; when every entry shares one
    tier there is nothing to drop by proximity, so it halves the list instead. It
    never returns an empty scope -- a scope too slow to run whole still runs its
    nearest test, because the alternative is shipping with no blast-radius evidence
    at all.
    """
    if scope.empty or len(scope.entries) <= MIN_SCOPE_FILES:
        return None
    widest = max(entry.tier for entry in scope.entries)
    kept = [entry for entry in scope.entries if entry.tier < widest]
    if not kept:
        kept = list(scope.entries[: max(MIN_SCOPE_FILES, len(scope.entries) // 2)])
    kept_paths = {entry.path for entry in kept}
    dropped = tuple(entry.path for entry in scope.entries if entry.path not in kept_paths)
    return BlastRadiusScope(
        entries=tuple(kept),
        language=scope.language,
        touched_paths=scope.touched_paths,
        dropped_for_cap=scope.dropped_for_cap,
        dropped_for_runtime=tuple(dict.fromkeys((*scope.dropped_for_runtime, *dropped))),
        shrink_rounds=scope.shrink_rounds + 1,
        index_truncated=scope.index_truncated,
    )


def shrink_scope_for_runtime(
    scope: BlastRadiusScope,
    *,
    observed_seconds: float,
    policy: BlastRadiusPolicy | None = None,
) -> BlastRadiusScope | None:
    """Shrink an over-budget scope, nearest tests kept. ``None`` when no shrink is due."""
    resolved_policy = policy or BlastRadiusPolicy()
    if scope.empty or observed_seconds <= resolved_policy.scope_seconds_cap:
        return None
    return shrink_scope_once(scope)


def apply_scope_shrink_rounds(scope: BlastRadiusScope, rounds: int) -> BlastRadiusScope:
    """Re-apply ``rounds`` shrink steps to a freshly selected scope.

    The scope is re-selected whenever the change touches more files, which would
    otherwise undo a shrink the runtime cap had already forced and quietly hand back
    a scope known to be too slow. Carrying the round count forward keeps the cap's
    decision in force across re-selection.
    """
    for _ in range(max(0, int(rounds))):
        smaller = shrink_scope_once(scope)
        if smaller is None:
            break
        scope = smaller
    return scope


# ---------------------------------------------------------------------------
# Observed scope runs (facts)
# ---------------------------------------------------------------------------


class ScopePhase(StrEnum):
    """When a test run happened relative to the first change to existing code."""

    #: Ran while no pre-existing repo path had been modified -- the clean tree.
    BASELINE = "baseline"
    #: Ran after existing code had already been changed.
    GATE = "gate"


_COMMAND_TOKEN_SPLIT_RE = re.compile(r"[\s;|&()<>]+")
_TOKEN_TRAILING_JUNK = ",;:'\"`)]}"
_SELECTOR_EXTENSIONS = frozenset(_PY_EXTENSIONS | _JS_EXTENSIONS | _GO_EXTENSIONS)


def command_path_selectors(command: str) -> tuple[str, ...]:
    """The test paths/directories a command explicitly selected.

    An empty result means the command named no paths -- a whole-suite run, which
    covers every scope. Node-id suffixes (``file.py::test``) reduce to their file,
    and flag values (``-k expr``, ``--maxfail=2``) are not paths so they drop out.
    """
    selectors: list[str] = []
    for raw in _COMMAND_TOKEN_SPLIT_RE.split(str(command or "")):
        token = raw.strip().strip("`'\"").rstrip(_TOKEN_TRAILING_JUNK)
        if not token or token.startswith("-"):
            continue
        token = token.split("::", 1)[0]
        normalized = normalize_repo_path(token)
        if not normalized:
            continue
        pure = PurePosixPath(normalized)
        if pure.suffix.casefold() in _SELECTOR_EXTENSIONS:
            selectors.append(normalized)
        elif "/" in normalized and "." not in pure.name:
            # A directory selector (``pytest tests/unit``). A bare word is not
            # treated as one: it is far more likely a subcommand or an -k value.
            selectors.append(normalized)
    return tuple(dict.fromkeys(selectors))


def selection_covers(selectors: Sequence[str], path: str) -> bool:
    """True when ``path`` was inside what a run selected (no selectors = whole suite)."""
    if not selectors:
        return True
    normalized = normalize_repo_path(path)
    if not normalized:
        return False
    for selector in selectors:
        clean = normalize_repo_path(selector)
        if not clean:
            continue
        if normalized == clean or normalized.startswith(f"{clean}/"):
            return True
    return False


@dataclass(frozen=True)
class ScopeRun:
    """One observed test run, with what it selected and when it happened."""

    command: str
    selectors: tuple[str, ...]
    phase: ScopePhase
    report: TestReport
    duration_seconds: float | None = None

    @property
    def whole_suite(self) -> bool:
        return not self.selectors

    @property
    def usable(self) -> bool:
        """Only a run whose failing ids are fully known can be compared."""
        return self.report.usable_as_baseline

    def covers(self, paths: Iterable[str]) -> bool:
        return all(selection_covers(self.selectors, path) for path in paths)

    def as_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "selectors": list(self.selectors),
            "whole_suite": self.whole_suite,
            "phase": self.phase.value,
            "duration_seconds": self.duration_seconds,
            "report": self.report.as_payload(),
        }


def classify_scope_phase(
    *,
    touched_repo_paths: Iterable[str],
    created_paths: Iterable[str],
) -> ScopePhase:
    """Baseline iff no pre-existing repo path has been modified yet.

    Files the agent authored this turn are excluded: creating a new file changes no
    existing behaviour, so a run made after writing a new test still observes the
    unpatched tree. This is the same discriminator step 5 uses, and for the same
    reason -- the edit generation counts the new file and would close the baseline
    window before the agent ever got to use it.
    """
    created = {
        normalized
        for normalized in (normalize_repo_path(path) for path in created_paths)
        if normalized
    }
    product = {
        normalized
        for normalized in (normalize_repo_path(path) for path in touched_repo_paths)
        if normalized and normalized not in created
    }
    return ScopePhase.GATE if product else ScopePhase.BASELINE


# ---------------------------------------------------------------------------
# Assessment (pure)
# ---------------------------------------------------------------------------


class BlastRadiusStatus(StrEnum):
    """The blast-radius protocol's state at a decision point."""

    #: Off, non-execute, nothing edited, or no diffable test surface near the change.
    NOT_APPLICABLE = "not_applicable"
    #: A scope exists but no post-fix run has covered it yet.
    GATE_MISSING = "gate_missing"
    #: The scope did run after the fix, but its output could not be parsed into
    #: per-test results. Honest degradation, not a deficit to nudge on: re-running
    #: the same runner would produce the same unreadable output.
    UNREADABLE = "unreadable"
    #: The scope ran after the fix, but no clean-tree run covers it, so its failures
    #: cannot be told apart from breakage that was already there.
    UNATTRIBUTED = "unattributed"
    #: The scope ran before and after the fix and broke nothing new.
    CLEAN = "clean"
    #: Tests that passed in the clean-tree baseline fail after the change.
    REGRESSED = "regressed"


@dataclass(frozen=True)
class BlastRadiusAssessment:
    """The mechanical state of the blast-radius gate for a turn."""

    status: BlastRadiusStatus = BlastRadiusStatus.NOT_APPLICABLE
    applicable: bool = False
    scope: BlastRadiusScope = EMPTY_SCOPE
    new_failures: tuple[str, ...] = ()
    pre_existing: tuple[str, ...] = ()
    unattributed: tuple[str, ...] = ()
    repaired: tuple[str, ...] = ()
    agent_authored: tuple[str, ...] = ()
    baseline_command: str = ""
    gate_command: str = ""
    baseline_whole_suite: bool = False
    over_broad_threshold: int = DEFAULT_OVER_BROAD_THRESHOLD

    @property
    def has_baseline(self) -> bool:
        return bool(self.baseline_command)

    @property
    def regressed(self) -> bool:
        return self.status == BlastRadiusStatus.REGRESSED

    @property
    def over_broad(self) -> bool:
        """The change broke so much that narrowing the patch beats fixing each test."""
        return len(self.new_failures) >= self.over_broad_threshold

    @property
    def satisfied(self) -> bool:
        return self.status == BlastRadiusStatus.CLEAN

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "applicable": self.applicable,
            "satisfied": self.satisfied,
            "over_broad": self.over_broad,
            "over_broad_threshold": self.over_broad_threshold,
            "new_failures": list(self.new_failures),
            "pre_existing": list(self.pre_existing),
            "unattributed": list(self.unattributed),
            "repaired": list(self.repaired),
            "agent_authored": list(self.agent_authored),
            "baseline_command": self.baseline_command,
            "gate_command": self.gate_command,
            "baseline_whole_suite": self.baseline_whole_suite,
            "has_baseline": self.has_baseline,
            "scope": self.scope.as_payload(),
        }


def _created_components(paths: Iterable[str]) -> list[str]:
    return [
        normalized for normalized in (normalize_repo_path(path) for path in paths) if normalized
    ]


def _join_commands(commands: Iterable[str]) -> str:
    """Render the baseline's command(s) for a message, bounded so it stays readable."""
    distinct = [command for command in dict.fromkeys(commands) if command]
    if not distinct:
        return ""
    rendered = ", ".join(distinct[:2])
    remaining = len(distinct) - min(len(distinct), 2)
    return f"{rendered} (+{remaining} more)" if remaining > 0 else rendered


def _id_file_is_agent_created(test_id: str, created: Sequence[str]) -> bool:
    file_path = node_id_file_path(test_id)
    if not file_path:
        return False
    normalized = normalize_repo_path(file_path)
    # Exact match only, for the reason step 3 documents: a basename match would let
    # a created ``test_foo.py`` mask a genuine regression in ``tests/test_foo.py``.
    return bool(normalized) and any(normalized == item for item in created)


def assess_blast_radius(
    *,
    scope: BlastRadiusScope,
    runs: Sequence[ScopeRun],
    applicable: bool,
    policy: BlastRadiusPolicy | None = None,
    agent_created_paths: Iterable[str] = (),
) -> BlastRadiusAssessment:
    """Diff the scope's post-fix run against its clean-tree baseline. Deterministic.

    The comparison is by *coverage*, not by command equality. The gate run must cover
    the whole scope -- that is what "run the scope" means. The baseline does not: every
    clean-tree run observed the same unpatched tree, so their coverage and their
    failures compose into one baseline, and attribution is then decided per failing
    test against what that composite actually ran. A failure the baseline never
    covered is ``unattributed`` -- never guessed into either column.
    """
    resolved_policy = policy or BlastRadiusPolicy()
    common: dict[str, Any] = {
        "applicable": bool(applicable),
        "scope": scope,
        "over_broad_threshold": resolved_policy.over_broad_threshold,
    }
    if not applicable or scope.empty or not scope.diffable:
        return BlastRadiusAssessment(status=BlastRadiusStatus.NOT_APPLICABLE, **common)

    scope_paths = scope.paths
    usable = [run for run in runs if run.usable]
    gate_runs = [run for run in usable if run.phase == ScopePhase.GATE and run.covers(scope_paths)]
    if not gate_runs:
        # Distinguish "never ran the scope" from "ran it, could not read the result".
        # Only the first is a deficit the agent can clear; nudging on the second
        # would loop forever against a runner whose output shape we cannot parse.
        unreadable = [
            run
            for run in runs
            if not run.usable and run.phase == ScopePhase.GATE and run.covers(scope_paths)
        ]
        if unreadable:
            return BlastRadiusAssessment(
                status=BlastRadiusStatus.UNREADABLE,
                gate_command=unreadable[-1].command,
                **common,
            )
        return BlastRadiusAssessment(status=BlastRadiusStatus.GATE_MISSING, **common)
    gate = gate_runs[-1]

    baseline_runs = [run for run in usable if run.phase == ScopePhase.BASELINE]
    baseline_whole_suite = any(run.whole_suite for run in baseline_runs)
    baseline_selectors: tuple[str, ...] = (
        ()
        if baseline_whole_suite
        else tuple(dict.fromkeys(item for run in baseline_runs for item in run.selectors))
    )
    baseline_failing = frozenset(
        test_id for run in baseline_runs for test_id in run.report.failing_ids
    )
    created = _created_components(agent_created_paths)

    new_failures: list[str] = []
    pre_existing: list[str] = []
    unattributed: list[str] = []
    agent_authored: list[str] = []
    for test_id in gate.report.failing_ids:
        if _id_file_is_agent_created(test_id, created):
            agent_authored.append(test_id)
        elif not baseline_runs:
            unattributed.append(test_id)
        elif test_id in baseline_failing:
            pre_existing.append(test_id)
        elif selection_covers(baseline_selectors, node_id_file_path(test_id) or ""):
            new_failures.append(test_id)
        else:
            unattributed.append(test_id)

    gate_failing = frozenset(gate.report.failing_ids)
    repaired = [
        test_id
        for test_id in dict.fromkeys(
            item for run in baseline_runs for item in run.report.failing_ids
        )
        if test_id not in gate_failing
        and selection_covers(gate.selectors, node_id_file_path(test_id) or "")
    ]

    if new_failures:
        status = BlastRadiusStatus.REGRESSED
    elif not baseline_runs or unattributed:
        status = BlastRadiusStatus.UNATTRIBUTED
    else:
        status = BlastRadiusStatus.CLEAN

    return BlastRadiusAssessment(
        status=status,
        new_failures=tuple(new_failures),
        pre_existing=tuple(pre_existing),
        unattributed=tuple(unattributed),
        repaired=tuple(repaired),
        agent_authored=tuple(agent_authored),
        baseline_command=_join_commands(run.command for run in baseline_runs),
        gate_command=gate.command,
        baseline_whole_suite=baseline_whole_suite,
        **common,
    )


def blast_radius_blocks_finalization(
    assessment: BlastRadiusAssessment,
    *,
    material_edit_count: int,
) -> bool:
    """True when the gate must not let this turn finalize yet.

    Only two states block: a scope that was never run after the fix, and proven new
    failures. ``unattributed`` does not block here -- step 3's own unattributed stage
    already owns that case, and blocking twice for one fact would just burn repair
    rounds. A turn that changed nothing has no blast radius.
    """
    if not assessment.applicable or material_edit_count <= 0:
        return False
    return assessment.status in {BlastRadiusStatus.GATE_MISSING, BlastRadiusStatus.REGRESSED}


# ---------------------------------------------------------------------------
# Directives and advisories (agent-facing text)
# ---------------------------------------------------------------------------


BLAST_RADIUS_TURN_DIRECTIVE = (
    "Blast-radius protocol (a correct fix that breaks other tests is a failed task):\n"
    "- BEFORE you change any existing file, run the tests that cover the area you are "
    "about to touch (the module's own test file, and the tests around it) once, and keep "
    "their result. That run is your baseline: whatever already fails there is not yours, "
    "and I will not attribute it to your change. Without it I cannot tell your breakage "
    "apart from breakage that was already in the repo.\n"
    "- AFTER your fix verifies, run that same set again. Anything failing now that passed "
    "in the baseline is a regression you introduced, and it is part of your task.\n"
    "- Repair regressions by narrowing your change, not by widening it. If one change "
    "breaks a large number of previously passing tests, the approach itself is wrong: "
    "revert it and write a narrower patch rather than patching up each failing test.\n"
    "- Never delete, skip, or weaken an existing test to make it pass."
)


def build_blast_radius_scope_advisory(
    scope: BlastRadiusScope,
    *,
    has_baseline: bool,
) -> str:
    """The concrete scope, emitted once the first change to existing code lands."""
    if scope.empty:
        return ""
    command = scope.suggested_command()
    listed = ", ".join(scope.paths[:MAX_LISTED_IDS])
    extra = len(scope.paths) - min(len(scope.paths), MAX_LISTED_IDS)
    if extra > 0:
        listed += f" (+{extra} more)"
    lines = [
        "Blast-radius scope for your change: "
        + listed
        + ". These are the tests nearest what you touched - the ones that mirror it, "
        "import it, or sit in the same package.",
    ]
    if has_baseline:
        lines.append(
            "A clean-tree run already covers this scope, so I can attribute failures. "
            f"Re-run it after your fix (for example `{command}`) and make sure nothing "
            "that passed then fails now."
        )
    else:
        lines.append(
            "Nothing was run on the clean tree covering this scope, so failures here "
            "cannot yet be told apart from breakage that was already in the repo. Run it "
            f"after your fix anyway (for example `{command}`) and read the result against "
            "what you know about the repo - do not assume a failure is pre-existing."
        )
    lines.append(
        "Advisory only - this does not block your edit. Run any equivalent command; "
        "what matters is that these files are covered."
    )
    return " ".join(lines)


def _format_ids(ids: Sequence[str]) -> str:
    listed = list(ids[:MAX_LISTED_IDS])
    rendered = ", ".join(listed)
    remaining = len(ids) - len(listed)
    if remaining > 0:
        rendered += f" (+{remaining} more)"
    return rendered


def build_blast_radius_nudge_line(assessment: BlastRadiusAssessment) -> str:
    """The bounded repair nudge for the current blast-radius status."""
    if not assessment.applicable:
        return ""
    if assessment.status == BlastRadiusStatus.GATE_MISSING:
        command = assessment.scope.suggested_command()
        detail = f" (for example `{command}`)" if command else ""
        return (
            "- You have not run the tests around what you changed: "
            + _format_ids(assessment.scope.paths)
            + f". Run them now{detail} and confirm your change did not break them. A "
            "written explanation cannot clear this - only the run can."
        )
    if assessment.status != BlastRadiusStatus.REGRESSED:
        return ""
    baseline = assessment.baseline_command or "the clean-tree baseline"
    if assessment.over_broad:
        return (
            f"- Your change broke {len(assessment.new_failures)} tests that passed before it: "
            + _format_ids(assessment.new_failures)
            + f". That many failures from one change means the change itself is too broad, "
            f"not that each test needs fixing. Revert it and write a narrower patch that "
            f"touches only what the task requires, then re-run both your reproduction and "
            f"`{baseline}`. Do not edit, skip, or delete those tests."
        )
    return (
        "- Tests your change broke (they passed in the clean-tree baseline of "
        f"`{baseline}`): "
        + _format_ids(assessment.new_failures)
        + ". Fix them while keeping your fix intact - prefer narrowing your change over "
        "adding more of it - then re-run both your reproduction and this scope. Do not "
        "edit, skip, or delete those tests to make them pass."
    )


_STATUS_SUMMARY_LINES = {
    BlastRadiusStatus.CLEAN: (
        "Blast radius: re-ran {count} nearby test file(s) after the fix{detail}; nothing "
        "that passed before it fails now."
    ),
    BlastRadiusStatus.REGRESSED: (
        "⛔ REGRESSIONS INTRODUCED — {n} test(s) that passed before my change now fail: "
        "{ids}. Baseline: `{baseline}`. I could not clear these within this run, so this "
        "result ships with KNOWN BREAKAGE outside the fix itself."
    ),
    BlastRadiusStatus.UNATTRIBUTED: (
        "⚠️ Blast radius: ran {count} nearby test file(s) after the fix{detail}, but with no "
        "clean-tree run covering them {attribution_detail}"
    ),
    BlastRadiusStatus.GATE_MISSING: (
        "⚠️ Blast radius: the tests around what I changed ({count} file(s)) were never run "
        "after the fix, so nothing confirms the change did not break them."
    ),
    BlastRadiusStatus.UNREADABLE: (
        "⚠️ Blast radius: the tests around what I changed ({count} file(s)) ran after the "
        "fix{detail}, but I could not read per-test results out of the runner's output, so "
        "I cannot say whether the change broke any of them."
    ),
}


def build_blast_radius_status_summary(assessment: BlastRadiusAssessment) -> str:
    """The visible blast-radius line appended to the summary.

    Emitted for every applicable turn, satisfied or not: a clean result says so
    plainly, and an unresolved regression leads with the failures. Reporting success
    without naming what else the change touched is the failure mode this whole step
    exists to remove, so silence is never an option here.
    """
    if not assessment.applicable:
        return ""
    template = _STATUS_SUMMARY_LINES.get(assessment.status, "")
    if not template:
        return ""
    command = (assessment.gate_command or "").strip()
    unattributed_ids = assessment.new_failures or assessment.unattributed
    attribution_detail = (
        "I cannot tell whether these failures are mine: "
        f"{_format_ids(unattributed_ids)}. Their cause is UNATTRIBUTED — neither confirmed "
        "pre-existing nor confirmed a regression."
        if unattributed_ids
        else (
            "no failures were reported, but the clean result is UNATTRIBUTED because it "
            "cannot be compared with a baseline."
        )
    )
    line = template.format(
        count=len(assessment.scope.paths),
        detail=f" (`{command}`)" if command else "",
        n=len(assessment.new_failures),
        # Whichever column this status is about: proven breakage, else the failures
        # that could not be attributed.
        ids=_format_ids(unattributed_ids),
        attribution_detail=attribution_detail,
        baseline=assessment.baseline_command or "the clean-tree baseline",
    )
    if assessment.status == BlastRadiusStatus.REGRESSED and assessment.over_broad:
        line += (
            f" Breaking {len(assessment.new_failures)} previously passing tests means the "
            "change is over-broad and should be rewritten narrowly, not patched up test by "
            "test."
        )
    if assessment.scope.dropped_for_runtime:
        line += (
            f" Scope was shrunk to stay inside the runtime cap; "
            f"{len(assessment.scope.dropped_for_runtime)} further test file(s) were not run."
        )
    elif assessment.scope.dropped_for_cap:
        line += (
            f" Scope was capped; {len(assessment.scope.dropped_for_cap)} further test file(s) "
            "were not run."
        )
    return f"\n\n---\n{line}"
