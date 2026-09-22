import assert from "node:assert/strict";
import test from "node:test";

import {
  EXTRA_CA_CERTS_ENV,
  NODE_EXTRA_CA_CERTS_ENV,
  PARENT_PID_ENV,
  applyHostNetworkEnvironment,
  applyParentProcessEnvironment,
  resolveHostNetworkSettings
} from "../src/client/hostEnvironment";

const exists = () => true;
const missing = () => false;

test("no settings at all leaves the environment untouched", () => {
  const env: NodeJS.ProcessEnv = { PATH: "/bin", HTTPS_PROXY: "http://ambient:3128" };
  applyHostNetworkEnvironment(env, undefined, "linux");
  assert.deepEqual(env, { PATH: "/bin", HTTPS_PROXY: "http://ambient:3128" });
  const { settings, notices } = resolveHostNetworkSettings(undefined);
  assert.deepEqual(notices, []);
  assert.deepEqual(settings, { proxySupport: "override", proxy: "", noProxy: [], extraCaCerts: "" });
});

test("http.proxy is exported as HTTP_PROXY and HTTPS_PROXY in both spellings on POSIX", () => {
  const { settings, notices } = resolveHostNetworkSettings({ proxy: " http://user:pw@proxy.corp.example:3128 " });
  assert.deepEqual(notices, []);
  const env: NodeJS.ProcessEnv = { http_proxy: "http://stale:1" };
  applyHostNetworkEnvironment(env, settings, "linux");
  assert.deepEqual(env, {
    HTTP_PROXY: "http://user:pw@proxy.corp.example:3128",
    http_proxy: "http://user:pw@proxy.corp.example:3128",
    HTTPS_PROXY: "http://user:pw@proxy.corp.example:3128",
    https_proxy: "http://user:pw@proxy.corp.example:3128"
  });
});

test("on Windows only the upper-case spelling is written (names are case-insensitive there)", () => {
  const { settings } = resolveHostNetworkSettings({ proxy: "http://proxy.corp.example:3128", noProxy: ["localhost"] });
  const env: NodeJS.ProcessEnv = { http_proxy: "http://stale:1" };
  applyHostNetworkEnvironment(env, settings, "win32");
  assert.deepEqual(env, {
    HTTP_PROXY: "http://proxy.corp.example:3128",
    HTTPS_PROXY: "http://proxy.corp.example:3128",
    NO_PROXY: "localhost"
  });
});

test("http.proxySupport off clears ambient proxy variables so the CLI matches the workbench", () => {
  const { settings, notices } = resolveHostNetworkSettings({
    proxySupport: "off",
    proxy: "http://proxy.corp.example:3128",
    noProxy: ["localhost"]
  });
  assert.deepEqual(notices, []);
  const env: NodeJS.ProcessEnv = {
    PATH: "/bin",
    HTTP_PROXY: "http://ambient:3128",
    https_proxy: "http://ambient:3128",
    ALL_PROXY: "socks5://ambient:1080",
    no_proxy: "localhost"
  };
  applyHostNetworkEnvironment(env, settings, "linux");
  assert.deepEqual(env, { PATH: "/bin" });
});

test("http.noProxy entries are trimmed, filtered and joined for NO_PROXY", () => {
  const { settings } = resolveHostNetworkSettings({
    noProxy: [" localhost ", "", ".corp.example", "10.0.0.0/8", "bad entry", "also,bad"]
  });
  assert.deepEqual(settings.noProxy, ["localhost", ".corp.example", "10.0.0.0/8"]);
  const env: NodeJS.ProcessEnv = {};
  applyHostNetworkEnvironment(env, settings, "linux");
  assert.equal(env.NO_PROXY, "localhost,.corp.example,10.0.0.0/8");
  assert.equal(env.no_proxy, "localhost,.corp.example,10.0.0.0/8");
});

