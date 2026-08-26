"""Tests for build identity: stamping, reading, dirty detection, refusal.

Runnable two ways:

    python3 tests/test_build_identity.py     # standalone, stdlib only
    pytest tests/test_build_identity.py

The module under test is loaded directly from its file path so that importing
it never executes ``alysis_code/__init__`` or any of the package's
dependency-heavy import chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed -- which is also the
environment the release job stamps a tree from.

The git fixtures reproduce real ``git status --porcelain`` output, including
the rename form and the generator's own self-inflicted modification, because
those are the two cases where a naive dirty check gives the wrong answer.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "src" / "alysis_code" / "build_identity.py"
_COMMITTED_BUILD_INFO = _REPO_ROOT / "src" / "alysis_code" / "_build_info.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_build_identity", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the module defines dataclasses, and
    # ``dataclasses`` resolves annotations via ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bi = _load_module()

_SHA = "4f3a1c9e2b7d8a605fe1c3b29d47a8e0f1c2d3b4"


def _git_runner(responses: dict[str, tuple[int, str]]):
    """A fake ``git`` that answers by subcommand and records what it was asked."""
    calls: list[list[str]] = []

    def run(args):
        calls.append(list(args))
        return responses.get(args[0], (1, ""))

    run.calls = calls  # type: ignore[attr-defined]
    return run


class BuildInfoRecordTests(unittest.TestCase):
    def test_dev_default_is_unidentifiable_and_dirty(self) -> None:
        info = bi.DEV_DEFAULT_BUILD_INFO
        self.assertFalse(info.is_identifiable)
        self.assertFalse(info.is_clean)
        self.assertTrue(info.dirty)
        self.assertEqual(info.source, bi.DEV_DEFAULT_SOURCE)

    def test_a_real_commit_is_identifiable(self) -> None:
        info = bi.BuildInfo(
            commit=_SHA,
            timestamp="2026-08-21T09:14:03Z",
            dirty=False,
            source="git",
        )
        self.assertTrue(info.is_identifiable)
        self.assertTrue(info.is_clean)
        self.assertEqual(info.commit_short, _SHA[:12])

    def test_a_dirty_tree_is_never_clean(self) -> None:
        info = bi.BuildInfo(commit=_SHA, timestamp="2026-08-21T09:14:03Z", dirty=True)
        self.assertTrue(info.is_identifiable)
        self.assertFalse(info.is_clean)

    def test_a_hand_written_commit_is_rejected(self) -> None:
        # The whole point is that build identity cannot be asserted by hand.
        for fake in ("probably main", "HEAD", "latest", "", "zzzz", "12345"):
            with self.subTest(fake=fake):
                self.assertFalse(bi.BuildInfo(commit=fake).is_identifiable)

    def test_abbreviated_hashes_are_accepted(self) -> None:
        self.assertTrue(bi.BuildInfo(commit="4f3a1c9").is_identifiable)

    def test_uppercase_hashes_are_accepted(self) -> None:
        self.assertTrue(bi.BuildInfo(commit=_SHA.upper()).is_identifiable)

    def test_describe_names_every_field(self) -> None:
        described = bi.BuildInfo(
            commit=_SHA,
            timestamp="2026-08-21T09:14:03Z",
            dirty=False,
            source="git",
        ).describe()
        self.assertIn(_SHA[:12], described)
        self.assertIn("2026-08-21T09:14:03Z", described)
        self.assertIn("dirty: no", described)
        self.assertIn("source: git", described)

    def test_describe_says_unknown_rather_than_going_blank(self) -> None:
        described = bi.DEV_DEFAULT_BUILD_INFO.describe()
        self.assertIn("commit: unknown", described)
        self.assertIn("built: unknown", described)
        self.assertIn("dirty: yes", described)

    def test_telemetry_payload_is_json_serializable(self) -> None:
        payload = bi.BuildInfo(commit=_SHA, dirty=False).telemetry_payload()
        json.dumps(payload)
        self.assertTrue(payload["identifiable"])
        self.assertTrue(payload["clean"])
        self.assertEqual(payload["commit"], _SHA)


class VersionLineTests(unittest.TestCase):
    def test_version_stays_the_leading_token(self) -> None:
        # The VS Code extension, the managed-CLI smoke test and the release
        # distribution validator read this by prefix, non-emptiness and
        # substring respectively.
        line = bi.version_line("0.10.0.dev6", bi.BuildInfo(commit=_SHA, dirty=False))
        self.assertTrue(line.startswith("0.10.0.dev6"))
        self.assertIn("0.10.0.dev6", line)
        self.assertEqual(line.split()[0], "0.10.0.dev6")

    def test_version_line_is_a_single_line(self) -> None:
        line = bi.version_line("0.10.0.dev6", bi.DEV_DEFAULT_BUILD_INFO)
        self.assertEqual(len(line.splitlines()), 1)

    def test_version_line_carries_the_provenance(self) -> None:
        line = bi.version_line("0.10.0.dev6", bi.BuildInfo(commit=_SHA, dirty=True))
        self.assertIn("commit:", line)
        self.assertIn("built:", line)
        self.assertIn("dirty: yes", line)


class RenderAndReadTests(unittest.TestCase):
    def test_render_read_round_trip(self) -> None:
        info = bi.BuildInfo(
            commit=_SHA,
            timestamp="2026-08-21T09:14:03Z",
            dirty=False,
            source="git",
        )
        with tempfile.TemporaryDirectory() as temp:
            path = bi.write_build_info(info, Path(temp) / "_build_info.py")
            self.assertEqual(bi.read_build_info(path), info)

    def test_rendered_module_uses_double_quotes(self) -> None:
        # Keeps the generated file stable under the repository's ruff format.
        rendered = bi.render_build_info_module(bi.BuildInfo(commit=_SHA, source="git"))
        self.assertIn(f'BUILD_COMMIT = "{_SHA}"', rendered)
        self.assertIn('BUILD_SOURCE = "git"', rendered)
        self.assertNotIn("'", rendered.split('"""')[-1])

    def test_committed_stamp_is_exactly_the_rendered_dev_default(self) -> None:
        # If this drifts, the committed stamp was hand-edited.
        expected = bi.render_build_info_module(bi.DEV_DEFAULT_BUILD_INFO)
        actual = _COMMITTED_BUILD_INFO.read_text(encoding="utf-8")
        self.assertEqual(actual, expected)

    def test_committed_stamp_reads_back_as_the_dev_default(self) -> None:
        self.assertEqual(bi.read_build_info(_COMMITTED_BUILD_INFO), bi.DEV_DEFAULT_BUILD_INFO)

    def test_a_missing_file_reads_as_the_dev_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "_build_info.py"
            self.assertEqual(bi.read_build_info(missing), bi.DEV_DEFAULT_BUILD_INFO)

    def test_a_corrupt_file_reads_as_the_dev_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "_build_info.py"
            broken.write_text("this is not python (((", encoding="utf-8")
            self.assertEqual(bi.read_build_info(broken), bi.DEV_DEFAULT_BUILD_INFO)

    def test_a_file_that_raises_on_import_reads_as_the_dev_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            hostile = Path(temp) / "_build_info.py"
            hostile.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
            self.assertEqual(bi.read_build_info(hostile), bi.DEV_DEFAULT_BUILD_INFO)

    def test_a_partial_file_fails_closed_to_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            partial = Path(temp) / "_build_info.py"
            partial.write_text(f'BUILD_COMMIT = "{_SHA}"\n', encoding="utf-8")
            info = bi.read_build_info(partial)
            self.assertEqual(info.commit, _SHA)
            self.assertTrue(info.dirty)
            self.assertFalse(info.is_clean)

    def test_a_non_boolean_dirty_flag_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            odd = Path(temp) / "_build_info.py"
            odd.write_text(
                f'BUILD_COMMIT = "{_SHA}"\nBUILD_DIRTY = "maybe"\n',
                encoding="utf-8",
            )
            self.assertTrue(bi.read_build_info(odd).dirty)

    def test_a_string_false_dirty_flag_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            odd = Path(temp) / "_build_info.py"
            odd.write_text(
                f'BUILD_COMMIT = "{_SHA}"\nBUILD_DIRTY = "false"\n',
                encoding="utf-8",
            )
            self.assertFalse(bi.read_build_info(odd).dirty)


