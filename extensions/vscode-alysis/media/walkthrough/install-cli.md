# Validate the managed Alysis Code runtime

Production builds bundle a platform-specific, signed Alysis Code runtime. The extension validates its
release signature and executable hash, installs it outside your workspace, checks the IDE protocol,
and keeps a last-known-good version for rollback. You do not need to install or manage a separate CLI.

Use **Check connection** to verify the managed runtime and bridge. If validation fails, open
**Troubleshooting** and the Alysis Code output before reinstalling the extension.

`alysis.cliPath`, **Locate Alysis Code CLI**, and the pipx install command remain available only for
extension development. An executable selected that way is labeled non-production and is never
treated as release-verified.
