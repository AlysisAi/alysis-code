"""Tests for the post-run session-capture mirror.

Runnable two ways:

    python3 tests/test_session_mirror.py     # standalone, stdlib only
    pytest tests/test_session_mirror.py

The module under test is loaded directly from its file path: the adapter that
uses it imports the installed package, which is not available in a bare
interpreter.
"""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "benchmarks"
    / "terminal_bench"
    / "session_mirror.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("_session_mirror", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sm = _load_module()

# The real values the adapter passes.
SESSION_DIR = "/logs/artifacts/alysis-session"
BASE_COMMAND = (
    "mkdir -p /logs/agent 2>/dev/null; alysis run --path . --yes "
    "</dev/null 2>&1 | tee /logs/artifacts/alysis.txt /logs/agent/alysis.txt"
)


class TestMirrorEnabled(unittest.TestCase):
    def test_default_is_on_when_unset(self) -> None:
        # Opt-out: the failure it guards against is silent, and by the time it
        # is noticed the campaign's logs already do not exist.
        self.assertTrue(sm.mirror_enabled(None))
        self.assertTrue(sm.mirror_enabled(""))
        self.assertTrue(sm.mirror_enabled("   "))

    def test_explicit_on_values(self) -> None:
        for raw in ("1", "yes", "true", "on", "  1  ", "anything-else"):
            with self.subTest(raw=raw):
                self.assertTrue(sm.mirror_enabled(raw))

    def test_explicit_off_values(self) -> None:
        for raw in ("0", "false", "no", "off", "OFF", " 0 ", "False"):
            with self.subTest(raw=raw):
                self.assertFalse(sm.mirror_enabled(raw))

    def test_env_var_name(self) -> None:
        self.assertEqual(sm.MIRROR_ENV_VAR, "ALYSIS_TBENCH_MIRROR_SESSION")


class TestMirrorClause(unittest.TestCase):
    def test_exact_clause(self) -> None:
        self.assertEqual(
            sm.session_mirror_clause(SESSION_DIR),
            'ln -sfn /logs/artifacts/alysis-session "$PWD/.alysis" 2>/dev/null || true',
        )

    def test_links_the_name_the_harness_harvests(self) -> None:
        # run_harbor_tbench.sh collects `--artifact /app/.alysis`.
        self.assertEqual(sm.WORKSPACE_MIRROR_NAME, ".alysis")
        self.assertIn('"$PWD/.alysis"', sm.session_mirror_clause(SESSION_DIR))

    def test_pwd_stays_expandable(self) -> None:
        # It must be the container's working directory at run time, so $PWD
        # cannot be shell-quoted into a literal.
        clause = sm.session_mirror_clause(SESSION_DIR)
        self.assertIn("$PWD", clause)
        self.assertNotIn("'$PWD", clause)

    def test_session_dir_is_quoted(self) -> None:
        clause = sm.session_mirror_clause("/logs/weird dir/session")
        self.assertIn("'/logs/weird dir/session'", clause)

    def test_failure_is_swallowed(self) -> None:
        # Nothing to harvest must not turn a finished run into a failed one.
        self.assertTrue(sm.session_mirror_clause(SESSION_DIR).endswith("|| true"))


