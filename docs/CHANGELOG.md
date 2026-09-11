# Changelog

Notable user-facing changes to Alysis Code are recorded here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) while the public API remains pre-1.0.

## [Unreleased]

No user-facing changes yet.

## [0.13.7] - 2026-09-11

### Added

- Rotating usage tips above the chat input while the agent works, with clickable links.
  Tips hide during dialogs and stay out of conversation history.

### Changed

- The context footer shows remaining conversation space after startup prompts and tools.
  Provider measurements and calibrated estimates inform the gauge, while capacity warnings
  continue to use the full request budget.
- Ctrl+J is the multiline input shortcut; Enter submits. The Alt+Enter alias is removed.
- Cleaned up public documentation and contributor guidance, and added repository hygiene
  checks and security workflows.

### Fixed

- Setup continuation hints stay visible above the footer on small terminals and long screens.
- Benchmark wheel builds preserve local build metadata and existing build artifacts, including
  when a build fails or is interrupted.

## [0.13.6.2] - 2026-09-08

### Fixed

- **Alysis Code account setup and configuration use subscription controls.**
  Setup identifies the Alysis Code endpoint without the retired MiMo label.
  Hosted profiles show Alysis Code subscription access and account sign-in,
  reconnect, and disconnect controls instead of an API-key editor.
- **Hosted model changes no longer ask for a base URL.** The managed endpoint
  stays in place through model selection and back navigation. Custom and BYOK
  providers keep their endpoint settings.
- **Switching to an Alysis Code account keeps the correct connection.** Signing
  in refreshes the full active profile before configuration is saved, preventing
  the previous provider's URL from overwriting the hosted endpoint. Login also
  switches back to the native agent from a selected delegated runtime. Account
  status reflects the Alysis login, including after logout when an unrelated
  provider key is still saved.

## [0.13.6.1] - 2026-09-08

### Fixed

- **V4.1 Flash beta on Alysis Code Pro.** The Pro model picker now includes
  `deepseek-v4.1-flash-expires-on-0910` and `deepseek-v4-flash-vision-exp`
  with descriptions. Newly discovered models are captioned "available on your
  Alysis Code Pro plan" instead of the retired MiMo "trial" wording.

## [0.13.6] - 2026-09-08

### Added

- **DeepSeek V4.1 Flash internal beta.** `deepseek-v4.1-flash-expires-on-0910`
  (announced 2026-09-08) is now on the DeepSeek preset menu. It is a
  new-architecture model with native multimodal input, served on the unchanged
  `https://api.deepseek.com` base URL under a temporary id that stops resolving
  on 2026-09-10; DeepSeek bills it at the `deepseek-v4-flash` rate and caps
  accounts at 20 concurrent requests. Registry metadata assumes the V4 Flash
  shape (1M context, 384K output, vision, thinking `low|high|max`) until
  DeepSeek publishes the GA id and limits. `deepseek-v4-pro` stays the default.

### Changed

- **DeepSeek footer labels follow vendor spelling.** Names now appear as
  `DeepSeek-V4-Pro` and `DeepSeek-V4.1-Flash`; dated beta ids omit their
  `-expires-on-<date>` suffix from the displayed label.

## [0.13.5] - 2026-09-07

### Added

- Ship eight bundled workflow skills: `address-pr-comments`, `code-review`,
  `commit`, `debug`, `fix-ci`, `release-notes`, `security-review`, and `skill-creator`.
- Select a relevant skill automatically with a bounded, reasoning-off model call;
  read its instructions before other task tools, with ordinary routing as fallback.
- Invoke skills with `$<name> [task]` at an idle chat prompt, with live completion
  and one-turn attachment. `/skill` remains supported.
- Add `bundled_skills_enabled` configuration, project and user override precedence,
  and model-in-loop bundled workflow evaluations with launch-readiness checks.

### Fixed

- Harden skill instruction boundaries, selection ordering, and per-turn cleanup.
- Preserve resolved skill settings in child agents and restore slash completion.
- Recognize silent and inline verification results and preserve authoritative
  verification evidence when supplemental checks run.
- Bound stalled OpenAI-compatible and Responses streams, including traffic that
  contains no meaningful progress.

## [0.13.4] - 2026-09-05

Model-catalog and TUI-access refresh.

### Added

- Added GPT-6 Astra as the default OpenAI model, with aliases, pricing, context-window,
  reasoning, OpenRouter, and ChatGPT-subscription metadata.
- Added newly available first-party and gateway routes for Claude Fable 5.1, Gemini 3.8
  Flash, Qwen 3.8, GLM-5.3, Kimi K3, Doubao Seed 2.1, DeepSeek V4 snapshots, Grok 4.6,
  and related hosted variants.

### Changed

- Refreshed every hosted preset's model picker, aliases, validation model, retirement
  mappings, pricing, context limits, and reasoning contracts against provider catalogs.
- Updated TUI model labels to preserve vendor naming and made profile pickers show each
  profile's model, host, active state, retired-model state, and duplicates.
- Migrated the Perplexity preset from the retiring Sonar Chat Completions endpoint to the
  Agent API, including hosted web search and saved-profile migration.
- Replaced the OpenAI economy and validation model with GPT-5.6 Luna and kept
  GPT-5.3 Codex limited to the Responses preset.

### Fixed

- OpenAI Chat Completions now uses `max_completion_tokens` for GPT-5 and newer models.
- Native Gemini tool and response schemas are projected onto Gemini's supported OpenAPI
  subset before requests are sent.

## [0.13.3] - 2026-08-28

### Fixed

