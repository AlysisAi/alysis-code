import { createHash } from "node:crypto";
import { lookup } from "node:dns/promises";
import {
  closeSync,
  lstatSync,
  openSync,
  readSync,
  readdirSync
} from "node:fs";
import { resolve } from "node:path";
import { BlockList, isIP } from "node:net";

const ENVIRONMENT_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const SAFE_EVIDENCE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._/@:+ ()-]*$/;
const SCAN_CHUNK_BYTES = 64 * 1024;
const QA_RUNTIME_ENVIRONMENT = new Set(
  [
    "ALLUSERSPROFILE",
    "APPDATA",
    "CI",
    "COLORTERM",
    "COMSPEC",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "FORCE_COLOR",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "LOGNAME",
    "NODE_EXTRA_CA_CERTS",
    "NO_COLOR",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "ALYSIS_CONFIG_DIR",
    "ALYSIS_DATA_DIR",
    "ALYSIS_LIVE_CLI_PATH",
    "ALYSIS_LIVE_PREFLIGHT",
    "ALYSIS_LIVE_QA_WORKSPACE",
    "ALYSIS_LIVE_SOAK",
    "ALYSIS_LIVE_SOAK_DURATION_MS",
    "ALYSIS_LIVE_SOAK_SMOKE",
    "ALYSIS_INSTALLED_PRODUCTION_QA_ONCE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "VSCODE_TEST_EXECUTABLE_PATH",
    "VSCODE_TEST_VERSION",
    "WAYLAND_DISPLAY",
    "WINDIR",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
    "XAUTHORITY",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "__CF_USER_TEXT_ENCODING"
  ].map((name) => name.toUpperCase())
);

export interface SafeTextEvidence {
  length: number;
  sha256: string;
}

export interface SafeErrorEvidence extends SafeTextEvidence {
  name: string;
}

export interface ValidatedLiveQaProviderEndpoint {
  baseUrl: string;
  origin: string;
}

/** Keep retained provider/model identifiers compact and incapable of carrying credential syntax. */
export function validateLiveQaEvidenceIdentifier(
  value: string,
  label: string,
  maximumLength: number
): string {
  const normalized = value.trim();
  if (
    normalized.length === 0 ||
    normalized.length > maximumLength ||
    !SAFE_EVIDENCE_IDENTIFIER.test(normalized) ||
    /(?:api[_ -]?key|access[_ -]?token|bearer|secret)\s*[:=]/i.test(normalized) ||
    /(?:^|\s)sk-[A-Za-z0-9_-]{16,}(?:\s|$)/.test(normalized)
  ) {
    throw new Error(`${label} is not safe retained evidence.`);
  }
  return normalized;
}

type ResolveProviderAddresses = (hostname: string) => Promise<readonly string[]>;

const NON_PUBLIC_PROVIDER_ADDRESSES = buildNonPublicProviderBlockList();

/**
 * Validate the reviewed provider endpoint before a live credential is consumed. The configured
 * base URL may include an API path, but its HTTPS origin must exactly match the independently
 * reviewed origin and every resolved address must be globally routable.
 */
export async function validateLiveQaProviderEndpoint(
  baseUrl: string,
  approvedOrigin: string,
  resolveAddresses: ResolveProviderAddresses = async (hostname) =>
    (await lookup(hostname, { all: true, verbatim: true })).map((entry) => entry.address)
): Promise<ValidatedLiveQaProviderEndpoint> {
  const endpoint = parseProviderUrl(baseUrl, "Live QA provider base URL", true);
  const approved = parseProviderUrl(approvedOrigin, "Live QA approved provider origin", false);
  if (endpoint.origin !== approved.origin) {
    throw new Error("Live QA provider base URL does not match the approved HTTPS origin.");
  }
  const hostname = unbracketHostname(endpoint.hostname);
  rejectLocalProviderHostname(hostname);
  const literalFamily = isIP(hostname);
  const addresses = literalFamily ? [hostname] : await resolveAddresses(hostname);
  if (addresses.length === 0) {
    throw new Error("Live QA provider hostname did not resolve to an address.");
  }
  for (const address of addresses) {
    const family = isIP(address);
    if (
      family === 0 ||
      address.toLowerCase().startsWith("::ffff:") ||
      NON_PUBLIC_PROVIDER_ADDRESSES.check(address, family === 4 ? "ipv4" : "ipv6")
    ) {
      throw new Error("Live QA provider origin resolved to a non-public address.");
    }
  }
  return { baseUrl: endpoint.toString(), origin: endpoint.origin };
}

