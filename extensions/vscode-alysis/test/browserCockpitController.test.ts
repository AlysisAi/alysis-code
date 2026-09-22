import { createHash } from "node:crypto";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

import {
  BrowserCockpitController,
  BrowserCockpitDependencies,
  BrowserCockpitState,
  ManagedBrowserBridge
} from "../src/browser/BrowserCockpitController";
import { BrowserPreviewStore } from "../src/browser/BrowserPreviewStore";
import {
  ManagedBrowserArtifactReadParams,
  ManagedBrowserClickParams,
  ManagedBrowserCloseParams,
  ManagedBrowserDiagnosticsParams,
  ManagedBrowserNavigateParams,
  ManagedBrowserScreenshotParams,
  ManagedBrowserSnapshotParams,
  ManagedBrowserStartParams,
  ManagedBrowserTypeParams
} from "../src/client/AlysisProtocol";

const OWNER = "browser-owner-session";
const BROWSER = "browserSession1234567890";
const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.alloc(1024 * 1024 + 23, 0x5a)
]);
const PNG_SHA = createHash("sha256").update(PNG).digest("hex");

class FakeBrowserBridge implements ManagedBrowserBridge {
  public starts: ManagedBrowserStartParams[] = [];
  public navigations: ManagedBrowserNavigateParams[] = [];
  public snapshots: ManagedBrowserSnapshotParams[] = [];
  public screenshots: ManagedBrowserScreenshotParams[] = [];
  public artifactReads: ManagedBrowserArtifactReadParams[] = [];
  public diagnosticCalls: ManagedBrowserDiagnosticsParams[] = [];
  public clicks: ManagedBrowserClickParams[] = [];
  public types: ManagedBrowserTypeParams[] = [];
  public closes: ManagedBrowserCloseParams[] = [];
  public listGate: Promise<void> | undefined;
  public corruptChunk = false;
  public responseOwner: string | undefined;
  public responseBrowser: string | undefined;

  private scope(params: { session_id: string; browser_session_id?: string }): Record<string, string> {
    return {
      session_id: this.responseOwner ?? params.session_id,
      browser_session_id: this.responseBrowser ?? params.browser_session_id ?? BROWSER
    };
  }

  public async browserStart(params: ManagedBrowserStartParams): Promise<any> {
    this.starts.push(params);
    return {
      ...this.scope(params),
      ...status(params.network_scope ?? "public"),
      browser_session_id: this.responseBrowser ?? BROWSER
    };
  }

  public async browserNavigate(params: ManagedBrowserNavigateParams): Promise<any> {
    this.navigations.push(params);
    return {
      ...this.scope(params),
      url: "https://example.test/public/path",
      result: { data: {}, truncated: false, size_bytes: 2 }
    };
  }

  public async browserSnapshot(params: ManagedBrowserSnapshotParams): Promise<any> {
    this.snapshots.push(params);
    return {
      ...this.scope(params),
      kind: params.kind ?? "semantic",
      text: "x".repeat(300 * 1024),
      truncated: false,
      size_bytes: 300 * 1024
    };
  }

  public async browserScreenshot(params: ManagedBrowserScreenshotParams): Promise<any> {
    this.screenshots.push(params);
    return {
      ...this.scope(params),
      artifact_id: `browser:${BROWSER}:screenshot-0001-deadbeef.png`,
      media_type: "image/png",
      size_bytes: PNG.length,
      sha256: PNG_SHA
    };
  }

  public async browserArtifactRead(params: ManagedBrowserArtifactReadParams): Promise<any> {
    this.artifactReads.push(params);
    const offset = params.offset ?? 0;
    const maxBytes = params.max_bytes ?? 256 * 1024;
    const chunk = PNG.subarray(offset, Math.min(PNG.length, offset + maxBytes));
    const nextOffset = offset + chunk.length + (this.corruptChunk ? 1 : 0);
    return {
      ...this.scope(params),
      artifact_id: params.artifact_id,
      media_type: "image/png",
      encoding: "base64",
      content: chunk.toString("base64"),
      offset,
      next_offset: nextOffset,
      size_bytes: PNG.length,
      truncated: nextOffset < PNG.length
    };
  }