for (const [label, proxy] of [
  ["not a URL", "proxy.corp.example:3128"],
  ["whitespace", "http://proxy corp:3128"],
  ["unsupported scheme", "ftp://proxy.corp.example:21"],
  ["no host", "http://"]
] as const) {
  test(`an invalid http.proxy is dropped with a notice, never exported: ${label}`, () => {
    const { settings, notices } = resolveHostNetworkSettings({ proxy });
    assert.equal(settings.proxy, "");
    assert.equal(notices.length, 1);
    assert.equal(notices[0].code, "proxy_invalid");
    const env: NodeJS.ProcessEnv = {};
    applyHostNetworkEnvironment(env, settings, "linux");
    assert.deepEqual(env, {});
  });
}

test("socks proxies are accepted", () => {
  const { settings, notices } = resolveHostNetworkSettings({ proxy: "socks5h://proxy.corp.example:1080" });
  assert.deepEqual(notices, []);
  assert.equal(settings.proxy, "socks5h://proxy.corp.example:1080");
});

test("extraCaCerts is exported for the CLI (additive merge) and for Node children", () => {
  const { settings, notices } = resolveHostNetworkSettings(
    { extraCaCerts: "/etc/pki/corp-ca.pem" },
    { fileExists: exists }
  );
  assert.deepEqual(notices, []);
  const env: NodeJS.ProcessEnv = {};
  applyHostNetworkEnvironment(env, settings, "linux");
  assert.equal(env[EXTRA_CA_CERTS_ENV], "/etc/pki/corp-ca.pem");
  assert.equal(env[NODE_EXTRA_CA_CERTS_ENV], "/etc/pki/corp-ca.pem");
  // SSL_CERT_FILE replaces httpx's bundled roots wholesale, so the extension never sets it; the
  // CLI derives it from the merged bundle.
  assert.equal(env.SSL_CERT_FILE, undefined);
});

test("a relative or missing extraCaCerts path is dropped with a notice", () => {
  const relative = resolveHostNetworkSettings({ extraCaCerts: "certs/corp-ca.pem" }, { fileExists: exists });
  assert.equal(relative.settings.extraCaCerts, "");
  assert.equal(relative.notices[0]?.code, "ca_bundle_relative");

  const absent = resolveHostNetworkSettings({ extraCaCerts: "/etc/pki/nope.pem" }, { fileExists: missing });
  assert.equal(absent.settings.extraCaCerts, "");
  assert.equal(absent.notices[0]?.code, "ca_bundle_missing");

  const env: NodeJS.ProcessEnv = {};
  applyHostNetworkEnvironment(env, absent.settings, "linux");
  assert.deepEqual(env, {});
});

test("http.proxyStrictSSL false is reported as ignored and changes nothing in the environment", () => {
  const { settings, notices } = resolveHostNetworkSettings({ proxyStrictSSL: false });
  assert.equal(notices.length, 1);
  assert.equal(notices[0].code, "strict_ssl_ignored");
  assert.match(notices[0].message, /extraCaCerts/);
  const env: NodeJS.ProcessEnv = {};
  applyHostNetworkEnvironment(env, settings, "linux");
  assert.deepEqual(env, {});
});

test("unknown proxySupport values fall back to override rather than to off", () => {
  const { settings } = resolveHostNetworkSettings({ proxySupport: "sometimes", proxy: "http://p.example:1" });
  assert.equal(settings.proxySupport, "override");
  const env: NodeJS.ProcessEnv = {};
  applyHostNetworkEnvironment(env, settings, "win32");
  assert.equal(env.HTTPS_PROXY, "http://p.example:1");
});

test("the parent PID is pinned only for a positive safe integer", () => {
  const env: NodeJS.ProcessEnv = {};
  applyParentProcessEnvironment(env, 4242);
  assert.equal(env[PARENT_PID_ENV], "4242");
  for (const bad of [0, -1, 1.5, Number.NaN, Number.MAX_SAFE_INTEGER + 1]) {
    const scratch: NodeJS.ProcessEnv = {};
    applyParentProcessEnvironment(scratch, bad);
    assert.deepEqual(scratch, {}, String(bad));
  }
  const fromProcess: NodeJS.ProcessEnv = {};
  applyParentProcessEnvironment(fromProcess);
  assert.equal(fromProcess[PARENT_PID_ENV], String(process.pid));
});
