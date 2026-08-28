# Contributing to Alysis Code

## Development setup

- Use Python 3.11+.
- Clone the repository:

```bash
git clone https://github.com/AlysisAi/alysis-code.git
cd Alysis Code
```

- Install development dependencies:

```bash
pip install -e ".[dev]"
```

- Run tests:

```bash
pytest
```

- Lint:

```bash
ruff check . && ruff format --check .
```

## Branch naming

Use one of these branch prefixes:

- `feat/<topic>`
- `fix/<topic>`
- `chore/<topic>`
- `docs/<topic>`
- `test/<topic>`

## Pull requests

- Keep PRs small and focused.
- Link related issues.
- All CI checks must be green.
- Open one PR per logical change.

## Commit messages

Use Conventional Commits style:

- `feat:`
- `fix:`
- `chore:`
- `docs:`
- `test:`
- `refactor:`

## Code of Conduct

Please follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
