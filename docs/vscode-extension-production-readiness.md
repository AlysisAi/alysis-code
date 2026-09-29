# VS Code production readiness

The IDE source and component checks are available. Production runtime signing, exact-candidate
installation and acceptance, and Marketplace publication remain separate release gates.

Follow [the current release status](vscode-marketplace-launch.md) and
[release checklist](vscode_extension_release_checklist.md). Record final acceptance in the
[production signoff template](vscode_extension_production_signoff_template.md).

A component VSIX without a production signed runtime is not publishable. A JetBrains development
preview does not establish native JCEF compatibility or JetBrains Marketplace readiness.