class TestComposeRunCommand(unittest.TestCase):
    def _composed(self) -> str:
        return sm.compose_run_command(
            BASE_COMMAND, mirror_clause=sm.session_mirror_clause(SESSION_DIR)
        )

    def test_mirror_is_present_and_ordered_after_the_run(self) -> None:
        composed = self._composed()
        self.assertIn("ln -sfn", composed)
        self.assertLess(
            composed.index("alysis run"),
            composed.index("ln -sfn"),
            "the mirror must be linked only after the agent has finished",
        )

    def test_exit_status_is_captured_before_the_mirror_and_re_raised(self) -> None:
        composed = self._composed()
        self.assertIn("__alysis_rc=$?", composed)
        self.assertLess(composed.index("__alysis_rc=$?"), composed.index("ln -sfn"))
        self.assertTrue(composed.rstrip().endswith("exit $__alysis_rc"))

    def test_disabled_mirror_leaves_the_command_untouched(self) -> None:
        for clause in (None, ""):
            with self.subTest(clause=clause):
                self.assertEqual(
                    sm.compose_run_command(BASE_COMMAND, mirror_clause=clause),
                    BASE_COMMAND,
                )

    def test_disabled_mirror_adds_no_shell_at_all(self) -> None:
        composed = sm.compose_run_command(BASE_COMMAND, mirror_clause=None)
        self.assertNotIn("ln -sfn", composed)
        self.assertNotIn("__alysis_rc", composed)

    def test_composed_command_is_parseable_shell(self) -> None:
        # A quoting mistake here would break every task in the campaign.
        composed = self._composed()
        self.assertTrue(shlex.split(composed))
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("POSIX shell is unavailable")
        result = subprocess.run(
            [shell, "-n", "-c", composed],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipIf(sys.platform.startswith("win"), "POSIX shell semantics")
class TestComposedCommandBehaviour(unittest.TestCase):
    """Execute the composed shape to prove the exit-code and link contract."""

    def _run(self, agent_exit: int, *, mirror: bool = True) -> tuple[int, Path, Path]:
        tmp = Path(tempfile.mkdtemp())
        session = tmp / "session"
        session.mkdir()
        (session / "log.jsonl").write_text("{}\n", encoding="utf-8")
        workspace = tmp / "workspace"
        workspace.mkdir()

        # Stand in for the agent pipeline: exits with the requested code.
        base = f"sh -c 'exit {agent_exit}' </dev/null 2>&1 | cat"
        clause = sm.session_mirror_clause(str(session)) if mirror else None
        composed = sm.compose_run_command(base, mirror_clause=clause)
        result = subprocess.run(
            ["sh", "-c", composed], cwd=workspace, capture_output=True, text=True
        )
        return result.returncode, workspace / ".alysis", session

    def test_mirror_appears_and_resolves_to_the_session_dir(self) -> None:
        _, link, session = self._run(0)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), session.resolve())
        self.assertTrue((link / "log.jsonl").is_file())

    def test_mirror_is_created_even_when_the_agent_failed(self) -> None:
        # A failed run's logs are exactly the ones worth harvesting.
        _, link, _ = self._run(1)
        self.assertTrue(link.is_symlink())

    def test_mirror_does_not_mask_the_exit_status(self) -> None:
        # The property that matters: mirrored and unmirrored commands agree on
        # the status in every case, so appending the mirror cannot change what
        # the harness sees. `ln` succeeding must never turn a non-zero run into
        # a zero one.
        for agent_exit in (0, 1, 3):
            with self.subTest(agent_exit=agent_exit):
                mirrored, _, _ = self._run(agent_exit, mirror=True)
                plain, _, _ = self._run(agent_exit, mirror=False)
                self.assertEqual(mirrored, plain)

    def test_status_is_the_pipeline_status_not_the_agent_status(self) -> None:
        # Documents a PRE-EXISTING property of the adapter's command, which
        # this module deliberately preserves rather than changes.
        #
        # The run is `alysis ... | tee`, and a POSIX pipeline reports the LAST
        # command's status -- tee's, which is 0 almost always. So the agent's
        # own exit code is already discarded before the mirror is appended,
        # and re-raising the captured status re-raises the pipeline's.
        #
        # Recovering the real code needs ${PIPESTATUS[0]} (or a status file),
        # which would let previously-masked failures start surfacing as
        # non-zero. That is a behaviour change well outside a capture mirror,
        # and is deliberately left alone here.
        for agent_exit in (1, 3):
            with self.subTest(agent_exit=agent_exit):
                code, _, _ = self._run(agent_exit)
                self.assertEqual(
                    code,
                    0,
                    "the tee pipeline is expected to mask the agent status; "
                    "if this now fails, the masking was fixed elsewhere and "
                    "the mirror's exit handling should be re-reviewed",
                )

    def test_nothing_is_created_when_the_mirror_is_disabled(self) -> None:
        _, link, _ = self._run(0, mirror=False)
        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())

    def test_a_missing_session_dir_does_not_fail_the_run(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        workspace = tmp / "workspace"
        workspace.mkdir()
        composed = sm.compose_run_command(
            "sh -c 'exit 0'",
            mirror_clause=sm.session_mirror_clause(str(tmp / "never-created")),
        )
        result = subprocess.run(
            ["sh", "-c", composed], cwd=workspace, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0)

    def test_relinking_over_an_existing_mirror_does_not_nest(self) -> None:
        # -n is what stops the second link being created *inside* the first.
        tmp = Path(tempfile.mkdtemp())
        session = tmp / "session"
        session.mkdir()
        workspace = tmp / "workspace"
        workspace.mkdir()
        composed = sm.compose_run_command(
            "sh -c 'exit 0'", mirror_clause=sm.session_mirror_clause(str(session))
        )
        for _ in range(2):
            subprocess.run(["sh", "-c", composed], cwd=workspace, check=False)

        link = workspace / ".alysis"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), session.resolve())
        self.assertFalse((session / ".alysis").exists())


