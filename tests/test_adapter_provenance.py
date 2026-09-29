"""Tests for benchmark adapter provenance: identity, the guard, the override.

Runnable two ways:

    python3 tests/test_adapter_provenance.py     # standalone, stdlib only
    pytest tests/test_adapter_provenance.py

The module under test is loaded directly from its file path, so these run in a
bare interpreter with no third-party packages installed -- the same environment
the runner box can always provide, and the one the adapter itself has to work
in when Harbor loads it as a plain file.

The git fixtures answer by full argv where it matters, because the guard issues
several probes that share a subcommand (two ``merge-base`` calls in opposite
directions, a ``cat-file`` existence check per side), and a fake that could not
tell them apart would let a broken ancestry check pass.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "scripts" / "benchmarks" / "terminal_bench" / "adapter_provenance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_adapter_provenance", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the module defines dataclasses, and
    # ``dataclasses`` resolves annotations via ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ap = _load_module()

_WHEEL_SHA = "f0b3f4beccf1ba09272c0f69a2ea8f18931c832e"
_ADAPTER_SHA = "007bc2ba1202877fe93651b4d655e79d8079e628"
_MAIN_SHA = "75c6ab6a6646087ea63f38bb5f0194abcc7ca7b5"
_VERSION = "0.14.0"


def _git(responses):
    """A fake ``git``: full-argv keys win over subcommand keys."""
    calls: list[list[str]] = []

    def run(args):
        argv = list(args)
        calls.append(argv)
        return responses.get(" ".join(argv), responses.get(argv[0], (1, "")))

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _unrelated_git():
    """Both commits exist; neither contains the other."""
    return _git({"cat-file": (0, ""), "merge-base": (1, "")})


def _related_git():
    """Both commits exist; the first is an ancestor of the second."""
    return _git(
        {
            "cat-file": (0, ""),
            f"merge-base --is-ancestor {_WHEEL_SHA} {_ADAPTER_SHA}": (0, ""),
            "merge-base": (1, ""),
        }
    )


def _wheel(
    directory,
    *,
    commit=_WHEEL_SHA,
    version=_VERSION,
    subject="release: 0.14.0",
    origin_main=None,
    stamp=True,
    metadata=True,
    name="alysis_code-0.14.0-py3-none-any.whl",
):
    """Write a wheel-shaped zip and return its path."""
    path = Path(directory) / name
    with zipfile.ZipFile(path, "w") as archive:
        if stamp:
            archive.writestr(
                "alysis_code/_build_info.py",
                'BUILD_COMMIT = "' + commit + '"\n'
                'BUILD_COMMIT_SUBJECT = "' + subject + '"\n'
                'BUILD_TIMESTAMP = "2026-08-29T09:00:00Z"\n'
                "BUILD_DIRTY = False\n"
                'BUILD_SOURCE = "git"\n'
                'BUILD_RESOLVED_ORIGIN_MAIN = "' + (origin_main or commit) + '"\n'
                "BUILD_INFO_SCHEMA_VERSION = 2\n",
            )
        if metadata:
            archive.writestr(
                "alysis_code-" + version + ".dist-info/METADATA",
                "Metadata-Version: 2.1\nName: alysis-code\nVersion: " + version + "\n\nbody\n",
            )
    return path


def _checkout(**kwargs):
    base = {
        "commit": _ADAPTER_SHA,
        "subject": "release: 0.14.0",
        "dirty": False,
        "root": "/repo",
    }
    base.update(kwargs)
    return ap.CheckoutIdentity(**base)


class ParseProjectVersionTests(unittest.TestCase):
    def test_reads_the_project_version(self) -> None:
        text = '[build-system]\nrequires = []\n\n[project]\nname = "x"\nversion = "0.14.0"\n'
        self.assertEqual(ap.parse_project_version(text), "0.14.0")

    def test_a_version_in_another_table_is_not_taken(self) -> None:
        # The failure this prevents: reporting some tool's pinned version as
        # the package's, which would make the check compare the wrong numbers.
        text = '[project]\nname = "x"\n\n[tool.other]\nversion = "9.9.9"\n'
        self.assertEqual(ap.parse_project_version(text), "")

    def test_single_quotes_are_accepted(self) -> None:
        self.assertEqual(ap.parse_project_version("[project]\nversion = '1.2.3'\n"), "1.2.3")

    def test_no_project_table_yields_nothing(self) -> None:
        self.assertEqual(ap.parse_project_version("[tool.ruff]\nversion = '1'\n"), "")

    def test_empty_input_yields_nothing(self) -> None:
        self.assertEqual(ap.parse_project_version(""), "")

    def test_the_real_pyproject_parses(self) -> None:
        text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(ap.parse_project_version(text), r"^\d+\.\d+\.\d+")


class ParseMetadataVersionTests(unittest.TestCase):
    def test_reads_the_version_header(self) -> None:
        self.assertEqual(ap.parse_metadata_version("Name: x\nVersion: 0.14.0\n"), "0.14.0")

    def test_stops_at_the_body(self) -> None:
        text = "Name: x\n\nVersion: 9.9.9 appears in the description\n"
        self.assertEqual(ap.parse_metadata_version(text), "")

    def test_a_missing_header_yields_nothing(self) -> None:
        self.assertEqual(ap.parse_metadata_version("Name: x\n"), "")


class ParseBuildInfoSourceTests(unittest.TestCase):
    def test_reads_build_assignments(self) -> None:
        values = ap.parse_build_info_source(
            'BUILD_COMMIT = "abc123def456"\nBUILD_DIRTY = False\nOTHER = "ignored"\n'
        )
        self.assertEqual(values["BUILD_COMMIT"], "abc123def456")
        self.assertIs(values["BUILD_DIRTY"], False)
        self.assertNotIn("OTHER", values)

    def test_a_corrupt_stamp_yields_nothing(self) -> None:
        self.assertEqual(ap.parse_build_info_source("BUILD_COMMIT = ((("), {})

    def test_a_computed_value_is_skipped_not_executed(self) -> None:
        # The stamp is read to decide whether the archive can be trusted, so it
        # must never be executed to find that out.
        values = ap.parse_build_info_source(
            'import os\nBUILD_COMMIT = os.system("touch /tmp/pwned")\nBUILD_SOURCE = "git"\n'
        )
        self.assertNotIn("BUILD_COMMIT", values)
        self.assertEqual(values["BUILD_SOURCE"], "git")


class CheckoutDirtyTests(unittest.TestCase):
    def test_no_output_is_clean(self) -> None:
        self.assertFalse(ap.parse_checkout_dirty(""))

    def test_blank_lines_are_clean(self) -> None:
        self.assertFalse(ap.parse_checkout_dirty("\n   \n"))

    def test_a_modified_file_is_dirty(self) -> None:
        self.assertTrue(ap.parse_checkout_dirty(" M scripts/x.py\n"))

    def test_an_untracked_file_is_dirty(self) -> None:
        # Unlike the build stamp, nothing is excluded here: a hand-edited
        # adapter usually shows up as an untracked file next to the real one.
        self.assertTrue(ap.parse_checkout_dirty("?? scripts/x_local.py\n"))

    def test_the_build_stamp_is_not_excluded_here(self) -> None:
        self.assertTrue(ap.parse_checkout_dirty(" M src/alysis_code/_build_info.py\n"))


class ResolveCheckoutIdentityTests(unittest.TestCase):
    def _runner(self, **overrides):
        responses = {
            "rev-parse --show-toplevel": (0, "/repo\n"),
            "rev-parse HEAD": (0, _ADAPTER_SHA + "\n"),
            "log": (0, "release: 0.14.0\n"),
            "status": (0, ""),
        }
        responses.update(overrides)
        return _git(responses)

    def test_a_clean_checkout_is_identified(self) -> None:
        identity = ap.resolve_checkout_identity("x.py", git_runner=self._runner())
        self.assertEqual(identity.commit, _ADAPTER_SHA)
        self.assertEqual(identity.subject, "release: 0.14.0")
        self.assertEqual(identity.root, "/repo")
        self.assertFalse(identity.dirty)
        self.assertTrue(identity.is_identifiable)

    def test_a_dirty_checkout_is_reported_dirty(self) -> None:
        identity = ap.resolve_checkout_identity(
            "x.py", git_runner=self._runner(**{"status": (0, " M a.py\n")})
        )
        self.assertTrue(identity.dirty)

    def test_a_failed_status_probe_fails_closed_to_dirty(self) -> None:
        identity = ap.resolve_checkout_identity(
            "x.py", git_runner=self._runner(**{"status": (128, "")})
        )
        self.assertTrue(identity.dirty)

    def test_no_git_yields_an_unidentifiable_checkout(self) -> None:
        identity = ap.resolve_checkout_identity("x.py", git_runner=_git({}))
        self.assertFalse(identity.is_identifiable)
        self.assertTrue(identity.dirty)
        self.assertEqual(identity.commit, "")

    def test_a_nonsense_head_is_unidentifiable(self) -> None:
        identity = ap.resolve_checkout_identity(
            "x.py", git_runner=self._runner(**{"rev-parse HEAD": (0, "HEAD\n")})
        )
        self.assertFalse(identity.is_identifiable)

    def test_a_pathological_subject_is_truncated(self) -> None:
        identity = ap.resolve_checkout_identity(
            "x.py", git_runner=self._runner(**{"log": (0, "y" * 4000)})
        )
        self.assertEqual(len(identity.subject), 200)

    def test_the_payload_is_json_serializable(self) -> None:
        payload = ap.resolve_checkout_identity("x.py", git_runner=self._runner()).payload()
        json.dumps(payload)
        self.assertEqual(payload["commit_short"], _ADAPTER_SHA[:12])


class PyprojectVersionAtTests(unittest.TestCase):
    def test_reads_the_version_at_that_commit(self) -> None:
        runner = _git({"show": (0, '[project]\nversion = "0.14.0"\n')})
        self.assertEqual(ap.pyproject_version_at(_ADAPTER_SHA, git_runner=runner), "0.14.0")
        self.assertEqual(runner.calls[-1], ["show", _ADAPTER_SHA + ":pyproject.toml"])

    def test_a_failed_show_yields_nothing(self) -> None:
        runner = _git({"show": (128, "")})
        self.assertEqual(ap.pyproject_version_at(_ADAPTER_SHA, git_runner=runner), "")

    def test_an_unidentifiable_commit_is_not_asked_about(self) -> None:
        runner = _git({"show": (0, '[project]\nversion = "9.9.9"\n')})
        self.assertEqual(ap.pyproject_version_at("", git_runner=runner), "")
        self.assertEqual(runner.calls, [])


class CommitsRelatedTests(unittest.TestCase):
    def test_the_same_commit_is_related_without_asking_git(self) -> None:
        runner = _git({})
        self.assertTrue(ap.commits_related(_WHEEL_SHA, _WHEEL_SHA, git_runner=runner))
        self.assertEqual(runner.calls, [])

    def test_case_differences_do_not_break_equality(self) -> None:
        self.assertTrue(ap.commits_related(_WHEEL_SHA.upper(), _WHEEL_SHA, git_runner=_git({})))

    def test_an_ancestor_is_related(self) -> None:
        self.assertTrue(ap.commits_related(_WHEEL_SHA, _ADAPTER_SHA, git_runner=_related_git()))

    def test_ancestry_is_checked_in_both_directions(self) -> None:
        # A wheel built from a commit the checkout has since moved past is as
        # coherent as one built from the checkout's own HEAD.
        runner = _git(
            {
                "cat-file": (0, ""),
                f"merge-base --is-ancestor {_ADAPTER_SHA} {_WHEEL_SHA}": (0, ""),
                "merge-base": (1, ""),
            }
        )
        self.assertTrue(ap.commits_related(_WHEEL_SHA, _ADAPTER_SHA, git_runner=runner))

    def test_two_fork_arms_are_not_related(self) -> None:
        self.assertFalse(ap.commits_related(_WHEEL_SHA, _ADAPTER_SHA, git_runner=_unrelated_git()))

    def test_an_unknown_commit_is_not_related(self) -> None:
        # The strongest evidence of two different trees: this checkout has
        # never heard of the commit the wheel says it was built from.
        runner = _git({"cat-file": (128, ""), "merge-base": (0, "")})
        self.assertFalse(ap.commits_related(_WHEEL_SHA, _ADAPTER_SHA, git_runner=runner))

    def test_a_missing_commit_is_not_related(self) -> None:
        self.assertFalse(ap.commits_related("", _ADAPTER_SHA, git_runner=_related_git()))
        self.assertFalse(ap.commits_related(_WHEEL_SHA, "", git_runner=_related_git()))

    def test_a_hand_written_commit_is_not_related(self) -> None:
        self.assertFalse(
            ap.commits_related("probably main", _ADAPTER_SHA, git_runner=_related_git())
        )


class ReadWheelIdentityTests(unittest.TestCase):
    def test_a_stamped_wheel_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            identity = ap.read_wheel_identity(_wheel(temp))
        self.assertTrue(identity.readable)
        self.assertEqual(identity.commit, _WHEEL_SHA)
        self.assertEqual(identity.commit_subject, "release: 0.14.0")
        self.assertEqual(identity.version, _VERSION)
        self.assertTrue(identity.is_identifiable)
        self.assertTrue(identity.is_origin_main)

    def test_a_missing_wheel_is_unreadable(self) -> None:
        identity = ap.read_wheel_identity("/no/such/wheel.whl")
        self.assertFalse(identity.readable)
        self.assertFalse(identity.is_identifiable)

    def test_an_empty_path_is_unreadable(self) -> None:
        self.assertFalse(ap.read_wheel_identity("").readable)

    def test_a_corrupt_archive_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "broken.whl"
            path.write_bytes(b"not a zip file")
            self.assertFalse(ap.read_wheel_identity(path).readable)

    def test_an_unstamped_wheel_is_readable_but_unidentifiable(self) -> None:
        # Built without running the generator: the archive is fine, it simply
        # cannot say which commit it is.
        with tempfile.TemporaryDirectory() as temp:
            identity = ap.read_wheel_identity(_wheel(temp, stamp=False))
        self.assertTrue(identity.readable)
        self.assertFalse(identity.is_identifiable)
        self.assertEqual(identity.resolved_origin_main, ap.UNAVAILABLE)

    def test_a_wheel_without_metadata_declares_no_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            identity = ap.read_wheel_identity(_wheel(temp, metadata=False))
        self.assertTrue(identity.readable)
        self.assertEqual(identity.version, "")

    def test_a_wheel_off_main_is_not_origin_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            identity = ap.read_wheel_identity(_wheel(temp, origin_main=_MAIN_SHA))
        self.assertTrue(identity.origin_main_resolved)
        self.assertFalse(identity.is_origin_main)

    def test_an_offline_wheel_cannot_claim_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            identity = ap.read_wheel_identity(_wheel(temp, origin_main=ap.UNAVAILABLE))
        self.assertFalse(identity.origin_main_resolved)
        self.assertFalse(identity.is_origin_main)

    def test_the_payload_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            json.dumps(ap.read_wheel_identity(_wheel(temp)).payload())


class GuardAgreementTests(unittest.TestCase):
    def _decide(self, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            wheel = kwargs.pop("wheel", None) or ap.read_wheel_identity(
                _wheel(temp, commit=_ADAPTER_SHA)
            )
        params = {
            "wheel": wheel,
            "checkout": _checkout(),
            "adapter_pyproject_version": _VERSION,
            "git_runner": _related_git(),
        }
        params.update(kwargs)
        return ap.decide_provenance(**params)

    def test_an_agreeing_pair_is_allowed(self) -> None:
        decision = self._decide()
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.failed)
        self.assertEqual(decision.failure_codes, ())

    def test_an_ancestor_wheel_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = ap.read_wheel_identity(_wheel(temp))
        self.assertTrue(self._decide(wheel=wheel).allowed)

    def test_the_payload_is_json_serializable(self) -> None:
        json.dumps(self._decide().payload())


class GuardCommitMismatchTests(unittest.TestCase):
    """Failure mode (a): the wheel and the adapter are different trees."""

    def _decide(self, wheel, **kwargs):
        params = {
            "wheel": wheel,
            "checkout": _checkout(),
            "adapter_pyproject_version": _VERSION,
            "git_runner": _unrelated_git(),
        }
        params.update(kwargs)
        return ap.decide_provenance(**params)

    def test_two_fork_arms_are_refused(self) -> None:
        # The historical defect: a wheel from f0b3f4be, an adapter on the
        # v0.13.1 arm, neither containing the other.
        with tempfile.TemporaryDirectory() as temp:
            decision = self._decide(ap.read_wheel_identity(_wheel(temp)))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.failure_codes, (ap.FAILURE_COMMIT_MISMATCH,))
        self.assertIn("separate lines of history", decision.failures[0].message)

    def test_an_unreadable_wheel_is_refused(self) -> None:
        decision = self._decide(ap.read_wheel_identity("/no/such.whl"))
        self.assertIn(ap.FAILURE_COMMIT_MISMATCH, decision.failure_codes)
        self.assertIn("could not be read", decision.failures[0].message)

    def test_an_unstamped_wheel_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            decision = self._decide(ap.read_wheel_identity(_wheel(temp, stamp=False)))
        self.assertIn(ap.FAILURE_COMMIT_MISMATCH, decision.failure_codes)
        self.assertIn("generate_build_info", decision.failures[0].message)

    def test_an_unidentifiable_checkout_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            decision = self._decide(
                ap.read_wheel_identity(_wheel(temp)), checkout=_checkout(commit="")
            )
        self.assertIn(ap.FAILURE_COMMIT_MISMATCH, decision.failure_codes)
        self.assertIn("cannot name its HEAD", decision.failures[0].message)

    def test_the_refusal_names_both_commits_and_how_to_proceed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            message = self._decide(ap.read_wheel_identity(_wheel(temp))).message()
        self.assertIn(_WHEEL_SHA[:12], message)
        self.assertIn(_ADAPTER_SHA[:12], message)
        self.assertIn("--provenance-override", message)
        self.assertIn("generate_build_info", message)


class GuardVersionMismatchTests(unittest.TestCase):
    """Failure mode (b): the wheel's version is not one the adapter declares."""

    def _decide(self, *, wheel_version=_VERSION, pyproject_version=_VERSION, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            wheel = ap.read_wheel_identity(_wheel(temp, commit=_ADAPTER_SHA, version=wheel_version))
        params = {
            "wheel": wheel,
            "checkout": _checkout(),
            "adapter_pyproject_version": pyproject_version,
            "git_runner": _related_git(),
        }
        params.update(kwargs)
        return ap.decide_provenance(**params)

    def test_a_differing_version_is_refused(self) -> None:
        # The tell that was missed: a 0.13.0 wheel driven by a 0.13.1 tree.
        decision = self._decide(wheel_version="0.13.0", pyproject_version="0.13.1")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.failure_codes, (ap.FAILURE_VERSION_MISMATCH,))
        self.assertIn("0.13.0", decision.failures[0].message)
        self.assertIn("0.13.1", decision.failures[0].message)

    def test_a_matching_version_passes(self) -> None:
        self.assertNotIn(ap.FAILURE_VERSION_MISMATCH, self._decide().failure_codes)

    def test_a_wheel_with_no_version_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = ap.read_wheel_identity(_wheel(temp, commit=_ADAPTER_SHA, metadata=False))
        decision = ap.decide_provenance(
            wheel=wheel,
            checkout=_checkout(),
            adapter_pyproject_version=_VERSION,
            git_runner=_related_git(),
        )
        self.assertIn(ap.FAILURE_VERSION_MISMATCH, decision.failure_codes)

    def test_an_unreadable_pyproject_is_refused(self) -> None:
        # Not knowing is not the same as agreeing.
        decision = self._decide(pyproject_version="")
        self.assertIn(ap.FAILURE_VERSION_MISMATCH, decision.failure_codes)
        self.assertIn("cannot be confirmed", decision.failures[0].message)


