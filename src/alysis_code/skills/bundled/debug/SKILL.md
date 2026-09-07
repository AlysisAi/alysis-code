---
name: debug
description: Diagnose, reproduce, fix, and verify a concrete runtime/test failure as a fallback when no narrower discovered workflow governs the request.
---

Use when: The user asks to diagnose, reproduce, or resolve a concrete runtime or test failure with an observable symptom.

Do not use when: The request concerns an automated build/check failure, only explains or mentions an error, or asks for a review, refactor, or feature task.

Treat commands below as suggestions; use the normal tool and approval flow.

1. Record the exact symptom, relevant environment, and expected behavior. Read repository instructions.
2. Reproduce the failure before changing code. Prefer the smallest existing test or command that demonstrates it. If reproduction is blocked, preserve the available evidence and state the gap.
3. Isolate the cause by reducing the case, tracing inputs and state, comparing a known-good path, or adding temporary instrumentation. `git bisect` is a suggestion when history and checkout changes are appropriate.
4. Form one evidence-backed cause hypothesis at a time and test it. Do not replace diagnosis with broad cleanup.
5. If the user requested a fix, add or tighten a focused regression test and make the smallest change that addresses the cause. For diagnosis-only requests, stop after establishing the cause and do not edit files.
6. After a fix, verify the original symptom first, then run the nearest affected checks. Remove temporary instrumentation.
7. Report the root cause in one line, followed by the fix when requested, verification performed, and any remaining uncertainty.
