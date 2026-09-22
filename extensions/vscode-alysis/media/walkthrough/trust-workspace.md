# Trust this workspace

Alysis Code follows VS Code Workspace Trust.

In an **untrusted** workspace the extension stays read-only: it will check bridge health and run
non-mutating previews, but it will not start write-capable Forge runs, forward your API key, or run
a workspace-local CLI binary.

Grant trust from the banner VS Code shows when you open a folder, or from
**Workspaces: Manage Workspace Trust**. Only trust folders whose contents you recognize.