class GuardLatestMainTests(unittest.TestCase):
    """Failure mode (c): a latest-main claim the build cannot support."""

    def _decide(self, *, origin_main=None, latest_main=True):
        with tempfile.TemporaryDirectory() as temp:
            wheel = ap.read_wheel_identity(
                _wheel(temp, commit=_ADAPTER_SHA, origin_main=origin_main)
            )
        return ap.decide_provenance(
            wheel=wheel,
            checkout=_checkout(),
            adapter_pyproject_version=_VERSION,
            latest_main=latest_main,
            git_runner=_related_git(),
        )

    def test_a_true_claim_passes(self) -> None:
        decision = self._decide(origin_main=_ADAPTER_SHA)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.latest_main_claimed)

    def test_a_false_claim_is_refused(self) -> None:
        # Exactly the reported defect: built off a fork arm, labelled main.
        decision = self._decide(origin_main=_MAIN_SHA)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.failure_codes, (ap.FAILURE_NOT_ORIGIN_MAIN,))
        self.assertIn(_MAIN_SHA[:12], decision.failures[0].message)

    def test_an_unverifiable_claim_is_refused(self) -> None:
        decision = self._decide(origin_main=ap.UNAVAILABLE)
        self.assertEqual(decision.failure_codes, (ap.FAILURE_NOT_ORIGIN_MAIN,))
        self.assertIn("cannot be checked", decision.failures[0].message)

    def test_an_unclaimed_run_is_not_failed_for_not_being_main(self) -> None:
        decision = self._decide(origin_main=_MAIN_SHA, latest_main=False)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.latest_main_claimed)

    def test_an_unclaimed_offline_build_is_fine(self) -> None:
        self.assertTrue(self._decide(origin_main=ap.UNAVAILABLE, latest_main=False).allowed)


