import { statSync } from "node:fs";
import path from "node:path";

/**
 * Host network plumbing for the spawned CLI.
 *
 * VS Code's `http.proxy`, `http.noProxy` and `http.proxySupport` live in the workbench, not in
 * `process.env`, so a child process bypasses the proxy the user configured and fails behind a
 * corporate gateway with a generic network error. This module translates those settings (plus
 * `alysis.extraCaCerts`) into the environment variables the Python CLI and its own children
 * actually read, and pins the extension host's PID so the CLI can notice when its parent is gone.
 *
 * Two phases, so user-facing notices surface once at config time rather than on every launch
 * profile comparison: `resolveHostNetworkSettings` validates and reports, and
 * `applyHostNetworkEnvironment` applies the validated result silently.
 *
 * Pure by design: no `vscode` import, so it is unit-tested without an extension host.
 */

export type ProxySupport = "off" | "on" | "fallback" | "override";

/** Raw values as read from VS Code settings. */
export interface HostNetworkSettings {
  /** `http.proxy` — a URL such as `http://proxy.corp.example:3128` (may carry credentials). */
  proxy?: string;
  /** `http.noProxy` — hosts, domain suffixes or CIDRs that bypass the proxy. */
  noProxy?: readonly string[];
  /** `http.proxySupport` — `off` means the user asked for no proxying at all. */
  proxySupport?: ProxySupport | string;
  /** `http.proxyStrictSSL` — deliberately not honoured; see `strict_ssl_ignored`. */
  proxyStrictSSL?: boolean;
  /** `alysis.extraCaCerts` — absolute path to a PEM bundle trusted in addition to the defaults. */
  extraCaCerts?: string;
}

/** Validated values; every field is safe to write into a child environment as-is. */
export interface ResolvedHostNetworkSettings {
  proxySupport: ProxySupport;
  /** Empty when unset or rejected. */
  proxy: string;
  noProxy: readonly string[];
  /** Empty when unset or rejected. */
  extraCaCerts: string;
}

export interface HostEnvironmentNotice {
  code: "proxy_invalid" | "ca_bundle_relative" | "ca_bundle_missing" | "strict_ssl_ignored";
  message: string;
}

export interface ResolveHostNetworkOptions {
  /** Injected for tests; defaults to a synchronous regular-file check. */
  fileExists?: (filePath: string) => boolean;
}

/** The extension host's PID. The CLI's parent watchdog exits when this process is gone. */
export const PARENT_PID_ENV = "ALYSIS_PARENT_PID";
/** Read by the CLI, which merges the file with its bundled roots and exports SSL_CERT_FILE. */
export const EXTRA_CA_CERTS_ENV = "ALYSIS_EXTRA_CA_CERTS";
/** Read natively by Node (and therefore by Node-based MCP servers the CLI spawns). Additive. */
export const NODE_EXTRA_CA_CERTS_ENV = "NODE_EXTRA_CA_CERTS";

const PROXY_ENV_NAMES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"] as const;
const PROXY_URL_PROTOCOLS = new Set(["http:", "https:", "socks:", "socks4:", "socks4a:", "socks5:", "socks5h:"]);
const PROXY_SUPPORT_VALUES: ReadonlySet<string> = new Set(["off", "on", "fallback", "override"]);

/**
 * Validates raw settings. Never throws: a bad value produces a notice and is dropped, so a typo
 * in `http.proxy` cannot take the CLI down with it.
 */