function parseProviderUrl(value: string, label: string, allowPath: boolean): URL {
  const trimmed = value.trim();
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new Error(`${label} is not a valid URL.`);
  }
  if (parsed.protocol !== "https:") {
    throw new Error(`${label} must use HTTPS.`);
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error(`${label} must not contain userinfo, a query, or a fragment.`);
  }
  if (parsed.port && parsed.port !== "443") {
    throw new Error(`${label} must use the default HTTPS port.`);
  }
  if (!allowPath && parsed.pathname !== "/") {
    throw new Error(`${label} must be an origin without an API path.`);
  }
  if (!allowPath && trimmed !== parsed.origin) {
    throw new Error(`${label} must be a canonical HTTPS origin.`);
  }
  if (parsed.hostname !== parsed.hostname.toLowerCase() || parsed.hostname.endsWith(".")) {
    throw new Error(`${label} hostname must be canonical.`);
  }
  return parsed;
}

function rejectLocalProviderHostname(hostname: string): void {
  const normalized = hostname.toLowerCase();
  if (
    normalized === "localhost" ||
    normalized.endsWith(".localhost") ||
    normalized.endsWith(".local") ||
    normalized.endsWith(".internal") ||
    (isIP(normalized) === 0 && !normalized.includes("."))
  ) {
    throw new Error("Live QA provider origin must use a public DNS hostname.");
  }
}

function unbracketHostname(hostname: string): string {
  return hostname.startsWith("[") && hostname.endsWith("]")
    ? hostname.slice(1, -1)
    : hostname;
}

function buildNonPublicProviderBlockList(): BlockList {
  const block = new BlockList();
  for (const [network, prefix] of [
    ["0.0.0.0", 8],
    ["10.0.0.0", 8],
    ["100.64.0.0", 10],
    ["127.0.0.0", 8],
    ["169.254.0.0", 16],
    ["172.16.0.0", 12],
    ["192.0.0.0", 24],
    ["192.0.2.0", 24],
    ["192.168.0.0", 16],
    ["198.18.0.0", 15],
    ["198.51.100.0", 24],
    ["203.0.113.0", 24],
    ["224.0.0.0", 4],
    ["240.0.0.0", 4]
  ] as const) {
    block.addSubnet(network, prefix, "ipv4");
  }
  for (const [network, prefix] of [
    ["::", 128],
    ["::1", 128],
    ["100::", 64],
    ["2001:db8::", 32],
    ["fc00::", 7],
    ["fe80::", 10],
    ["ff00::", 8]
  ] as const) {
    block.addSubnet(network, prefix, "ipv6");
  }
  return block;
}

/**
 * Consume a live-QA secret exactly once from this process. The delete happens before validation so
 * an empty or malformed invocation cannot leave a credential available to a later extension spawn.
 */
export function takeEnvironmentSecret(name: string): string {
  const raw = process.env[name];
  delete process.env[name];
  const value = raw?.trim() ?? "";
  if (!value) {
    throw new Error(`${name} must be configured for live provider QA.`);
  }
  return value;
}

/**
 * Reduce a live runner or Extension Host environment to the OS/runtime variables explicitly
 * required by VS Code and the CLI. Unknown variables are removed even when their names do not look
 * secret (for example signing-key PEM variables).
 */
export function restrictLiveQaEnvironment(
  environment: NodeJS.ProcessEnv,
  qaSecret?: string
): void {
  for (const [name, value] of Object.entries(environment)) {
    if (
      !isAllowedRuntimeEnvironmentName(name) ||
      (qaSecret !== undefined && qaSecret.length > 0 && value?.includes(qaSecret))
    ) {
      delete environment[name];
    }
  }
}

/** Copy only OS/runtime variables required to launch an isolated VS Code Extension Host. */
export function allowlistedQaRuntimeEnvironment(
  base: NodeJS.ProcessEnv,
  forbiddenValue?: string
): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {};
  for (const [name, value] of Object.entries(base)) {
    if (
      value !== undefined &&
      isAllowedRuntimeEnvironmentName(name) &&
      !(forbiddenValue && value.includes(forbiddenValue))
    ) {
      environment[name] = value;
    }
  }
  return environment;
}

