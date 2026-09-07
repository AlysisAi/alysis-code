#!/bin/sh
set -eu
git init -q
git config user.name "Skills Eval"
git config user.email "skills-eval@example.invalid"
printf '%s\n' '# Bundled Commit Fixture' > README.md
git add README.md
git commit -qm "chore: initialize fixture"
printf '%s\n' 'retry_limit = 3' > settings.toml
printf '%s\n' 'tmp trace line' > debug.log
