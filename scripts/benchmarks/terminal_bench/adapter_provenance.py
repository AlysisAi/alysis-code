"""Whether the wheel under test and the adapter driving it are the same tree.

Two provenance defects have now cost a campaign its meaning, and neither was
visible while the campaign was running.

*The wrong commit.* A run was built from the tip of an unmerged fork arm and
labelled "latest main". The wheel's stale version stamp was the only tell, and
nobody looked at it until teardown.

*The wrong checkout.* A run's artifacts showed adapter behaviour that no single
commit explains -- session mirroring present, per-task budgets absent. The
runner adapter had been imported from a different checkout than the one the
wheel was built from, and nothing recorded the adapter's own SHA, so the
inconsistency could only be inferred afterwards from behaviour.

The wheel already proves what it is (``alysis_code.build_identity``). This
module supplies the missing half -- what the *adapter* is -- and the comparison
between them:

*The adapter's identity.* :func:`resolve_checkout_identity` asks git about the
checkout the adapter file was actually imported from, not about a configured
repo root or the current working directory. The file's own path is the only
input that cannot be pointed somewhere else.

*The wheel's identity.* :func:`read_wheel_identity` reads the stamp and the
version out of the wheel archive itself. Reading them from the host's source
tree would defeat the purpose: that tree is the adapter checkout, which is
precisely the thing being compared. The stamp is parsed, never executed.

*The refusal.* :func:`decide_provenance` fails a benchmark run at startup on
three disagreements, each of which silently invalidated a past campaign:
unrelated commits, a version the adapter's tree never declared, and a
latest-main claim the build cannot support. An operator who knows better
passes a reason, which is recorded verbatim alongside the failures it waived --
a blank reason is not a reason and does not override.

Stdlib only, and loadable as a bare file: the adapter runs on a runner box
where the package may not be importable, and the tests load this straight from
its path.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Mirrors ``build_identity.UNAVAILABLE``. Duplicated rather than imported so
#: this module stays loadable without the package on the path.
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"

#: Set on the host to record why a provenance failure was accepted. The value is
#: the operator's reason, recorded verbatim; the runner script's
#: ``--provenance-override REASON`` writes it.
PROVENANCE_OVERRIDE_ENV = "ALYSIS_PROVENANCE_OVERRIDE"

#: Set on the host when the campaign is being labelled as running latest main.
#: Only then is check (c) applied -- a run that never claimed to be main is not
#: failed for not being it.
LATEST_MAIN_ENV = "ALYSIS_TBENCH_LATEST_MAIN"

#: Set to a falsey value on the host to disable the guard entirely. Opt-out
#: rather than opt-in, for the same reason the clean-build refusal is: the
#: failure it catches is silent, and by the time it is noticed the campaign is
#: already worthless. Disabling it is not the same as overriding it -- an
#: override records a reason and still reports the disagreement.
PROVENANCE_GUARD_ENV = "ALYSIS_PROVENANCE_GUARD"

FAILURE_COMMIT_MISMATCH = "commit_mismatch"
FAILURE_VERSION_MISMATCH = "version_mismatch"
FAILURE_NOT_ORIGIN_MAIN = "not_origin_main"

BUDGET_SOURCE_HOST_OVERRIDE = "host_override"
BUDGET_SOURCE_TASK_TABLE = "task_table"
BUDGET_SOURCE_FLAT_DEFAULT = "flat_default"
# A host override tightened a per-task table budget (min(table, override)). The
# override is a ceiling, never a floor: a campaign that exports one flat
# ALYSIS_RUN_BUDGET_SECONDS for the whole suite (the common operator mistake)
# must not displace the per-task budget that stops the agent cleanly before
# Harbor's per-task kill. Recorded distinctly so an operator can see the table
# still governed the task and the override only capped it.
BUDGET_SOURCE_HOST_CEILING = "host_ceiling"

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})
_FALSEY = frozenset({"0", "false", "no", "off", "disabled"})
_COMMIT_RE = re.compile(r"\A[0-9a-f]{7,64}\Z")
_SUBJECT_MAX_CHARS = 200

#: ``[project]`` ... ``version = "..."``, stopping at the next section header so
#: a ``version`` key belonging to some other table cannot be picked up. Used in
#: place of ``tomllib`` because that is 3.11+ and these helpers are expected to
#: run in the bare 3.10 interpreter the runner box also has.
_PROJECT_VERSION_RE = re.compile(
    r"^\[project\]\s*$(?P<body>.*?)(?=^\[|\Z)",
    re.MULTILINE | re.DOTALL,
)
_VERSION_LINE_RE = re.compile(r"^\s*version\s*=\s*[\"'](?P<version>[^\"']+)[\"']", re.MULTILINE)

GitRunner = Callable[[Sequence[str]], "tuple[int, str]"]


class ProvenanceError(RuntimeError):
    """Raised to refuse a benchmark run whose provenance does not hold up."""


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------


def _run_git(args: Sequence[str], *, cwd: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return completed.returncode, completed.stdout


def git_runner_for(path: str | os.PathLike[str]) -> GitRunner:
    """A git runner rooted at ``path``, for the checkout that contains it."""
    root = Path(path)
    if root.is_file():
        root = root.parent
    return lambda args: _run_git(args, cwd=root)


# ---------------------------------------------------------------------------
# The adapter's own checkout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckoutIdentity:
    """What the checkout an adapter was imported from can prove about itself."""

    commit: str = ""
    subject: str = ""
    dirty: bool = True
    root: str = ""

    @property
    def commit_short(self) -> str:
        return self.commit[:12] if self.commit else ""

    @property
    def is_identifiable(self) -> bool:
        return bool(_COMMIT_RE.fullmatch(self.commit.strip().casefold()))

    def describe(self) -> str:
        return (
            f"commit: {self.commit_short or UNKNOWN}, "
            f"subject: {self.subject or UNKNOWN}, "
            f"dirty: {'yes' if self.dirty else 'no'}"
        )

    def payload(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "commit_short": self.commit_short,
            "commit_subject": self.subject,
            "dirty": self.dirty,
            "root": self.root,
            "identifiable": self.is_identifiable,
        }


def parse_checkout_dirty(porcelain_output: str) -> bool:
    """Whether ``git status --porcelain`` reported anything at all.

    Deliberately stricter than the build stamp's equivalent, which excludes the
    generated ``_build_info.py`` it is about to rewrite. Nothing rewrites the
    adapter checkout during a run, so every reported path counts -- including
    an untracked one, which is how a hand-edited adapter usually shows up.
    """
    return any(line.strip() for line in str(porcelain_output or "").splitlines())


def resolve_checkout_identity(
    start: str | os.PathLike[str],
    *,
    git_runner: GitRunner | None = None,
) -> CheckoutIdentity:
    """Identify the checkout containing ``start``, which is normally ``__file__``.

    Resolved from the importing file's own path rather than a configured root:
    the whole failure being guarded against is an adapter loaded from somewhere
    other than where the operator believes, and a configured root would be
    describing the belief rather than the fact.

    Every probe failing yields an unidentifiable checkout rather than an error.
    That is the honest answer for a source tree shipped without its ``.git``,
    and :func:`decide_provenance` is what decides it is not good enough.
    """
    runner: GitRunner = git_runner if git_runner is not None else git_runner_for(start)

    root_code, root_out = runner(["rev-parse", "--show-toplevel"])
    root = root_out.strip().splitlines()[0].strip() if root_code == 0 and root_out.strip() else ""

    code, out = runner(["rev-parse", "HEAD"])
    commit = out.strip().splitlines()[0].strip().casefold() if code == 0 and out.strip() else ""
    if not _COMMIT_RE.fullmatch(commit):
        return CheckoutIdentity(commit="", subject="", dirty=True, root=root)

    subject_code, subject_out = runner(["log", "-1", "--format=%s", commit])
    subject = ""
    if subject_code == 0 and subject_out.strip():
        subject = subject_out.strip().splitlines()[0].strip()[:_SUBJECT_MAX_CHARS]

    status_code, status_out = runner(["status", "--porcelain"])
    # Fail closed, exactly as the build stamp does: a status probe that did not
    # answer leaves the tree's state unknown, and unknown is not clean.
    dirty = True if status_code != 0 else parse_checkout_dirty(status_out)
    return CheckoutIdentity(commit=commit, subject=subject, dirty=dirty, root=root)


def pyproject_version_at(commit: str, *, git_runner: GitRunner) -> str:
    """The ``[project] version`` declared at ``commit``, or ``""``.

    Read out of git rather than off disk so it is the version that commit
    actually declares, not whatever the working tree happens to hold now.
    """
    if not _COMMIT_RE.fullmatch(str(commit or "").strip().casefold()):
        return ""
    code, out = git_runner(["show", f"{commit}:pyproject.toml"])
    if code != 0 or not out.strip():
        return ""
    return parse_project_version(out)


def parse_project_version(pyproject_text: str) -> str:
    """The ``version`` under ``[project]`` in ``pyproject_text``, or ``""``."""
    section = _PROJECT_VERSION_RE.search(str(pyproject_text or ""))
    if section is None:
        return ""
    match = _VERSION_LINE_RE.search(section.group("body"))
    return match.group("version").strip() if match else ""


def commits_related(left: str, right: str, *, git_runner: GitRunner) -> bool:
    """True when ``left`` and ``right`` are the same commit or one contains the other.

    Ancestry counts as agreement, not just equality: a wheel built from the
    commit the adapter checkout is sitting on, and a wheel built from a commit
    that checkout has since moved past, are both coherent stories about one
    line of history. Two tips of a fork are not, and that is the case that
    produced an unattributable campaign.

    Unknown commits are *not* related. A wheel stamped with a commit this
    checkout has never heard of is the strongest possible evidence that the two
    came from different trees.
    """
    left_ok = bool(_COMMIT_RE.fullmatch(str(left or "").strip().casefold()))
    right_ok = bool(_COMMIT_RE.fullmatch(str(right or "").strip().casefold()))
    if not (left_ok and right_ok):
        return False
    if left.strip().casefold() == right.strip().casefold():
        return True
    for candidate in (left, right):
        if git_runner(["cat-file", "-e", f"{candidate}^{{commit}}"])[0] != 0:
            return False
    if git_runner(["merge-base", "--is-ancestor", left, right])[0] == 0:
        return True
    return git_runner(["merge-base", "--is-ancestor", right, left])[0] == 0


# ---------------------------------------------------------------------------
# The wheel under test
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WheelIdentity:
    """The stamp and version read out of the wheel archive itself."""

    path: str = ""
    version: str = ""
    commit: str = ""
    commit_subject: str = ""
    resolved_origin_main: str = UNAVAILABLE
    readable: bool = False

    @property
    def commit_short(self) -> str:
        return self.commit[:12] if self.commit else ""

    @property
    def is_identifiable(self) -> bool:
        return bool(_COMMIT_RE.fullmatch(self.commit.strip().casefold()))

    @property
    def origin_main_resolved(self) -> bool:
        return bool(_COMMIT_RE.fullmatch(self.resolved_origin_main.strip().casefold()))

    @property
    def is_origin_main(self) -> bool:
        """True only when the remote was resolved *and* matched.

        A build that could not see the remote must not be able to claim it is
        main; not knowing never reads as agreement.
        """
        if not (self.is_identifiable and self.origin_main_resolved):
            return False
        return self.commit.strip().casefold() == self.resolved_origin_main.strip().casefold()

    def describe(self) -> str:
        return (
            f"version: {self.version or UNKNOWN}, "
            f"commit: {self.commit_short or UNKNOWN}, "
            f"subject: {self.commit_subject or UNKNOWN}"
        )

    def payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "readable": self.readable,
            "version": self.version,
            "commit": self.commit,
            "commit_short": self.commit_short,
            "commit_subject": self.commit_subject,
            "identifiable": self.is_identifiable,
            "resolved_origin_main": self.resolved_origin_main,
            "origin_main_resolved": self.origin_main_resolved,
            "is_origin_main": self.is_origin_main,
        }


def parse_build_info_source(source: str) -> dict[str, Any]:
    """Read ``BUILD_*`` assignments out of a ``_build_info.py`` without executing it.

    The file is generated and is only ever module-level literal assignments, so
    parsing it is enough -- and importing a module out of an untrusted archive
    to learn whether that archive can be trusted is the wrong order of
    operations.
    """
    values: dict[str, Any] = {}
    try:
        tree = ast.parse(str(source or ""))
    except SyntaxError:
        return values
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.startswith("BUILD_"):
                values[target.id] = value
    return values


def read_wheel_identity(wheel_path: str | os.PathLike[str]) -> WheelIdentity:
    """Read the build stamp and version out of a wheel.

    An unreadable, missing or stamp-less wheel yields ``readable=False`` with
    empty fields rather than raising, so the refusal is made in one place with
    the full picture instead of as an import-time traceback.
    """
    path = Path(wheel_path)
    identity = WheelIdentity(path=os.fspath(path))
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            stamp_names = [n for n in names if n.endswith("alysis_code/_build_info.py")]
            metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
            stamp: dict[str, Any] = {}
            if stamp_names:
                stamp = parse_build_info_source(
                    archive.read(sorted(stamp_names)[0]).decode("utf-8", "replace")
                )
            version = ""
            if metadata_names:
                version = parse_metadata_version(
                    archive.read(sorted(metadata_names)[0]).decode("utf-8", "replace")
                )
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return identity

    return WheelIdentity(
        path=os.fspath(path),
        version=version,
        commit=str(stamp.get("BUILD_COMMIT") or "").strip().casefold(),
        commit_subject=str(stamp.get("BUILD_COMMIT_SUBJECT") or "").strip(),
        resolved_origin_main=(
            str(stamp.get("BUILD_RESOLVED_ORIGIN_MAIN") or "").strip().casefold() or UNAVAILABLE
        ),
        readable=True,
    )


def parse_metadata_version(metadata_text: str) -> str:
    """The ``Version:`` field of a wheel's ``METADATA``, or ``""``."""
    for line in str(metadata_text or "").splitlines():
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
        if not line.strip():
            # Headers end at the first blank line; the body can say anything.
            break
    return ""