/**
 * Build the Extension Host environment from a strict runtime allowlist, then add only the one
 * short-lived handoff variable consumed by the test driver before Alysis Code activation.
 */
export function liveExtensionHostEnvironment(
  base: NodeJS.ProcessEnv,
  secretName: string,
  qaSecret: string
): NodeJS.ProcessEnv {
  if (!ENVIRONMENT_NAME.test(secretName)) {
    throw new Error("Live QA secret environment name is invalid.");
  }
  if (!qaSecret.trim()) {
    throw new Error("Live QA secret must not be empty.");
  }
  const environment = allowlistedQaRuntimeEnvironment(base, qaSecret);
  environment[secretName] = qaSecret;
  return environment;
}

function isAllowedRuntimeEnvironmentName(name: string): boolean {
  const normalized = name.toUpperCase();
  return QA_RUNTIME_ENVIRONMENT.has(normalized) || /^LC_[A-Z0-9_]+$/.test(normalized);
}

/** Return content-independent diagnostics suitable for CI output and retained evidence. */
export function safeTextEvidence(value: string): SafeTextEvidence {
  return {
    length: value.length,
    sha256: createHash("sha256").update(value, "utf8").digest("hex")
  };
}

/** Never return an exception message or stack, only its class and message fingerprint. */
export function safeErrorEvidence(error: unknown): SafeErrorEvidence {
  const name = error instanceof Error && error.name.trim() ? error.name : "UnknownError";
  const message = error instanceof Error ? error.message : String(error);
  return { name, ...safeTextEvidence(message) };
}

/**
 * Fail closed when the exact secret remains in runner-owned files. Both UTF-8 and UTF-16LE are
 * checked because VS Code profile and diagnostic files can use either representation on Windows.
 */
export function assertSecretAbsentFromTrees(roots: readonly string[], qaSecret: string): void {
  if (!qaSecret) {
    throw new Error("Cannot scan live QA artifacts for an empty secret.");
  }
  const needles = uniqueBuffers([
    Buffer.from(qaSecret, "utf8"),
    Buffer.from(qaSecret, "utf16le")
  ]);
  for (const root of roots) {
    const affected = findSecretFile(resolve(root), needles);
    if (affected) {
      throw new Error(
        `Live QA secret leak check failed; exact credential bytes remained in ${affected}.`
      );
    }
  }
}

function uniqueBuffers(values: readonly Buffer[]): Buffer[] {
  const byHex = new Map<string, Buffer>();
  for (const value of values) {
    if (value.length > 0) {
      byHex.set(value.toString("hex"), value);
    }
  }
  return [...byHex.values()];
}

function findSecretFile(path: string, needles: readonly Buffer[]): string | undefined {
  const stats = lstatSync(path);
  if (stats.isSymbolicLink()) {
    return undefined;
  }
  if (stats.isDirectory()) {
    const entries = readdirSync(path, { withFileTypes: true }).sort((left, right) =>
      left.name.localeCompare(right.name)
    );
    for (const entry of entries) {
      const affected = findSecretFile(resolve(path, entry.name), needles);
      if (affected) {
        return affected;
      }
    }
    return undefined;
  }
  if (!stats.isFile()) {
    return undefined;
  }
  return fileContainsAny(path, needles) ? path : undefined;
}

function fileContainsAny(path: string, needles: readonly Buffer[]): boolean {
  const handle = openSync(path, "r");
  const buffer = Buffer.allocUnsafe(SCAN_CHUNK_BYTES);
  const overlapBytes = Math.max(...needles.map((needle) => needle.length - 1), 0);
  let carry = Buffer.alloc(0);
  try {
    while (true) {
      const bytesRead = readSync(handle, buffer, 0, buffer.length, null);
      if (bytesRead === 0) {
        return false;
      }
      const current = Buffer.concat([carry, buffer.subarray(0, bytesRead)]);
      if (needles.some((needle) => current.indexOf(needle) !== -1)) {
        return true;
      }
      carry = overlapBytes > 0
        ? Buffer.from(current.subarray(Math.max(0, current.length - overlapBytes)))
        : Buffer.alloc(0);
    }
  } finally {
    closeSync(handle);
  }
}