class DirtyStatusTests(unittest.TestCase):
    def test_empty_status_is_clean(self) -> None:
        self.assertFalse(bi.parse_dirty_status(""))
        self.assertFalse(bi.parse_dirty_status("\n  \n"))

    def test_a_modified_file_is_dirty(self) -> None:
        self.assertTrue(bi.parse_dirty_status(" M src/alysis_code/cli.py\n"))

    def test_a_staged_file_is_dirty(self) -> None:
        self.assertTrue(bi.parse_dirty_status("M  pyproject.toml\n"))

    def test_an_untracked_file_is_dirty(self) -> None:
        # An untracked, non-ignored .py file is code the artifact may execute.
        self.assertTrue(bi.parse_dirty_status("?? src/alysis_code/scratch.py\n"))

    def test_the_generated_stamp_alone_is_not_dirty(self) -> None:
        # The generator rewrites this file just before the build; counting it
        # would make every release build report itself dirty.
        self.assertFalse(bi.parse_dirty_status(" M src/alysis_code/_build_info.py\n"))

    def test_the_stamp_plus_a_real_change_is_dirty(self) -> None:
        self.assertTrue(
            bi.parse_dirty_status(" M src/alysis_code/_build_info.py\n M src/alysis_code/cli.py\n")
        )

    def test_a_rename_is_judged_on_its_destination(self) -> None:
        self.assertTrue(bi.parse_dirty_status("R  old/path.py -> new/path.py\n"))
        self.assertFalse(bi.parse_dirty_status("R  old.py -> src/alysis_code/_build_info.py\n"))


