import { redactForDisplay } from "../client/CliDiscovery";
import { COMMANDS } from "../commands/registry";
import { CockpitAction, TRUST_MANAGEMENT_COMMAND, cockpitAction } from "./CockpitReadiness";

/**
 * One user-facing failure class. The union is the shared host/webview contract: the renderer keys
 * its treatment off `kind` and never off backend prose, so a reworded backend sentence can never
 * change which recovery a user is offered.
 */
export type ChatErrorKind =
  | "auth_invalid"
  | "rate_limited"
  | "quota_exhausted"
  | "network_unreachable"
  | "context_overflow"
  | "runtime_missing"
  | "cli_outdated"
  | "extension_outdated"
  | "workspace_untrusted"
  | "sandbox_unavailable"
  | "checkpoint_unavailable"
  | "cancelled"
  | "interrupted"
  | "unknown";

/**
 * The redacted, plain-language projection of one failure. `title`/`detail` never carry a protocol
 * code, HTTP status, or stack frame — the raw text stays in the Output channel and the runtime
 * diagnostics feed, which is where diagnostics belong.
 */
export interface ClassifiedChatError {
  kind: ChatErrorKind;
  title: string;
  detail: string;
  actions: CockpitAction[];
  /** Only set when the backend actually told us how long to wait. */
  retryAfterSeconds?: number;
}

export interface ChatErrorContext {
  /** Managed-runtime origin (`managed`, `unavailable`, ...) when the host knows it. */
  runtimeOrigin?: string;
  /** Which side of a protocol mismatch is behind, when the bridge reported one. */
  protocolDirection?: "extension_outdated" | "cli_outdated" | "unknown";
}

/** This build ships no managed runtime bundle. Recoverable: installing or repairing one fixes it. */
export const BUNDLED_RUNTIME_MISSING_CODE = "BUNDLED_RUNTIME_MISSING";
/** A bundle exists but failed verification. NOT recoverable by retrying — it needs a reinstall. */
export const BUNDLED_RUNTIME_INVALID_CODE = "BUNDLED_RUNTIME_INVALID";

/** Failure codes that mean "no Alysis Code runtime is available to run anything". */
export const RUNTIME_MISSING_ERROR_CODES: readonly string[] = [
  "cli_missing",
  "cli_runtime_unavailable",
  BUNDLED_RUNTIME_MISSING_CODE,
  "unsupported_transport"
];

/** Present-but-untrustworthy managed bundle. Recoverable only by repairing the installation. */
export const RUNTIME_INVALID_ERROR_CODES: readonly string[] = [BUNDLED_RUNTIME_INVALID_CODE];

const CLI_OUTDATED_ERROR_CODES: readonly string[] = [
  "unsupported_protocol_version",
  "incompatible_bridge",
  "ide_bridge_missing",
  "method_not_found"
];

const CANCELLED_ERROR_CODES: readonly string[] = [
  "cancelled",
  "canceled",
  "job_cancelled",
  "run_cancelled",
  "operation_cancelled",
  "cancellation_requested"
];

const AUTH_ERROR_CODES: readonly string[] = [
  "auth_error",
  "auth_failed",
  "authentication_error",
  "invalid_api_key",
  "unauthorized",
  "forbidden",
  "permission_denied",
  "credentials_missing",
  "credentials_invalid"
];

const RATE_LIMIT_ERROR_CODES: readonly string[] = ["rate_limited", "rate_limit_exceeded", "too_many_requests"];

const QUOTA_ERROR_CODES: readonly string[] = [
  "quota_exceeded",
  "quota_exhausted",
  "insufficient_quota",
  "credit_balance_exhausted",
  "organization_spend_limit_exceeded",
  "project_spend_limit_exceeded",
  "organization_usage_limit_exceeded",
  "billing_error",
  "payment_required"
];

const NETWORK_ERROR_CODES: readonly string[] = [
  "network_error",
  "transport_error",
  "connection_failed",
  "connection_refused",
  "request_timeout",
  "timeout",
  "ENOTFOUND",
  "ECONNREFUSED",
  "ECONNRESET",
  "ETIMEDOUT",
  "EAI_AGAIN",
  "EHOSTUNREACH",
  "ENETUNREACH"
];

const CONTEXT_OVERFLOW_ERROR_CODES: readonly string[] = [
  "context_length_exceeded",
  "context_overflow",
  "prompt_too_long",
  "max_tokens_exceeded"
];

const TRUST_ERROR_CODES: readonly string[] = ["cli_untrusted", "workspace_untrusted"];

const AUTH_MESSAGE_PATTERN =
  /\b(?:401|403)\b|invalid[ _-]?api[ _-]?key|incorrect api key|api key (?:is )?(?:missing|invalid|expired|not valid)|unauthori[sz]ed|authentication (?:failed|error)|not authenticated|no api key|permission denied/i;
