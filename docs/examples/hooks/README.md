# Hook Examples

This directory contains example command hooks and a sample hook configuration.
They demonstrate common policy patterns and are not installed automatically.

## Files

- `sample_hooks.json` shows example hook configuration.
- `block_dangerous.py`, `block_destructive_git.py`, and `block_env_files.py`
  demonstrate blocking policies.
- `format_on_write.py` demonstrates post-write automation.
- `notify_done.py` and `notify_done_macos.py` demonstrate notifications.
- `secret_scanner.py` demonstrates scanning for secret-like values.

## Notes

Project hook config is executable policy and is not trusted by default. Review
copied hooks before running `alysis hooks trust --path .`.

## See Also

- [Lifecycle hooks](../../hooks.md)
- [Security model](../../security_model.md)