class GenerateTests(unittest.TestCase):
    def test_a_clean_tree_stamps_a_clean_build(self) -> None:
        runner = _git_runner({"rev-parse": (0, _SHA + "\n"), "status": (0, "")})
        info = bi.generate_build_info(repo_root=".", git_runner=runner, environ={})
        self.assertEqual(info.commit, _SHA)
        self.assertFalse(info.dirty)
        self.assertTrue(info.is_clean)
        self.assertEqual(info.source, bi.GENERATED_SOURCE)
        self.assertEqual([call[0] for call in runner.calls], ["rev-parse", "status"])

    def test_a_dirty_tree_stamps_a_dirty_build(self) -> None:
        runner = _git_runner({"rev-parse": (0, _SHA), "status": (0, " M cli.py\n")})
        info = bi.generate_build_info(repo_root=".", git_runner=runner, environ={})
        self.assertTrue(info.dirty)
        self.assertFalse(info.is_clean)

    def test_no_git_yields_the_dev_default(self) -> None:
        runner = _git_runner({})
        info = bi.generate_build_info(repo_root=".", git_runner=runner, environ={})
        self.assertEqual(info.commit, "")
        self.assertTrue(info.dirty)
        self.assertEqual(info.source, bi.DEV_DEFAULT_SOURCE)
        self.assertFalse(info.is_identifiable)

    def test_a_nonsense_rev_parse_yields_the_dev_default(self) -> None:
        runner = _git_runner({"rev-parse": (0, "HEAD\n"), "status": (0, "")})
        info = bi.generate_build_info(repo_root=".", git_runner=runner, environ={})
        self.assertFalse(info.is_identifiable)

    def test_a_failed_status_probe_fails_closed_to_dirty(self) -> None:
        runner = _git_runner({"rev-parse": (0, _SHA), "status": (128, "")})
        info = bi.generate_build_info(repo_root=".", git_runner=runner, environ={})
        self.assertTrue(info.dirty)

    def test_a_generated_stamp_always_carries_a_timestamp(self) -> None:
        runner = _git_runner({"rev-parse": (0, _SHA), "status": (0, "")})
        info = bi.generate_build_info(repo_root=".", git_runner=runner, environ={})
        self.assertTrue(info.timestamp.endswith("Z"))
        self.assertEqual(len(info.timestamp), 20)


class TimestampTests(unittest.TestCase):
    def test_explicit_moment_is_formatted_as_utc(self) -> None:
        moment = datetime(2026, 8, 21, 9, 14, 3, tzinfo=timezone.utc)
        self.assertEqual(bi.build_timestamp(moment, environ={}), "2026-08-21T09:14:03Z")

    def test_source_date_epoch_wins(self) -> None:
        stamped = bi.build_timestamp(
            datetime(2026, 8, 21, 9, 14, 3, tzinfo=timezone.utc),
            environ={"SOURCE_DATE_EPOCH": "0"},
        )
        self.assertEqual(stamped, "1970-01-01T00:00:00Z")

    def test_a_broken_source_date_epoch_is_ignored(self) -> None:
        moment = datetime(2026, 8, 21, 9, 14, 3, tzinfo=timezone.utc)
        self.assertEqual(
            bi.build_timestamp(moment, environ={"SOURCE_DATE_EPOCH": "yesterday"}),
            "2026-08-21T09:14:03Z",
        )

    def test_a_naive_datetime_is_treated_as_utc(self) -> None:
        self.assertEqual(
            bi.build_timestamp(datetime(2026, 8, 21, 9, 14, 3), environ={}),
            "2026-08-21T09:14:03Z",
        )


