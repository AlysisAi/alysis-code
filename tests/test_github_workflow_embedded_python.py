from __future__ import annotations

import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PYTHON_HEREDOC = re.compile(r"\bpython(?:3)?\s+-\s+<<-?'?(?P<marker>PY(?:THON)?)'?\s*$")


def _embedded_python(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        match = PYTHON_HEREDOC.search(lines[index])
        if match is None:
            index += 1
            continue
        marker = match.group("marker")
        start = index + 2
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != marker:
            body.append(lines[index])
            index += 1
        if index == len(lines):
            raise AssertionError(f"{path.name}:{start}: unterminated {marker} heredoc")
        blocks.append((start, textwrap.dedent("\n".join(body)) + "\n"))
        index += 1
    return blocks


def test_every_github_workflow_embedded_python_block_compiles() -> None:
    failures: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, source in _embedded_python(workflow):
            try:
                compile(source, f"{workflow.name}:{line_number}", "exec")
            except SyntaxError as error:
                failures.append(
                    f"{workflow.name}:{line_number + (error.lineno or 1) - 1}: {error.msg}"
                )
    assert not failures, "Embedded workflow Python is invalid:\n" + "\n".join(failures)
