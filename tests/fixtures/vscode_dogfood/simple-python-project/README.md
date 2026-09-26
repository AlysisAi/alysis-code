# Alysis Code VS Code Dogfood Fixture

This is a disposable, no-dependency Python project for VS Code extension dogfood runs.

## Verification

Run:

```bash
python -m unittest discover -s tests
```

## Expected Dogfood Task

Ask Alysis Code:

```text
Add a pure clamp(value, lower, upper) function to src/simple_math.py and cover it with tests.
```

The expected implementation should not need network access, external dependencies, generated files,
or secrets.