- Restored the Ubuntu Python 3.11 and 3.12 CI jobs after the repository cleanup by aligning Ruff
  formatting, updating moved sandbox contract paths, preserving the security-documentation
  contract, making immediate shell-output coverage independent of thread scheduling, and allowing
  harmless floating-point clock precision in deadline coverage.

## [0.13.2] - 2026-08-28

Public launch and repository-layout refresh.

### Added

- A theme-adaptive README banner with dedicated light and dark artwork.
- `alysis-code` as the canonical executable, while retaining `alysis` as the short alias.

### Changed

- Simplified the public README around installation, core capabilities, modes, and documentation.
- Moved governance files under `.github/`, release and changelog documents under `docs/`, and
  sandbox and benchmark sources under `scripts/` for a cleaner repository root.
- Legacy Sylliptor environment and command compatibility now forwards silently while retaining
  diagnostic logging for support.

### Fixed

- Device-login approval URLs now remain pinned to the configured Alysis site instead of trusting
  a backend-provided origin.

## [0.13.1] - 2026-08-27

### Fixed

- Kept terminal input, selection, scrolling, paste hints, and theme behavior consistent across
  chat and configuration screens.
- Made intentional clean stops exit successfully while preserving non-zero exits for genuine
  failures and unknown stop reasons.
- Added bounded retries for connections interrupted during a response, with consistent deadline
  and diagnostic reporting.

## [0.13.0] - 2026-08-26

### Changed

- Renamed Sylliptor to Alysis Code. The package is now `alysis-code`, the command is `alysis`, and
  the Python module is `alysis_code`.
- Preserved the deprecated `sylliptor` command and migration support for existing configuration,
  credentials, plugins, and project directories. See the [migration guide](migration-alysis-code.md).
- Improved run-budget handling, deadline-aware shell waits, persistent services, edit diagnostics,
  sampling controls, and build provenance.

### Security

- Added redaction at log and telemetry write boundaries for credential-shaped values.

## [0.12.0] - 2026-08-18

### Added

- Added Code, Architect, Ask, and Debug personas.
- Added provider-neutral Terminal-Bench and Harbor adapters.
- Added deterministic release builds, dependency auditing, SBOM generation, and provenance
  attestations.

### Changed

- Improved evidence-based finalization, blocker reporting, provider authentication, model
  discovery, context management, and verification repair.
- Strengthened boundaries around credentials, protected paths, filesystem access, and sensitive
  tool output.

## [0.11.2] - 2026-07-27

### Added

- Added route arbitration, turn-contract enforcement, regression baselines, and evidence-backed
  completion checks.
- Added tracked process cleanup, workspace provisioning, incomplete-subagent containment, and new
  Moonshot/Kimi model presets.
- Added a master switch for web tools and search backends.

### Changed

- Refreshed the ChatGPT subscription model catalog and provider-reported final-answer handling.

## [0.11.1] - 2026-07-20

### Changed

- Redesigned the interactive configuration screen with grouped settings, clearer summaries, and
  improved provider and model selection.
- Added provider-specific reasoning controls and clearer per-role model overrides.

### Fixed

- Applied configuration changes made before the first message without restarting the terminal UI.
- Corrected input-budget calculations for shared-window models.

## [0.11.0] - 2026-07-18

### Added

- Added specialist subagent roles and capability-aware delegation.
- Added live subagent progress and result attribution to the terminal UI.
- Added an opt-in visual designer for image generation.

### Security

- Constrained child tools, execution modes, deadlines, and result handling so delegated output
  cannot change parent authority or permissions.

## [0.10.0] - 2026-07-13

### Added

- Added ChatGPT Codex subscription login, encrypted refreshable credentials, and live model
  discovery.
- Added DDGS web search as a keyless fallback and expanded hosted search support.
- Added repository mapping, focused test discovery, cached update prompts, and safer workspace
  previews.

### Changed

- Improved provider-aware prompt caching, usage accounting, terminal navigation, Forge planning,
  and OpenAI Responses streaming recovery.

## [0.9.8] - 2026-06-29

### Added

- Added stronger completion contracts and verification evidence for one-shot and managed tasks.
- Added shell waiting, durable service tools, and stricter web-fetch provenance.

### Changed

- Improved Forge verification, runtime deadlines, provider adapters, usage tracking, and recovery
  from unknown tool calls.

## Earlier releases

Release notes for versions before 0.9.8 remain available in the
[GitHub Releases archive](https://github.com/AlysisAi/alysis-code/releases).

[Unreleased]: https://github.com/AlysisAi/alysis-code/compare/v0.13.7...HEAD
[0.13.7]: https://github.com/AlysisAi/alysis-code/compare/v0.13.6.2...v0.13.7
[0.13.6.2]: https://github.com/AlysisAi/alysis-code/compare/v0.13.6.1...v0.13.6.2
[0.13.6.1]: https://github.com/AlysisAi/alysis-code/compare/v0.13.6...v0.13.6.1
[0.13.6]: https://github.com/AlysisAi/alysis-code/compare/v0.13.5...v0.13.6
[0.13.5]: https://github.com/AlysisAi/alysis-code/compare/v0.13.4...v0.13.5
[0.13.4]: https://github.com/AlysisAi/alysis-code/compare/v0.13.3...v0.13.4
[0.13.3]: https://github.com/AlysisAi/alysis-code/compare/v0.13.2...v0.13.3
[0.13.2]: https://github.com/AlysisAi/alysis-code/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/AlysisAi/alysis-code/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/AlysisAi/alysis-code/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/AlysisAi/alysis-code/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/AlysisAi/alysis-code/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/AlysisAi/alysis-code/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/AlysisAi/alysis-code/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/AlysisAi/alysis-code/compare/v0.9.8...v0.10.0
[0.9.8]: https://github.com/AlysisAi/alysis-code/releases/tag/v0.9.8