export function resolveHostNetworkSettings(
  raw: HostNetworkSettings | undefined,
  options: ResolveHostNetworkOptions = {}
): { settings: ResolvedHostNetworkSettings; notices: HostEnvironmentNotice[] } {
  const notices: HostEnvironmentNotice[] = [];
  const support = String(raw?.proxySupport ?? "override").trim().toLowerCase();
  const settings: ResolvedHostNetworkSettings = {
    proxySupport: (PROXY_SUPPORT_VALUES.has(support) ? support : "override") as ProxySupport,
    proxy: "",
    noProxy: [],
    extraCaCerts: ""
  };
  if (!raw) {
    return { settings, notices };
  }

  const proxy = String(raw.proxy ?? "").trim();
  if (proxy.length > 0) {
    const problem = validateProxyUrl(proxy);
    if (problem) {
      notices.push({ code: "proxy_invalid", message: `http.proxy is ignored: ${problem}` });
    } else {
      settings.proxy = proxy;
    }
  }

  settings.noProxy = (raw.noProxy ?? [])
    .map((entry) => String(entry ?? "").trim())
    .filter((entry) => entry.length > 0 && !/[\s,]/.test(entry));

  const extraCaCerts = String(raw.extraCaCerts ?? "").trim();
  if (extraCaCerts.length > 0) {
    if (!path.isAbsolute(extraCaCerts)) {
      notices.push({
        code: "ca_bundle_relative",
        message: `alysis.extraCaCerts is ignored: "${extraCaCerts}" is not an absolute path.`
      });
    } else if (!(options.fileExists ?? defaultFileExists)(extraCaCerts)) {
      notices.push({
        code: "ca_bundle_missing",
        message: `alysis.extraCaCerts is ignored: "${extraCaCerts}" does not exist or is not a readable file.`
      });
    } else {
      settings.extraCaCerts = extraCaCerts;
    }
  }

  if (raw.proxyStrictSSL === false) {
    notices.push({
      code: "strict_ssl_ignored",
      message:
        "http.proxyStrictSSL is false, but Alysis Code never disables certificate verification for the CLI. "
        + "For a TLS-inspecting proxy, point alysis.extraCaCerts at the proxy's CA bundle instead."
    });
  }
  return { settings, notices };
}

/** Writes validated settings into `env` in place. Silent; validation happened in `resolve`. */
export function applyHostNetworkEnvironment(
  env: NodeJS.ProcessEnv,
  settings: ResolvedHostNetworkSettings | undefined,
  platform: NodeJS.Platform = process.platform
): void {
  if (!settings) {
    return;
  }
  if (settings.proxySupport === "off") {
    // The user turned proxying off for extensions. Ambient shell variables must not leak into the
    // child either, or the CLI would proxy while the workbench does not.
    for (const name of PROXY_ENV_NAMES) {
      deleteCaseInsensitive(env, name);
    }
  } else {
    if (settings.proxy.length > 0) {
      setProxyVariable(env, "HTTP_PROXY", settings.proxy, platform);
      setProxyVariable(env, "HTTPS_PROXY", settings.proxy, platform);
    }
    if (settings.noProxy.length > 0) {
      setProxyVariable(env, "NO_PROXY", settings.noProxy.join(","), platform);
    }
  }
  if (settings.extraCaCerts.length > 0) {
    env[EXTRA_CA_CERTS_ENV] = settings.extraCaCerts;
    env[NODE_EXTRA_CA_CERTS_ENV] = settings.extraCaCerts;
  }
}

/** Pins the parent PID so the CLI can exit if this extension host is hard-killed. */
export function applyParentProcessEnvironment(env: NodeJS.ProcessEnv, pid: number = process.pid): void {
  if (Number.isSafeInteger(pid) && pid > 0) {
    env[PARENT_PID_ENV] = String(pid);
  }
}

function validateProxyUrl(value: string): string | undefined {
  if (/\s/.test(value)) {
    return "the URL contains whitespace.";
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return `"${value}" is not a URL.`;
  }
  if (!PROXY_URL_PROTOCOLS.has(parsed.protocol)) {
    return `unsupported scheme "${parsed.protocol}" (expected http, https or socks).`;
  }
  if (!parsed.hostname) {
    return "the URL has no host.";
  }
  return undefined;
}

// Windows environment names are case-insensitive, so writing both spellings would create a
// duplicate in the child's environment block. Elsewhere the lowercase spelling is what curl and
// most Unix tools read, while the CLI and Node accept either.
function setProxyVariable(env: NodeJS.ProcessEnv, name: string, value: string, platform: NodeJS.Platform): void {
  deleteCaseInsensitive(env, name);
  env[name] = value;
  if (platform !== "win32") {
    env[name.toLowerCase()] = value;
  }
}

function deleteCaseInsensitive(env: NodeJS.ProcessEnv, name: string): void {
  const upper = name.toUpperCase();
  for (const key of Object.keys(env)) {
    if (key.toUpperCase() === upper) {
      delete env[key];
    }
  }
}

function defaultFileExists(filePath: string): boolean {
  try {
    return statSync(filePath).isFile();
  } catch {
    return false;
  }
}
