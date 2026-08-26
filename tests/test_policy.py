from __future__ import annotations

import pytest

from alysis_code.policy import evaluate_shell_command


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",
        "rm -fr /",
        "rm -r -f /",
        "rm -f -r /",
        "rm -rfv /tmp/test",
        "rm -vfr /tmp/test",
    ],
)
def test_policy_blocks_destructive_rm_variants(cmd: str) -> None:
    d = evaluate_shell_command(cmd)
    assert d.allowed is False


def test_policy_confirms_git_push() -> None:
    d = evaluate_shell_command("git push origin main")
    assert d.allowed is True
    assert d.needs_confirm is True


def test_policy_allows_safe_rm_without_recursive_force_combo() -> None:
    d = evaluate_shell_command("rm -f file.txt")
    assert d.allowed is True
    assert d.needs_confirm is False
