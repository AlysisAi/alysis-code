"""Build and cross the shared UI/Java/Python boundary using an isolated deterministic agent."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from build import ROOT, build


def run(ide_home: Path) -> None:
    build(ide_home)
    repo = ROOT.parent.parent
    java_bin = ide_home / "jbr/bin"
    suffix = ".exe" if os.name == "nt" else ""
    tests = ROOT / "build/test-classes"
    tests.mkdir(exist_ok=True)
    classpath = os.pathsep.join(
        map(str, [ROOT / "build/classes", ide_home / "lib/*", ide_home / "jbr/lib/*", tests])
    )
    subprocess.run(
        [
            str(java_bin / f"javac{suffix}"),
            "--release",
            "17",
            "-encoding",
            "UTF-8",
            "-cp",
            classpath,
            "-d",
            str(tests),
            str(ROOT / "test/TransportHarness.java"),
        ],
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="alysis-jetbrains-test-") as directory:
        temporary = Path(directory)
        workspace = temporary / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("# Disposable protocol test\n", encoding="utf-8")
        env = dict(
            os.environ,
            PYTHONPATH=str(repo / "src"),
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            ALYSIS_CONFIG_DIR=str(temporary / "config"),
            ALYSIS_DATA_DIR=str(temporary / "data"),
            ALYSIS_TERMINAL_OWNERSHIP_DIR=str(temporary / "terminals"),
        )
        subprocess.run(
            [
                "node",
                str(ROOT / "test/ui-transport-smoke.cjs"),
                str(java_bin / f"java{suffix}"),
                classpath,
                sys.executable,
                str(workspace),
            ],
            env=env,
            check=True,
            timeout=120,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ide-home", required=True, type=Path)
    run(parser.parse_args().ide_home.resolve())