const RATE_LIMIT_MESSAGE_PATTERN = /\b429\b|rate[ _-]?limit|too many requests|slow down/i;
const QUOTA_MESSAGE_PATTERN =
  /\b402\b|quota|insufficient[ _-]?(?:quota|credit|funds|balance)|(?:out of|no(?: remaining)?) credits?\b|credit(?:s| balance)? (?:is |are )?(?:exhausted|depleted)|billing|payment required|subscription (?:expired|inactive)|hard limit/i;
const NETWORK_MESSAGE_PATTERN =
  /ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|EHOSTUNREACH|ENETUNREACH|socket hang up|fetch failed|network (?:is )?(?:error|unreachable|failure)|offline|dns (?:lookup )?fail|could not (?:connect|reach)|connection (?:refused|reset|timed out)|proxy/i;
const CONTEXT_OVERFLOW_MESSAGE_PATTERN =
  /context (?:length|window|limit)|maximum context|too many tokens|prompt is too long|input is too long|reduce the length of the messages/i;
const TRUST_MESSAGE_PATTERN = /workspace trust|untrusted workspace|trust this (?:folder|workspace)/i;
const CANCELLED_MESSAGE_PATTERN = /\bcancell?ed\b|cancellation (?:requested|completed)|aborted by user/i;
const RUNTIME_MISSING_MESSAGE_PATTERN =
  /could not be found or launched|is not installed|no such file or directory|spawn .* enoent|command not found|cli could not be located/i;
const EXTENSION_OUTDATED_MESSAGE_PATTERN = /newer than this extension|update the alysis vs code extension/i;
const RETRY_AFTER_MESSAGE_PATTERN = /retry (?:again )?(?:after|in)\s+(\d{1,5})\s*(seconds?|secs?|s\b|minutes?|mins?|m\b)?/i;

const MAX_DETAIL_CHARS = 200;
const MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60;

/**
 * Classify one failure into the shared taxonomy. Structural signals (error code, protocol direction)
 * win over prose; prose matching is the last resort so an unclassified backend sentence still lands
 * as `unknown` with its codes and stack frames stripped rather than pasted into the card.
 */