  public async browserDiagnostics(params: ManagedBrowserDiagnosticsParams): Promise<any> {
    this.diagnosticCalls.push(params);
    const typedText = this.types.at(-1)?.text;
    return {
      ...this.scope(params),
      events: Array.from({ length: 120 }, (_, index) => ({
        category: index % 2 ? "console" : "network",
        method: index === 1 ? "Log.entryAdded" : `Event.${index}`,
        params: {
          data: index === 1
            ? {
                entry: {
                  level: "warning",
                  text: typedText ? `Page echoed ${typedText}` : "Build failed safely",
                  value: typedText,
                  description: typedText,
                  args: typedText ? [{ type: "string", value: typedText, description: typedText }] : []
                }
              }
            : { text: "x".repeat(20 * 1024) },
          truncated: false,
          size_bytes: 20 * 1024
        }
      })),
      truncated: true,
      max_events: 100
    };
  }

  public async browserClick(params: ManagedBrowserClickParams): Promise<any> {
    this.clicks.push(params);
    return { ...this.scope(params), clicked: true };
  }

  public async browserType(params: ManagedBrowserTypeParams): Promise<any> {
    this.types.push(params);
    return {
      ...this.scope(params),
      typed: true,
      character_count: params.text.length
    };
  }

  public async browserStatus(params: any): Promise<any> {
    return { session_id: params.session_id, ...status() };
  }

  public async browserList(sessionId: string): Promise<any> {
    await this.listGate;
    return { session_id: sessionId, browsers: [status()], count: 1 };
  }

  public async browserClose(params: ManagedBrowserCloseParams): Promise<any> {
    this.closes.push(params);
    return {
      ...this.scope(params),
      status: "closed"
    };
  }
}

test("BrowserCockpitController starts only a public, discovered browser and gates mutations on trust", async () => {
  await withController(async ({ controller, bridge, setTrusted }) => {
    await controller.start();
    assert.deepEqual(bridge.starts, [{
      session_id: OWNER,
      workspace_trusted: true,
      network_scope: "public"
    }]);
    assert.equal("allow_local_destinations" in bridge.starts[0], false);
    assert.equal("executable_path" in bridge.starts[0], false);
    assert.equal(controller.state().selectedBrowserId, BROWSER);

    // A public session is agent-shared. It must not be able to aim the browser at this machine or
    // the local network — including the cloud metadata endpoint — even for the round trip it would
    // take the backend to refuse.
    for (const local of [
      "http://127.0.0.1:8000/",
      "http://localhost:3000/",
      "http://169.254.169.254/latest/meta-data/",
      "http://192.168.1.10/admin",
      "http://[::1]:8080/"
    ]) {
      await assert.rejects(controller.navigate(local), /public web only/, local);
    }
    assert.equal(bridge.navigations.length, 0);
    await controller.navigate("https://example.test/docs");
    assert.equal(bridge.navigations.length, 1);

    setTrusted(false);
    await assert.rejects(controller.navigate("https://example.test/?secret=query"), /Workspace Trust/);
    await assert.rejects(controller.click("#save"), /Workspace Trust/);
    await assert.rejects(controller.type("#token", "typed-secret"), /Workspace Trust/);
    // Still the one public navigation from above; losing trust adds nothing.
    assert.equal(bridge.navigations.length, 1);
    assert.equal(bridge.clicks.length, 0);
    assert.equal(bridge.types.length, 0);
  });
});

test("BrowserCockpitController starts loopback browsing only after direct confirmation and never sends the legacy flag", async () => {
  await withController(async ({ controller, bridge, setLocalConfirmed }) => {
    setLocalConfirmed(false);
    await controller.startLocal();
    assert.equal(bridge.starts.length, 0, "cancelling the modal cannot create an owner-scoped browser");

    setLocalConfirmed(true);
    await controller.startLocal();
    assert.deepEqual(bridge.starts, [{
      session_id: OWNER,
      workspace_trusted: true,
      network_scope: "public_loopback",
      yes: true,
      confirm: true
    }]);
    assert.equal("allow_local_destinations" in bridge.starts[0], false);
    assert.equal("executable_path" in bridge.starts[0], false);
    assert.equal(controller.state().sessions[0]?.networkScope, "public_loopback");
    assert.match(controller.state().notice ?? "", /Direct IDE/);
    assert.match(controller.state().notice ?? "", /agent cannot access/i);
  });
});

test("BrowserCockpitController serializes operations and fences stale bridge results", async () => {
  await withController(async ({ controller, bridge, states }) => {
    await controller.setOwnerSession(OWNER);
    const gate = deferred<void>();
    bridge.listGate = gate.promise;
    const refresh = controller.refresh();
    await Promise.resolve();
    await assert.rejects(controller.start(), /already running/);
    await controller.disconnect("bridge restarted");
    gate.resolve();
    await refresh;

    const final = controller.state();
    assert.equal(final.phase, "disconnected");
    assert.equal(final.sessions.length, 0);
    assert.equal(final.notice, "bridge restarted");
    assert.equal(states.at(-1)?.phase, "disconnected");
  });
});

