import assert from "node:assert/strict";
import test from "node:test";

import { isChatPanelMessage } from "../src/chat/webviewMessages";

test("isChatPanelMessage accepts expected webview messages", () => {
  assert.equal(isChatPanelMessage({ type: "submit", text: "hello", requestId: "request-1" }), true);
  assert.equal(isChatPanelMessage({ type: "cancel" }), true);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: "/plans", requestId: "request-2" }), true);
  assert.equal(isChatPanelMessage({ type: "approval", approvalId: "a1", decision: "deny" }), true);
  assert.equal(isChatPanelMessage({ type: "artifact.refresh" }), true);
  assert.equal(isChatPanelMessage({ type: "artifact.open", artifactId: "session:a.txt" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.executePreview" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.executeReview" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.status.refresh" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.diff.open", diffId: "diff-1" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.review" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.review", taskId: "t-1" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.assets.refresh" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.assets.open", assetId: "a-1" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.artifact.open", sessionId: "session-1", artifactId: "artifact-1" }), true);
  assert.equal(isChatPanelMessage({ type: "forge.approval", sessionId: "session-1", approvalId: "approval-1", decision: "allow_once" }), true);
  assert.equal(isChatPanelMessage({ type: "swarm.recovery.refresh" }), true);
  assert.equal(isChatPanelMessage({ type: "swarm.recovery.resume", jobId: "job-1", revision: 7 }), true);
  assert.equal(isChatPanelMessage({ type: "swarm.recovery.dismiss", jobId: "job-1", revision: 7 }), true);
  assert.equal(isChatPanelMessage({ type: "command.openSetupGuide" }), true);
  assert.equal(isChatPanelMessage({ type: "command.openOutput" }), true);
  assert.equal(isChatPanelMessage({ type: "command.setCliPath" }), true);
  assert.equal(isChatPanelMessage({ type: "cockpit.retryStatus" }), true);
  assert.equal(isChatPanelMessage({ type: "bridge.restart" }), true);
  assert.equal(isChatPanelMessage({ type: "composerMode.set", mode: "chat" }), true);
  assert.equal(isChatPanelMessage({ type: "composerMode.set", mode: "forge" }), true);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "readonly" }), true);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "auto" }), true);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "fullaccess" }), true);
  assert.equal(isChatPanelMessage({ type: "session.model.set", model: "gpt-5" }), true);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "architect" }), true);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "My.Custom_persona-2" }), true);
  assert.equal(isChatPanelMessage({ type: "profile.use", name: "openai" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.start" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.startLocal" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.navigate", url: "https://example.com" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.snapshot", kind: "accessibility" }), true);
  assert.equal(isChatPanelMessage({ type: "browser.screenshot", fullPage: true }), true);
  assert.equal(isChatPanelMessage({ type: "browser.type", selector: "#field", text: "hello", replace: true }), true);
  assert.equal(isChatPanelMessage({ type: "browser.screenshot.save" }), true);
});

test("isChatPanelMessage rejects unexpected webview messages", () => {
  assert.equal(isChatPanelMessage({ type: "unknown" }), false);
  assert.equal(isChatPanelMessage(null), false);
  assert.equal(isChatPanelMessage("submit"), false);
  assert.equal(isChatPanelMessage({ type: "submit", text: "" }), false);
  assert.equal(isChatPanelMessage({ type: "submit" }), false);
  assert.equal(isChatPanelMessage({ type: "submit", text: "hello" }), false);
  assert.equal(isChatPanelMessage({ type: "approval", approvalId: "a1", decision: "approve" }), false);
  assert.equal(isChatPanelMessage({ type: "approval", decision: "deny" }), false);
  assert.equal(isChatPanelMessage({ type: "artifact.open" }), false);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: "plans", requestId: "r" }), false);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: "/plans" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.diff.open", diffId: "" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.assets.open" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.assets.open", assetId: "" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.review", taskId: "" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.artifact.open", sessionId: "", artifactId: "artifact-1" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.approval", approvalId: "approval-1", decision: "allow_once" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.approval", approvalId: "approval-1", decision: "approve" }), false);
  assert.equal(isChatPanelMessage({ type: "composerMode.set", mode: "swarm" }), false);
  assert.equal(isChatPanelMessage({ type: "composerMode.set" }), false);
  assert.equal(isChatPanelMessage({ type: "mode.set", mode: "turbo" }), false);
  assert.equal(isChatPanelMessage({ type: "mode.set" }), false);
  assert.equal(isChatPanelMessage({ type: "session.model.set" }), false);
  assert.equal(isChatPanelMessage({ type: "session.model.set", model: "" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "has space" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: "../traversal" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: ".hidden" }), false);
  assert.equal(isChatPanelMessage({ type: "persona.set", name: `a${"b".repeat(64)}` }), false);
  assert.equal(isChatPanelMessage({ type: "profile.use" }), false);
  assert.equal(isChatPanelMessage({ type: "profile.use", name: "" }), false);
  assert.equal(isChatPanelMessage({ type: "submit", text: "x".repeat(20_001), requestId: "r" }), false);
  assert.equal(isChatPanelMessage({ type: "slash.quickAction", command: `/${"x".repeat(4_096)}`, requestId: "r" }), false);
  assert.equal(isChatPanelMessage({ type: "approval", approvalId: "x".repeat(1_025), decision: "deny" }), false);
  assert.equal(isChatPanelMessage({ type: "forge.executePreview", auto: "yes" }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.start", parallel: 0 }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.start", parallel: 9 }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.start", parallel: 2.5 }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.regenerate", taskId: "task-1", instruction: "x".repeat(20_001) }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.recovery.resume", jobId: "", revision: 7 }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.recovery.resume", jobId: "job-1", revision: -1 }), false);
  assert.equal(isChatPanelMessage({ type: "swarm.recovery.dismiss", jobId: "job-1", revision: 1.5 }), false);
  assert.equal(isChatPanelMessage({ type: "session.model.set", model: "x".repeat(513) }), false);
  assert.equal(isChatPanelMessage({ type: "browser.start", allow_local_destinations: true }), false);
  assert.equal(isChatPanelMessage({ type: "browser.start", executable_path: "chrome" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.startLocal", network_scope: "public_loopback" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.startLocal", confirm: true }), false);
  assert.equal(isChatPanelMessage({ type: "browser.navigate", url: "x".repeat(8_193) }), false);
  assert.equal(isChatPanelMessage({ type: "browser.snapshot", kind: "raw-cdp" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.type", selector: "#field", text: "" }), false);
  assert.equal(isChatPanelMessage({ type: "browser.screenshot.save", destination: "C:/secret.png" }), false);
});

test("every message case is a closed schema: unexpected keys are rejected uniformly", () => {
  // Previously approval, artifact.open, forge.*, swarm.*, profile.use, session.model.set,
  // composerMode.set, and mode.set accepted extra keys. No handler spreads a message today, but the
  // validator is the boundary — it must not depend on that staying true.
  const withExtras: unknown[] = [
    { type: "approval", approvalId: "a1", decision: "deny", allow: true },
    { type: "artifact.open", artifactId: "a.txt", path: "/etc/passwd" },
    { type: "forge.executePreview", auto: true, mode: "auto" },
    { type: "forge.diff.open", diffId: "diff-1", uri: "file:///etc/passwd" },
    { type: "forge.review", taskId: "t-1", approve: true },
    { type: "forge.assets.open", assetId: "a-1", path: "/etc/passwd" },
    { type: "forge.artifact.open", sessionId: "s", artifactId: "a", destination: "/tmp/x" },
    { type: "forge.approval", sessionId: "s", approvalId: "a", decision: "deny", allow: true },
    { type: "swarm.start", parallel: 2, mode: "auto" },
    { type: "swarm.apply", taskId: "t-1", force: true },
    { type: "swarm.discard", taskId: "t-1", force: true },
    { type: "swarm.regenerate", taskId: "t-1", instruction: "redo", mode: "auto" },
    { type: "swarm.recovery.resume", jobId: "job-1", revision: 1, force: true },
    { type: "swarm.recovery.dismiss", jobId: "job-1", revision: 1, force: true },
    { type: "profile.use", name: "openai", apiKey: "sk-live" },
    { type: "session.model.set", model: "gpt-5", baseUrl: "https://evil.example" },
    { type: "persona.set", name: "architect", mode: "auto" },
    { type: "composerMode.set", mode: "chat", sandbox: "none" },
    { type: "mode.set", mode: "readonly", sandbox: "none" },
    { type: "cancel", sessionId: "other-session" },
    { type: "bridge.restart", cliPath: "/tmp/evil" },
    { type: "cockpit.retryStatus", cliPath: "/tmp/evil" }
  ];

  for (const message of withExtras) {
    assert.equal(isChatPanelMessage(message), false, `accepted extra keys: ${JSON.stringify(message)}`);
  }
});