# ---------------------------------------------------------------------------
# Per-task budget provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetResolution:
    """Which budget a task actually ran under, and where the number came from.

    Recorded per task because the silent case is the damaging one: when the
    per-task table is not consulted, the run falls back to a flat deadline and
    behaves exactly as it did before the table existed. The previous campaign's
    artifacts showed that fallback and nothing distinguished it from a table
    hit, so it could only be diagnosed by reasoning backwards from behaviour.
    """

    task_name: str = ""
    seconds: int = 0
    source: str = BUDGET_SOURCE_FLAT_DEFAULT
    table_hit: bool = False
    # The host override value, when one was present, whether or not it won.
    # None means no override was set. Recorded so an operator can see both the
    # budget that governed the task and the ceiling that was in force.
    host_ceiling_seconds: int | None = None

    @property
    def task_identified(self) -> bool:
        return bool(self.task_name)

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "task_name": self.task_name,
            "task_identified": self.task_identified,
            "budget_seconds": self.seconds,
            "budget_source": self.source,
            "budget_table_hit": self.table_hit,
        }
        if self.host_ceiling_seconds is not None:
            data["budget_host_ceiling_seconds"] = self.host_ceiling_seconds
        return data


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def latest_main_claimed(raw: str | None) -> bool:
    """Whether this campaign is labelled as running latest main."""
    return str(raw or "").strip().casefold() in _TRUTHY