class GuardMultipleFailureTests(unittest.TestCase):
    def test_every_disagreement_is_reported_at_once(self) -> None:
        # An operator should learn everything that is wrong from one refusal,
        # not discover the next problem after fixing the first.
        with tempfile.TemporaryDirectory() as temp:
            wheel = ap.read_wheel_identity(_wheel(temp, version="0.13.0", origin_main=_MAIN_SHA))
        decision = ap.decide_provenance(
            wheel=wheel,
            checkout=_checkout(),
            adapter_pyproject_version="0.14.0",
            latest_main=True,
            git_runner=_unrelated_git(),
        )
        self.assertEqual(
            sorted(decision.failure_codes),
            sorted(
                [
                    ap.FAILURE_COMMIT_MISMATCH,
                    ap.FAILURE_VERSION_MISMATCH,
                    ap.FAILURE_NOT_ORIGIN_MAIN,
                ]
            ),
        )


class OverrideTests(unittest.TestCase):
    def _failed(self, reason):
        with tempfile.TemporaryDirectory() as temp:
            wheel = ap.read_wheel_identity(_wheel(temp))
        return ap.decide_provenance(
            wheel=wheel,
            checkout=_checkout(),
            adapter_pyproject_version=_VERSION,
            override_reason=reason,
            git_runner=_unrelated_git(),
        )

    def test_a_reason_allows_the_run(self) -> None:
        decision = self._failed("pinned to the export lineage for trial 3")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.overridden)

    def test_the_failure_is_recorded_not_erased(self) -> None:
        # An override says "proceed anyway", never "nothing was wrong".
        decision = self._failed("deliberate")
        self.assertTrue(decision.failed)
        self.assertEqual(decision.failure_codes, (ap.FAILURE_COMMIT_MISMATCH,))

    def test_the_reason_is_recorded_verbatim(self) -> None:
        reason = "  trial-3 waiver: wheel pinned to f0b3f4be; see notes/2026-08-26.md  "
        payload = self._failed(reason).payload()
        self.assertEqual(payload["override_reason"], reason.strip())

    def test_punctuation_and_unicode_survive(self) -> None:
        reason = 'waiver "A/B" — 50% budget, see §4'
        self.assertEqual(self._failed(reason).payload()["override_reason"], reason)

    def test_a_blank_reason_does_not_override(self) -> None:
        # Mirrors the repository's existing waiver policy: a waiver without a
        # rationale is not a waiver.
        for reason in ("", "   ", "\t\n", None):
            decision = self._failed(reason)
            self.assertFalse(decision.overridden, reason)
            self.assertFalse(decision.allowed, reason)

    def test_an_override_with_nothing_to_waive_is_not_an_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = ap.read_wheel_identity(_wheel(temp, commit=_ADAPTER_SHA))
        decision = ap.decide_provenance(
            wheel=wheel,
            checkout=_checkout(),
            adapter_pyproject_version=_VERSION,
            override_reason="not needed",
            git_runner=_related_git(),
        )
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.overridden)

    def test_normalize_strips_but_keeps_the_words(self) -> None:
        self.assertEqual(ap.normalize_override_reason("  why  "), "why")
        self.assertEqual(ap.normalize_override_reason("   "), "")
        self.assertEqual(ap.normalize_override_reason(None), "")

    def test_the_payload_carries_reason_and_codes_together(self) -> None:
        payload = self._failed("because").payload()
        json.dumps(payload)
        self.assertTrue(payload["overridden"])
        self.assertEqual(payload["override_reason"], "because")
        self.assertEqual(payload["failure_codes"], [ap.FAILURE_COMMIT_MISMATCH])