@unittest.skipIf(sys.platform.startswith("win"), "POSIX shell semantics")
class TestAgentPipelineCommand(unittest.TestCase):
    """The tee'd run pipeline re-raises the AGENT's status, not tee's.

    Counterpart to ``test_status_is_the_pipeline_status_not_the_agent_status``
    above: that test pins what a bare ``agent | tee`` pipeline does under
    ``compose_run_command`` (masking, deliberately preserved there), this class
    proves ``agent_pipeline_command`` is the shape that removes the masking --
    portably, under plain ``sh``, with no reliance on the executor enabling
    ``pipefail``. Trial 3's exit codes survived only because Harbor 0.21.0
    happened to run the pipeline in a shell that preserved them.
    """

    def _pipeline(self, agent_exit: int, tmp: Path) -> str:
        return sm.agent_pipeline_command(
            f"sh -c 'echo agent-output; exit {agent_exit}' </dev/null",
            tee_targets=(str(tmp / "a.txt"), str(tmp / "b.txt")),
            status_file=str(tmp / "status"),
        )

    def test_agent_status_survives_tee_under_plain_sh(self) -> None:
        for agent_exit in (0, 1, 75):
            with self.subTest(agent_exit=agent_exit):
                tmp = Path(tempfile.mkdtemp())
                result = subprocess.run(
                    ["sh", "-c", self._pipeline(agent_exit, tmp)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, agent_exit)

    def test_output_still_reaches_every_tee_target(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        subprocess.run(["sh", "-c", self._pipeline(3, tmp)], capture_output=True)
        for name in ("a.txt", "b.txt"):
            self.assertEqual((tmp / name).read_text().strip(), "agent-output")

    def test_status_file_is_cleaned_up(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        subprocess.run(["sh", "-c", self._pipeline(7, tmp)], capture_output=True)
        self.assertFalse((tmp / "status").exists())

    def test_a_stale_status_file_from_an_earlier_run_never_wins(self) -> None:
        # The wrapper removes the file up front, so a leftover from a previous
        # process cannot be mistaken for this run's answer.
        tmp = Path(tempfile.mkdtemp())
        (tmp / "status").write_text("99", encoding="utf-8")
        result = subprocess.run(
            ["sh", "-c", self._pipeline(5, tmp)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 5)

    def test_an_unwritable_status_file_falls_back_to_the_pipeline_status(self) -> None:
        # The degraded case: when the status cannot be captured (here: the
        # file path is unwritable; in the field: a SIGKILL that takes the
        # subshell down before printf runs), the pipeline's own status is the
        # only truthful answer left. That is tee's 0 -- the pre-fix behaviour
        # -- so degradation reproduces the old semantics rather than
        # inventing a code or failing the shell.
        tmp = Path(tempfile.mkdtemp())
        cmd = sm.agent_pipeline_command(
            "sh -c 'exit 9' </dev/null",
            tee_targets=(str(tmp / "a.txt"),),
            status_file=str(tmp / "no-such-dir" / "status"),
        )
        result = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)

    def test_composes_with_the_mirror_without_losing_the_status(self) -> None:
        # End-to-end shape as the adapter builds it: pipeline, then mirror,
        # then the re-raise. The mirror must still appear and the agent's
        # code must still be what the harness sees.
        tmp = Path(tempfile.mkdtemp())
        session = tmp / "session"
        session.mkdir()
        workspace = tmp / "workspace"
        workspace.mkdir()
        composed = sm.compose_run_command(
            self._pipeline(75, tmp),
            mirror_clause=sm.session_mirror_clause(str(session)),
        )
        result = subprocess.run(
            ["sh", "-c", composed], cwd=workspace, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 75)
        self.assertTrue((workspace / ".alysis").is_symlink())

    def test_rejects_an_empty_tee_target_list(self) -> None:
        with self.assertRaises(ValueError):
            sm.agent_pipeline_command("sh -c 'exit 0'", tee_targets=())

    def test_command_is_valid_posix_shell(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        result = subprocess.run(
            ["sh", "-n", "-c", self._pipeline(0, tmp)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