def guard_enabled(raw: str | None) -> bool:
    """Whether the provenance guard runs. Unset means yes.

    Only an explicitly falsey value turns it off, so a typo cannot silently
    disable the check and leave a campaign unguarded while looking guarded.
    """
    value = str(raw or "").strip().casefold()
    if not value:
        return True
    return value not in _FALSEY


def normalize_override_reason(raw: str | None) -> str:
    """The operator's override reason, or ``""`` when none was really given.

    Whitespace is not a reason. The repository already refuses a waived check
    whose rationale is blank, and an override that records nothing about why it
    was taken reproduces the failure this whole module exists to prevent.
    """
    return str(raw or "").strip()


@dataclass(frozen=True)
class ProvenanceFailure:
    """One disagreement, named so artifacts can be filtered on it."""

    code: str
    message: str

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ProvenanceDecision:
    """Whether a benchmark run may start, and what it must record either way."""

    failures: tuple[ProvenanceFailure, ...] = ()
    override_reason: str = ""
    latest_main_claimed: bool = False
    wheel: WheelIdentity = field(default_factory=WheelIdentity)
    checkout: CheckoutIdentity = field(default_factory=CheckoutIdentity)
    adapter_pyproject_version: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    @property
    def overridden(self) -> bool:
        """True when failures were waived by an explicit, non-blank reason."""
        return self.failed and bool(self.override_reason)

    @property
    def allowed(self) -> bool:
        return not self.failed or self.overridden

    @property
    def failure_codes(self) -> tuple[str, ...]:
        return tuple(failure.code for failure in self.failures)

    def message(self) -> str:
        """The refusal, as an operator can act on it."""
        lines = [
            "Benchmark provenance check failed. The wheel under test and the "
            "adapter driving it do not agree, so any score this run produces "
            "cannot be attributed to a source tree.",
            "",
            f"  wheel    ({self.wheel.path or UNKNOWN}): {self.wheel.describe()}",
            f"  adapter  ({self.checkout.root or UNKNOWN}): {self.checkout.describe()}",
            "",
        ]
        lines += [f"  - [{failure.code}] {failure.message}" for failure in self.failures]
        lines += [
            "",
            "Rebuild the wheel from this checkout (python3 "
            "scripts/generate_build_info.py, then build), or check out the "
            "commit the wheel was built from. To proceed anyway, pass "
            "--provenance-override with a reason; it is recorded verbatim in "
            "every artifact this run produces.",
        ]
        return "\n".join(lines)

    def payload(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "failed": self.failed,
            "overridden": self.overridden,
            "override_reason": self.override_reason,
            "failure_codes": list(self.failure_codes),
            "failures": [failure.payload() for failure in self.failures],
            "latest_main_claimed": self.latest_main_claimed,
            "adapter_pyproject_version": self.adapter_pyproject_version,
            "wheel": self.wheel.payload(),
            "adapter_checkout": self.checkout.payload(),
        }