test("BrowserCockpitController bounds snapshot and diagnostic projections", async () => {
  await withController(async ({ controller }) => {
    await controller.start();
    await controller.snapshot("text");
    assert.equal(Buffer.byteLength(controller.state().snapshot?.preview ?? ""), 256 * 1024);
    assert.equal(controller.state().snapshot?.truncated, true);

    await controller.diagnostics();
    const state = controller.state();
    assert.equal(state.diagnostics.length, 100);
    assert.equal(state.diagnosticsTruncated, true);
    assert.ok(state.diagnostics.every((event) => Buffer.byteLength(event.preview) <= 4 * 1024));
    assert.ok(state.diagnostics.every((event) => !event.method.startsWith("Event.")));
    assert.equal(JSON.stringify(state.diagnostics).includes("Event.0"), false, "raw protocol method names stay host-side");
    assert.equal(state.diagnostics[1]?.method, "Console warning");
    assert.equal(state.diagnostics[1]?.preview, "Console content hidden because it can contain browser input.");
  });
});

test("BrowserCockpitController verifies chunked PNG bytes before publishing or saving", async () => {
  await withController(async ({ controller, bridge, root }) => {
    await controller.start();
    await controller.screenshot(true);
    const preview = controller.currentPreview();
    assert.ok(preview);
    assert.equal(preview?.sha256, PNG_SHA);
    assert.deepEqual(await readFile(preview!.path), PNG);
    assert.equal(bridge.artifactReads.length, 2, "screenshot is read in bounded chunks");

    const destination = join(root, "saved.png");
    await controller.saveScreenshot(destination);
    assert.deepEqual(await readFile(destination), PNG);
  });
});

test("BrowserCockpitController rejects incoherent artifact chunks without exposing details", async () => {
  await withController(async ({ controller, bridge }) => {
    await controller.start();
    bridge.corruptChunk = true;
    await controller.screenshot();
    assert.equal(controller.currentPreview(), null);
    assert.equal(controller.state().screenshot, null);
    assert.equal(controller.state().error, "Browser screenshot chunk verification failed.");
  });
});

test("BrowserCockpitController never publishes typed text and permits confirmed cleanup after trust loss", async () => {
  await withController(async ({ controller, bridge, setTrusted, states }) => {
    await controller.start();
    const secret = "typed-browser-secret-value";
    await controller.type("#password", secret);
    assert.equal(bridge.types[0]?.text, secret);
    await controller.diagnostics();
    assert.equal(JSON.stringify(controller.state()).includes(secret), false);
    assert.equal(states.some((state) => JSON.stringify(state).includes(secret)), false);

    setTrusted(false);
    await controller.close();
    assert.deepEqual(bridge.closes, [{
      session_id: OWNER,
      browser_session_id: BROWSER,
      delete_artifacts: true,
      confirm: true
    }]);
    assert.equal(controller.state().sessions.length, 0);
  });
});

test("BrowserCockpitController retains a browser row when backend cleanup fails", async () => {
  await withController(async ({ controller, bridge }) => {
    await controller.start();
    bridge.browserClose = async () => {
      throw Object.assign(new Error("sensitive cleanup internals"), { code: "browser_cleanup_incomplete" });
    };
    await controller.close();
    assert.equal(controller.state().sessions.length, 1);
    assert.equal(controller.state().selectedBrowserId, BROWSER);
    assert.equal(controller.state().error, "Managed browser request failed (browser_cleanup_incomplete).");
  });
});

test("BrowserCockpitController never closes without an explicit direct confirmation", async () => {
  await withController(async ({ controller, bridge, setCloseConfirmed }) => {
    await controller.start();
    setCloseConfirmed(false);
    await controller.close();
    assert.equal(bridge.closes.length, 0);
    assert.equal(controller.state().sessions.length, 1);
  });
});

test("BrowserCockpitController closes the browser the confirmation named, not a later selection", async () => {
  const other = "otherBrowserSession12345";
  await withController(async ({ controller, bridge, setConfirmCloseHook }) => {
    await controller.start();
    bridge.browserList = async (sessionId: string) => ({
      session_id: sessionId,
      browsers: [status(), { ...status(), browser_session_id: other }],
      count: 2
    });
    await controller.refresh();
    assert.equal(controller.state().selectedBrowserId, BROWSER);

    // The modal names BROWSER; a select() lands while it is open.
    setConfirmCloseHook(async () => {
      controller.select(other);
    });
    await controller.close();

    assert.deepEqual(bridge.closes, [{
      session_id: OWNER,
      browser_session_id: BROWSER,
      delete_artifacts: true,
      confirm: true
    }]);
    assert.deepEqual(controller.state().sessions.map((item) => item.browserSessionId), [other]);
    assert.equal(controller.state().selectedBrowserId, other);
  });
});

