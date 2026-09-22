"""Offline build against an installed IntelliJ Platform SDK; no machine-global installation."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
SHARED = ROOT.parent / "shared-ui"


def build(ide_home: Path) -> Path:
    ide_home = ide_home.resolve(strict=True)
    javac = ide_home / "jbr" / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    if not javac.is_file() or not (ide_home / "lib").is_dir():
        raise SystemExit("IDE SDK must include lib/ and the bundled JDK compiler in jbr/bin/.")
    build_root = ROOT / "build"
    classes = build_root / "classes"
    # Recreate only this script's resolved generated classes directory.
    if classes.exists():
        if classes.resolve().parent != build_root.resolve():
            raise SystemExit("Refusing to clean a classes directory outside this build.")
        shutil.rmtree(classes)
    classes.mkdir(parents=True)
    sources = sorted((ROOT / "src/main/java").rglob("*.java"))
    classpath = os.pathsep.join([str(ide_home / "lib" / "*"), str(ide_home / "jbr/lib" / "*")])
    subprocess.run(
        [
            str(javac),
            "--release",
            "17",
            "-encoding",
            "UTF-8",
            "-cp",
            classpath,
            "-d",
            str(classes),
            *map(str, sources),
        ],
        check=True,
    )
    jar = build_root / "alysis-code.jar"
    with ZipFile(jar, "w", ZIP_DEFLATED) as archive:
        for base in [classes, ROOT / "src/main/resources"]:
            for file in sorted(base.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(base).as_posix())
        for name in [
            "startView.html",
            "startView.css",
            "startView.js",
            "portable-chat.js",
            "THIRD_PARTY_NOTICES.txt",
        ]:
            archive.write(SHARED / name, name)
        archive.write(ROOT.parent / "vscode-alysis/resources/alysis-logo.png", "alysis-logo.png")
    target = build_root / "alysis-code-jetbrains-0.1.0-preview.zip"
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        archive.write(jar, "alysis-code/lib/alysis-code.jar")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ide-home", required=True, type=Path)
    args = parser.parse_args()
    print(build(args.ide_home))