class GuardEnabledTests(unittest.TestCase):
    def test_unset_means_enabled(self) -> None:
        self.assertTrue(ap.guard_enabled(None))
        self.assertTrue(ap.guard_enabled(""))

    def test_explicit_falsey_values_disable_it(self) -> None:
        for raw in ("0", "false", "FALSE", "no", "off", " disabled "):
            self.assertFalse(ap.guard_enabled(raw), raw)

    def test_truthy_values_keep_it_on(self) -> None:
        for raw in ("1", "true", "yes", "on"):
            self.assertTrue(ap.guard_enabled(raw), raw)

    def test_an_unrecognised_value_keeps_it_on(self) -> None:
        # A typo must not silently leave a campaign unguarded.
        self.assertTrue(ap.guard_enabled("flase"))


class LatestMainClaimedTests(unittest.TestCase):
    def test_truthy_values_claim_it(self) -> None:
        for raw in ("1", "true", "YES", " on "):
            self.assertTrue(ap.latest_main_claimed(raw), raw)

    def test_anything_else_does_not(self) -> None:
        for raw in (None, "", "0", "false", "maybe"):
            self.assertFalse(ap.latest_main_claimed(raw), raw)


class BudgetResolutionTests(unittest.TestCase):
    def test_a_table_hit_is_recorded(self) -> None:
        payload = ap.BudgetResolution(
            task_name="overfull-hbox",
            seconds=2805,
            source=ap.BUDGET_SOURCE_TASK_TABLE,
            table_hit=True,
        ).payload()
        self.assertEqual(payload["budget_seconds"], 2805)
        self.assertEqual(payload["budget_source"], ap.BUDGET_SOURCE_TASK_TABLE)
        self.assertTrue(payload["budget_table_hit"])
        self.assertTrue(payload["task_identified"])

    def test_a_silent_fallback_is_visible(self) -> None:
        # The whole point: the number alone cannot say the table was missed.
        payload = ap.BudgetResolution(
            task_name="unlisted-task",
            seconds=10800,
            source=ap.BUDGET_SOURCE_FLAT_DEFAULT,
            table_hit=False,
        ).payload()
        self.assertFalse(payload["budget_table_hit"])
        self.assertEqual(payload["budget_source"], ap.BUDGET_SOURCE_FLAT_DEFAULT)

    def test_an_unidentified_task_is_visible(self) -> None:
        payload = ap.BudgetResolution().payload()
        self.assertFalse(payload["task_identified"])
        self.assertEqual(payload["task_name"], "")

    def test_the_payload_is_json_serializable(self) -> None:
        json.dumps(ap.BudgetResolution(task_name="x", seconds=1).payload())