export function classifyChatError(error: unknown, context: ChatErrorContext = {}): ClassifiedChatError {
  const code = errorCode(error);
  const message = errorMessage(error);
  const details = errorDetails(error);

  if (code === "checkpoint_unavailable" || /\bcheckpoint_unavailable\b/.test(message)) {
    return {
      kind: "checkpoint_unavailable",
      title: "Restore snapshots need attention",
      detail: "This action was paused before it could change files because restore snapshots are not ready. File reading and Git review remain available. Wait a moment, then send your request again. If this keeps happening, check the setup and storage permissions.",
      actions: [cockpitAction("Check setup", COMMANDS.runDoctor)]
    };
  }

  if (code === "sandbox_unavailable" ||
    /failed to connect to the docker API|cannot connect to the docker daemon|docker is (?:installed, but it is not running|not installed)|no usable sandbox backend/i.test(message)) {
    const dockerStopped = /failed to connect to the docker API|cannot connect to the docker daemon|docker is installed, but it is not running/i.test(message);
    return {
      kind: "sandbox_unavailable",
      title: "Command execution needs setup",
      detail: dockerStopped
        ? "Docker is not reachable. Start Docker on the computer running this workspace, then retry. File reading and Git review remain available."
        : "The command runner is unavailable. Check the workspace setup to enable shell commands and tests. File reading and Git review remain available.",
      actions: [
        cockpitAction("Check setup", COMMANDS.runDoctor, { primary: true }),
        cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
      ]
    };
  }

  if (code === "interrupted_indeterminate" || (!code && /\binterrupted_indeterminate\b/i.test(message))) {
    return {
      kind: "interrupted",
      title: "This task was interrupted",
      detail: "Some actions may have finished before the interruption. Review your files, then send the request again. Pending approvals need a fresh review.",
      actions: []
    };
  }

  if (matchesCode(code, CANCELLED_ERROR_CODES) || (!code && CANCELLED_MESSAGE_PATTERN.test(message))) {
    return {
      kind: "cancelled",
      title: "Task stopped",
      detail: "Alysis Code stopped this task at your request.",
      actions: []
    };
  }

  if (matchesCode(code, RUNTIME_INVALID_ERROR_CODES)) {
    return {
      kind: "runtime_missing",
      title: "Setup needs attention",
      detail: "The Alysis Code engine that ships with this extension did not pass verification, so it was not started.",
      actions: [
        cockpitAction("Reinstall guide", COMMANDS.openSetupGuide, { primary: true }),
        cockpitAction("Check connection", COMMANDS.showBridgeHealth)
      ]
    };
  }

  if (
    matchesCode(code, RUNTIME_MISSING_ERROR_CODES) ||
    (!code && RUNTIME_MISSING_MESSAGE_PATTERN.test(message))
  ) {
    return {
      kind: "runtime_missing",
      title: "Locate the CLI on this computer",
      detail: "Alysis Code could not find or start its local engine, so the task never began.",
      actions: runtimeMissingActions(context)
    };
  }

  if (
    context.protocolDirection === "extension_outdated" ||
    EXTENSION_OUTDATED_MESSAGE_PATTERN.test(message)
  ) {
    return {
      kind: "extension_outdated",
      title: "An update is needed",
      detail: "The local Alysis Code engine is newer than this extension, so this VS Code extension needs updating.",
      actions: [
        cockpitAction("Update extension", COMMANDS.checkForUpdates, { primary: true }),
        cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
      ]
    };
  }

  if (matchesCode(code, CLI_OUTDATED_ERROR_CODES) || isFeatureCompatibilityError(error)) {
    return {
      kind: "cli_outdated",
      title: "An update is needed",
      detail: "The local Alysis Code engine is older than this extension and cannot run this request yet.",
      actions: [
        cockpitAction("Copy upgrade command", COMMANDS.copyCliUpgradeCommand, { primary: true }),
        cockpitAction("Open setup guide", COMMANDS.openSetupGuide),
        cockpitAction("Check connection", COMMANDS.showBridgeHealth)
      ]
    };
  }

  if (matchesCode(code, TRUST_ERROR_CODES) || TRUST_MESSAGE_PATTERN.test(message)) {
    return {
      kind: "workspace_untrusted",
      title: "Limited until you trust this folder",
      detail: "Alysis Code needs Workspace Trust before it can run this kind of work in this folder.",
      actions: [cockpitAction("Trust this folder", TRUST_MANAGEMENT_COMMAND, { primary: true })]
    };
  }

  if (matchesCode(code, AUTH_ERROR_CODES) || AUTH_MESSAGE_PATTERN.test(message)) {
    return {
      kind: "auth_invalid",
      title: "Your API key was rejected",
      detail: "The provider refused the stored key, so nothing was sent for this task.",
      actions: [
        cockpitAction("Update API key", COMMANDS.configureProvider, { primary: true }),
        cockpitAction("Review provider", COMMANDS.showModels)
      ]
    };
  }

  // Providers also use HTTP 429 for exhausted allowance. Billing recovery takes precedence over
  // that status, while an explicit rate-limit code still wins over ambiguous quota prose.
  if (matchesCode(code, QUOTA_ERROR_CODES) ||
    (!matchesCode(code, RATE_LIMIT_ERROR_CODES) && QUOTA_MESSAGE_PATTERN.test(message))) {
    return {
      kind: "quota_exhausted",
      title: "This provider has no available allowance",
      detail: "The provider reports exhausted credits or an account limit. Check API credits and limits in its billing settings, or connect a funded provider.",
      actions: [
        cockpitAction("Switch provider", COMMANDS.configureProvider, { primary: true }),
        cockpitAction("Review provider", COMMANDS.showModels)
      ]
    };
  }

  if (matchesCode(code, RATE_LIMIT_ERROR_CODES) || RATE_LIMIT_MESSAGE_PATTERN.test(message)) {
    const retryAfterSeconds = retryAfterFrom(details, message);
    return {
      kind: "rate_limited",
      title: "The provider asked Alysis Code to slow down",
      detail: "This provider is limiting how often requests can be sent right now.",
      actions: [cockpitAction("Choose another model", COMMANDS.showModels)],
      ...(retryAfterSeconds === undefined ? {} : { retryAfterSeconds })
    };
  }

  if (matchesCode(code, NETWORK_ERROR_CODES) || NETWORK_MESSAGE_PATTERN.test(message)) {
    return {
      kind: "network_unreachable",
      title: "Alysis Code could not reach the network",
      detail: "The provider or local engine could not be reached from this computer.",
      actions: [cockpitAction("Check connection", COMMANDS.showBridgeHealth, { primary: true })]
    };
  }

  if (matchesCode(code, CONTEXT_OVERFLOW_ERROR_CODES) || CONTEXT_OVERFLOW_MESSAGE_PATTERN.test(message)) {
    return {
      kind: "context_overflow",
      title: "This conversation is too long",
      detail: "The request exceeded how much context the selected model can read at once.",
      actions: [
        cockpitAction("Compact this task", COMMANDS.sessionCompact, { primary: true }),
        cockpitAction("Start a new task", COMMANDS.newSession)
      ]
    };
  }

  return {
    kind: "unknown",
    title: "Alysis Code could not finish that",
    detail: plainDetail(message),
    actions: [cockpitAction("Check connection", COMMANDS.showBridgeHealth)]
  };
}