class RequireCleanBuildTests(unittest.TestCase):
    def test_env_truthy_values_request_the_refusal(self) -> None:
        for raw in ("1", "true", "TRUE", "yes", "on", "enabled", " 1 "):
            with self.subTest(raw=raw):
                self.assertTrue(
                    bi.require_clean_build_requested(environ={bi.REQUIRE_CLEAN_BUILD_ENV: raw})
                )

    def test_env_falsy_and_unknown_values_do_not(self) -> None:
        # A typo must not silently enable a refusal that then reads as a build
        # problem rather than a configuration problem.
        for raw in ("0", "false", "no", "off", "", "  ", "please"):
            with self.subTest(raw=raw):
                self.assertFalse(
                    bi.require_clean_build_requested(environ={bi.REQUIRE_CLEAN_BUILD_ENV: raw})
                )

    def test_absent_env_does_not_request_it(self) -> None:
        self.assertFalse(bi.require_clean_build_requested(environ={}))

    def test_the_flag_wins_over_a_silent_environment(self) -> None:
        self.assertTrue(bi.require_clean_build_requested(environ={}, flag=True))


class CleanBuildDecisionTests(unittest.TestCase):
    def test_unrequested_always_allows(self) -> None:
        decision = bi.decide_clean_build(info=bi.DEV_DEFAULT_BUILD_INFO, environ={})
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.required)
        self.assertEqual(decision.reason, "not_required")
        self.assertEqual(decision.message, "")

    def test_a_clean_build_is_allowed(self) -> None:
        decision = bi.decide_clean_build(
            info=bi.BuildInfo(commit=_SHA, timestamp="2026-08-21T09:14:03Z", dirty=False),
            environ={bi.REQUIRE_CLEAN_BUILD_ENV: "1"},
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.required)
        self.assertEqual(decision.reason, "clean")

    def test_a_missing_commit_is_refused(self) -> None:
        decision = bi.decide_clean_build(
            info=bi.DEV_DEFAULT_BUILD_INFO,
            environ={bi.REQUIRE_CLEAN_BUILD_ENV: "1"},
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "missing_commit")
        self.assertIn(bi.REQUIRE_CLEAN_BUILD_ENV, decision.message)
        self.assertIn("generate_build_info", decision.message)

    def test_a_dirty_build_is_refused(self) -> None:
        decision = bi.decide_clean_build(
            info=bi.BuildInfo(commit=_SHA, timestamp="2026-08-21T09:14:03Z", dirty=True),
            environ={bi.REQUIRE_CLEAN_BUILD_ENV: "1"},
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "dirty_tree")
        self.assertIn(_SHA[:12], decision.message)

    def test_a_refusal_explains_how_to_proceed(self) -> None:
        for info in (
            bi.DEV_DEFAULT_BUILD_INFO,
            bi.BuildInfo(commit=_SHA, dirty=True),
        ):
            with self.subTest(commit=info.commit):
                decision = bi.decide_clean_build(
                    info=info,
                    environ={bi.REQUIRE_CLEAN_BUILD_ENV: "1"},
                )
                self.assertIn("unset", decision.message)

    def test_decision_payload_is_json_serializable(self) -> None:
        decision = bi.decide_clean_build(
            info=bi.DEV_DEFAULT_BUILD_INFO,
            environ={bi.REQUIRE_CLEAN_BUILD_ENV: "1"},
        )
        json.dumps(decision.payload())
        self.assertEqual(
            set(decision.payload()),
            {"required", "allowed", "reason"},
        )


class DefaultPathTests(unittest.TestCase):
    def test_default_path_points_at_the_committed_stamp(self) -> None:
        self.assertEqual(bi.default_build_info_path(), _COMMITTED_BUILD_INFO.resolve())

    def test_process_cache_is_resettable(self) -> None:
        bi.reset_build_info_cache_for_tests()
        first = bi.load_build_info()
        self.assertIs(bi.load_build_info(), first)
        bi.reset_build_info_cache_for_tests()


class IdempotenceTests(unittest.TestCase):
    """Rendering and decision-making are pure: replaying gives the same answer."""

    def test_rendering_is_stable(self) -> None:
        info = bi.BuildInfo(commit=_SHA, timestamp="2026-08-21T09:14:03Z", dirty=False)
        self.assertEqual(bi.render_build_info_module(info), bi.render_build_info_module(info))

    def test_decisions_are_stable(self) -> None:
        environ = {bi.REQUIRE_CLEAN_BUILD_ENV: "1"}
        first = bi.decide_clean_build(info=bi.DEV_DEFAULT_BUILD_INFO, environ=environ)
        second = bi.decide_clean_build(info=bi.DEV_DEFAULT_BUILD_INFO, environ=environ)
        self.assertEqual(first, second)

    def test_a_written_stamp_rewrites_identically(self) -> None:
        info = bi.BuildInfo(commit=_SHA, timestamp="2026-08-21T09:14:03Z", dirty=False)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "_build_info.py"
            bi.write_build_info(info, path)
            once = path.read_text(encoding="utf-8")
            bi.write_build_info(bi.read_build_info(path), path)
            self.assertEqual(path.read_text(encoding="utf-8"), once)


if __name__ == "__main__":
    unittest.main(verbosity=2)