def decide_provenance(
    *,
    wheel: WheelIdentity,
    checkout: CheckoutIdentity,
    adapter_pyproject_version: str = "",
    latest_main: bool = False,
    override_reason: str | None = None,
    git_runner: GitRunner | None = None,
) -> ProvenanceDecision:
    """Apply the three provenance checks and say what a run may do.

    Each check corresponds to a way a past campaign was silently invalidated,
    and each fails closed: an input that could not be read fails the check that
    needed it rather than being skipped, because a check that quietly does not
    run is indistinguishable from one that passed.
    """
    runner: GitRunner = git_runner if git_runner is not None else (lambda _args: (1, ""))
    failures: list[ProvenanceFailure] = []

    # (a) The wheel and the adapter must describe one line of history.
    if not wheel.readable:
        failures.append(
            ProvenanceFailure(
                FAILURE_COMMIT_MISMATCH,
                f"the wheel could not be read, so it cannot prove which commit it is "
                f"({wheel.path or UNKNOWN}).",
            )
        )
    elif not wheel.is_identifiable:
        failures.append(
            ProvenanceFailure(
                FAILURE_COMMIT_MISMATCH,
                "the wheel records no commit; it was built without running "
                "scripts/generate_build_info.py.",
            )
        )
    elif not checkout.is_identifiable:
        failures.append(
            ProvenanceFailure(
                FAILURE_COMMIT_MISMATCH,
                f"the adapter checkout at {checkout.root or UNKNOWN} cannot name its "
                f"HEAD, so it cannot be compared with the wheel's "
                f"{wheel.commit_short}.",
            )
        )
    elif not commits_related(wheel.commit, checkout.commit, git_runner=runner):
        failures.append(
            ProvenanceFailure(
                FAILURE_COMMIT_MISMATCH,
                f"the wheel was built from {wheel.commit_short} "
                f"({wheel.commit_subject or UNKNOWN}) but the adapter is running from "
                f"{checkout.commit_short} ({checkout.subject or UNKNOWN}); neither "
                f"commit contains the other, so they are separate lines of history.",
            )
        )

    # (b) The wheel's version must be one the adapter's tree actually declares.
    if not wheel.version:
        failures.append(
            ProvenanceFailure(
                FAILURE_VERSION_MISMATCH,
                "the wheel does not declare a version in its METADATA.",
            )
        )
    elif not adapter_pyproject_version:
        failures.append(
            ProvenanceFailure(
                FAILURE_VERSION_MISMATCH,
                f"the adapter checkout does not declare a version at "
                f"{checkout.commit_short or UNKNOWN}, so the wheel's "
                f"{wheel.version} cannot be confirmed.",
            )
        )
    elif wheel.version != adapter_pyproject_version:
        failures.append(
            ProvenanceFailure(
                FAILURE_VERSION_MISMATCH,
                f"the wheel is version {wheel.version} but pyproject.toml at the "
                f"adapter's HEAD ({checkout.commit_short}) declares "
                f"{adapter_pyproject_version}.",
            )
        )

    # (c) Only checked when the run claims to be latest main.
    if latest_main:
        if not wheel.origin_main_resolved:
            failures.append(
                ProvenanceFailure(
                    FAILURE_NOT_ORIGIN_MAIN,
                    "this run is labelled latest-main, but the wheel did not resolve "
                    "origin/main at build time, so the claim cannot be checked.",
                )
            )
        elif not wheel.is_origin_main:
            failures.append(
                ProvenanceFailure(
                    FAILURE_NOT_ORIGIN_MAIN,
                    f"this run is labelled latest-main, but the wheel was built from "
                    f"{wheel.commit_short} while origin/main was "
                    f"{wheel.resolved_origin_main[:12]}.",
                )
            )

    return ProvenanceDecision(
        failures=tuple(failures),
        override_reason=normalize_override_reason(override_reason),
        latest_main_claimed=latest_main,
        wheel=wheel,
        checkout=checkout,
        adapter_pyproject_version=adapter_pyproject_version,
    )


def evaluate_adapter_provenance(
    *,
    adapter_file: str | os.PathLike[str],
    wheel_path: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
    getenv: Callable[[str], str | None] | None = None,
    git_runner: GitRunner | None = None,
) -> ProvenanceDecision:
    """Resolve both identities and decide, in one call for the adapter to make.

    ``getenv`` exists because the Harbor adapter reads host settings through
    its own accessor, which consults per-agent overrides before the process
    environment.
    """
    if getenv is None:
        source: Mapping[str, str] = os.environ if environ is None else environ

        def getenv(key: str) -> str | None:  # noqa: E731 - simple local default
            return source.get(key)

    runner: GitRunner = git_runner if git_runner is not None else git_runner_for(adapter_file)
    checkout = resolve_checkout_identity(adapter_file, git_runner=runner)
    wheel = read_wheel_identity(wheel_path)
    return decide_provenance(
        wheel=wheel,
        checkout=checkout,
        adapter_pyproject_version=pyproject_version_at(checkout.commit, git_runner=runner),
        latest_main=latest_main_claimed(getenv(LATEST_MAIN_ENV)),
        override_reason=getenv(PROVENANCE_OVERRIDE_ENV),
        git_runner=runner,
    )
