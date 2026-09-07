---
name: security-review
description: Perform a requested security review of a diff or branch and report severity-ordered findings.
---

Use when: The user explicitly requests a security review, threat-focused audit, or secure diff assessment.

Do not use when: The user requests an ordinary code review or a dependency version lookup.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Read repository security and contribution instructions. Establish the exact diff or branch range with suggested `git diff` and `git log` commands.
2. Identify changed trust boundaries, attacker-controlled inputs, privilege transitions, sensitive outputs, and dependency changes.
3. Check for exposed secrets, command or query injection, path traversal, unsafe deserialization, authentication and authorization gaps, weak validation, and insecure dependency pins.
4. Trace each suspected issue to a reachable path. Separate exploitable findings from hardening ideas and uncertain concerns.
5. Run focused static or test commands only when they materially validate a finding. Never print secret values.
6. Do not modify code, rotate credentials, or update dependencies unless the user separately asks.
7. Report findings first, ordered by severity. Include impact, attack conditions, evidence, exact `file:line`, confidence, and a concise remediation. If none are found, state residual scope and testing limits.