test("BrowserCockpitController rejects cross-owner and cross-browser responses before commit", async () => {
  await withController(async ({ controller, bridge }) => {
    bridge.responseOwner = "different-owner";
    await controller.start();
    assert.equal(controller.state().sessions.length, 0);
    assert.equal(controller.state().error, "Managed browser response scope verification failed.");
  });

  const operations: Array<(controller: BrowserCockpitController) => Promise<void>> = [
    (controller) => controller.navigate("https://example.test"),
    (controller) => controller.snapshot("text"),
    (controller) => controller.screenshot(),
    (controller) => controller.diagnostics(),
    (controller) => controller.click("#submit"),
    (controller) => controller.type("#field", "safe test text"),
    (controller) => controller.close()
  ];
  for (const operation of operations) {
    await withController(async ({ controller, bridge }) => {
      await controller.start();
      bridge.responseBrowser = "differentBrowser1234567890";
      await operation(controller);
      assert.equal(controller.state().error, "Managed browser response scope verification failed.");
      assert.equal(controller.state().sessions.length, 1, "mismatched responses never remove the selected row");
    });
  }
});

test("session resets fence ids and preview state even when local cache deletion fails", async () => {
  await withController(async ({ controller, previewStore }) => {
    await controller.start();
    await controller.screenshot();
    assert.ok(controller.currentPreview());
    previewStore.remove = async () => {
      throw new Error("simulated locked preview");
    };

    await controller.disconnect("bridge replaced");
    const state = controller.state();
    assert.equal(state.ownerSessionId, null);
    assert.equal(state.selectedBrowserId, null);
    assert.equal(state.sessions.length, 0);
    assert.equal(state.screenshot, null);
    assert.equal(controller.currentPreview(), null);
  });
});

async function withController(
  run: (fixture: {
    controller: BrowserCockpitController;
    bridge: FakeBrowserBridge;
    states: BrowserCockpitState[];
    setTrusted(value: boolean): void;
    setLocalConfirmed(value: boolean): void;
    setCloseConfirmed(value: boolean): void;
    setConfirmCloseHook(hook: (() => Promise<void>) | undefined): void;
    root: string;
    previewStore: BrowserPreviewStore;
  }) => Promise<void>
): Promise<void> {
  const root = await mkdtemp(join(tmpdir(), "alysis-browser-controller-"));
  const bridge = new FakeBrowserBridge();
  const states: BrowserCockpitState[] = [];
  let trusted = true;
  let closeConfirmed = true;
  let localConfirmed = true;
  let confirmCloseHook: (() => Promise<void>) | undefined;
  const previewStore = new BrowserPreviewStore(join(root, "previews"));
  const dependencies: BrowserCockpitDependencies = {
    bridge,
    previewStore,
    ensureOwnerSession: async () => OWNER,
    isWorkspaceTrusted: () => trusted,
    compatibility: () => ({
      supported: true,
      reason: null,
      localTestingSupported: true,
      localTestingReason: null
    }),
    confirmLocalStart: async () => localConfirmed,
    confirmClose: async () => {
      await confirmCloseHook?.();
      return closeConfirmed;
    },
    onChange: (state) => states.push(state)
  };
  const controller = new BrowserCockpitController(dependencies);
  try {
    await run({
      controller,
      bridge,
      states,
      setTrusted: (value) => {
        trusted = value;
      },
      setLocalConfirmed: (value) => {
        localConfirmed = value;
      },
      setCloseConfirmed: (value) => {
        closeConfirmed = value;
      },
      setConfirmCloseHook: (hook) => {
        confirmCloseHook = hook;
      },
      root,
      previewStore
    });
  } finally {
    await controller.dispose();
    await rm(root, { recursive: true, force: true });
  }
}

function status(networkScope: "public" | "public_loopback" | "local_network" = "public"): any {
  return {
    browser_session_id: BROWSER,
    product: "chrome",
    state: "running",
    created_at: 1,
    network_scope: networkScope,
    allow_local_destinations: networkScope !== "public",
    active_url: null,
    artifact_count: 0
  };
}

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolvePromise!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}