function runtimeMissingActions(context: ChatErrorContext): CockpitAction[] {
  // A managed build owns its signed runtime; pointing those users at a separate pipx executable would
  // silently move them off the verified lifecycle, so they get repair actions instead.
  if (context.runtimeOrigin === "managed" || context.runtimeOrigin === "unavailable") {
    return [
      cockpitAction("Check connection", COMMANDS.showBridgeHealth, { primary: true }),
      cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
    ];
  }
  return [
    cockpitAction("Locate the CLI", COMMANDS.locateCli, { primary: true }),
    cockpitAction("Copy install command", COMMANDS.copyCliInstallCommand),
    cockpitAction("Open setup guide", COMMANDS.openSetupGuide)
  ];
}

function matchesCode(code: string, codes: readonly string[]): boolean {
  if (!code) {
    return false;
  }
  const normalized = code.toLowerCase();
  return codes.some((candidate) => candidate.toLowerCase() === normalized);
}

function isFeatureCompatibilityError(error: unknown): boolean {
  return error instanceof Error && error.name === "FeatureCompatibilityError";
}

function errorCode(error: unknown): string {
  if (!error || typeof error !== "object") {
    return "";
  }
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code.trim() : "";
}

function errorDetails(error: unknown): Record<string, unknown> | undefined {
  if (!error || typeof error !== "object") {
    return undefined;
  }
  const details = (error as { details?: unknown }).details;
  return details && typeof details === "object" && !Array.isArray(details)
    ? (details as Record<string, unknown>)
    : undefined;
}

function errorMessage(error: unknown): string {
  if (typeof error === "string") {
    return error;
  }
  if (error instanceof Error) {
    return error.message;
  }
  if (error && typeof error === "object") {
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string") {
      return message;
    }
  }
  return error === undefined || error === null ? "" : String(error);
}

function retryAfterFrom(details: Record<string, unknown> | undefined, message: string): number | undefined {
  const candidates = [
    details?.retry_after_seconds,
    details?.retry_after,
    details?.retryAfterSeconds,
    details?.retryAfter
  ];
  for (const candidate of candidates) {
    const seconds = boundedSeconds(candidate);
    if (seconds !== undefined) {
      return seconds;
    }
  }
  const match = RETRY_AFTER_MESSAGE_PATTERN.exec(message);
  if (!match) {
    return undefined;
  }
  const unit = (match[2] ?? "s").toLowerCase();
  const multiplier = unit.startsWith("m") && !unit.startsWith("ms") ? 60 : 1;
  return boundedSeconds(Number.parseInt(match[1], 10) * multiplier);
}

function boundedSeconds(value: unknown): number | undefined {
  const numeric = typeof value === "number" ? value : typeof value === "string" ? Number.parseFloat(value) : Number.NaN;
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return undefined;
  }
  return Math.min(Math.ceil(numeric), MAX_RETRY_AFTER_SECONDS);
}

/**
 * Last-resort detail for an unclassified failure: redacted, stripped of trailing protocol codes,
 * HTTP statuses, and stack frames, then reduced to one bounded sentence. Never a raw backend dump.
 */
export function plainDetail(message: string): string {
  const redacted = redactForDisplay(message);
  const withoutFrames = redacted
    .split(/\r?\n/)
    .filter((line) => !/^\s*at\s+\S+/.test(line))
    .join(" ");
  const withoutCodes = withoutFrames
    // Trailing/embedded machine codes: "(config_error)", "(429)".
    .replace(/\s*\((?:[a-z][a-z0-9_.:-]{1,63}|\d{3})\)\s*/gi, " ")
    // Explicit HTTP statuses ("HTTP 429", "status code: 401") and bare 4xx/5xx tokens.
    .replace(/\bhttp\s*[1-5]\d{2}\b/gi, " ")
    .replace(/\bstatus(?:\s*code)?\s*[:=]?\s*[1-5]\d{2}\b/gi, " ")
    .replace(/\b[45]\d{2}\b/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
  const firstSentence = /^[^.!?]*[.!?]/.exec(withoutCodes)?.[0] ?? withoutCodes;
  const bounded = firstSentence.trim().slice(0, MAX_DETAIL_CHARS).trim();
  if (!bounded) {
    return "Alysis Code could not complete the request. Check the Alysis Code output for details.";
  }
  return /[.!?]$/.test(bounded) ? bounded : `${bounded}.`;
}