class EvaluateAdapterProvenanceTests(unittest.TestCase):
    def test_it_reads_both_env_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = _wheel(temp, commit=_ADAPTER_SHA, origin_main=_MAIN_SHA)
            decision = ap.evaluate_adapter_provenance(
                adapter_file=str(Path(temp) / "adapter.py"),
                wheel_path=wheel,
                environ={
                    ap.LATEST_MAIN_ENV: "1",
                    ap.PROVENANCE_OVERRIDE_ENV: "known divergence",
                },
                git_runner=_git(
                    {
                        "rev-parse --show-toplevel": (0, "/repo"),
                        "rev-parse HEAD": (0, _ADAPTER_SHA),
                        "log": (0, "release: 0.14.0"),
                        "status": (0, ""),
                        "show": (0, '[project]\nversion = "0.14.0"\n'),
                    }
                ),
            )
        self.assertTrue(decision.latest_main_claimed)
        self.assertEqual(decision.failure_codes, (ap.FAILURE_NOT_ORIGIN_MAIN,))
        self.assertTrue(decision.overridden)
        self.assertEqual(decision.override_reason, "known divergence")

    def test_a_custom_getenv_is_honoured(self) -> None:
        # Harbor's adapter reads host settings through its own accessor, which
        # consults per-agent overrides before the process environment.
        seen: list[str] = []

        def getenv(key):
            seen.append(key)
            return None

        with tempfile.TemporaryDirectory() as temp:
            ap.evaluate_adapter_provenance(
                adapter_file=str(Path(temp) / "adapter.py"),
                wheel_path=_wheel(temp),
                getenv=getenv,
                git_runner=_git({}),
            )
        self.assertIn(ap.LATEST_MAIN_ENV, seen)
        self.assertIn(ap.PROVENANCE_OVERRIDE_ENV, seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
