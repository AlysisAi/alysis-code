"use strict";
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { createInterface } = require("node:readline");
const { once } = require("node:events");
const { JSDOM, VirtualConsole } = require("../../vscode-alysis/node_modules/jsdom");
const root = resolve(__dirname, "..");
const shared = resolve(root, "../shared-ui");
const [java, classpath, python, workspace] = process.argv.slice(2);
const child = spawn(java, ["-cp", classpath, "com.alysis.ide.TransportHarness", python, resolve(__dirname, "bridge_fixture.py"), workspace], { windowsHide: true, stdio: "pipe" });
let stderr = "";
child.stderr.on("data", value => { stderr = (stderr + value).slice(-6000); });
const exited = once(child, "exit");
const errors = [];
const console = new VirtualConsole(); console.on("jsdomError", value => errors.push(value.message));
const markup = readFileSync(resolve(shared, "startView.html"), "utf8").replace(/<script[\s\S]*?<\/script>/g, "");
const dom = new JSDOM(markup, { runScripts: "outside-only", pretendToBeVisual: true, virtualConsole: console });
const window = dom.window;
let state;
window.addEventListener("message", event => { if (event.data?.type === "state") state = event.data.state; });
window.alysisNativeSend = value => child.stdin.write(value + "\n");
const lines = createInterface({ input: child.stdout });
lines.on("line", line => {
  try { window.dispatchEvent(new window.MessageEvent("message", { data: JSON.parse(line) })); }
  catch (error) { errors.push(error.message); }
});
for (const file of [resolve(shared, "portable-chat.js"), resolve(root, "src/main/resources/jetbrains-host.js"), resolve(shared, "startView.js")]) window.eval(readFileSync(file, "utf8"));
async function until(predicate, label) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (predicate()) return;
    if (child.exitCode !== null) throw new Error("Native test transport stopped: " + stderr);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  throw new Error(`Timed out: ${label}; ${JSON.stringify(state?.conversation)}; ${stderr}`);
}
async function submit(id, instruction) {
  const previousId = state.lastAcceptedTaskRequestId;
  const input = window.document.querySelector("#taskInput");
  input.value = instruction;
  input.dispatchEvent(new window.Event("input", { bubbles: true }));
  window.document.querySelector("#taskForm").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
  await until(() => state?.lastAcceptedTaskRequestId && state.lastAcceptedTaskRequestId !== previousId, `${id} submission acknowledgement`);
  assert.equal(input.value, "", "accepted submissions clear the composer rather than restoring a retry draft");
}
async function main() {
  try {
    assert.equal(state.ready, false);
    const connect = window.document.querySelector('[data-native-settings] [data-command="alysis.showBridgeHealth"]');
    assert.ok(connect, "the preview exposes its implemented native settings");
    connect.click();
    await until(() => state.ready, "connect");
    await submit("approve", "approve");
    await until(() => state.conversation.items.some(item => item.kind === "approval" && item.status === "pending"), "approval");
    let approval = state.conversation.items.find(item => item.kind === "approval" && item.status === "pending");
    assert.match(window.document.querySelector("#conversationItems").textContent, /Review the proposed test change/);
    window.alysisHost.postMessage({ type: "approval", approvalId: approval.approvalId, decision: "allow_once" });
    await until(() => state.conversation.jobStatus === "completed", "approved completion");
    assert.match(window.document.querySelector("#conversationItems").textContent, /Approved test change/);

    await submit("deny", "deny");
    await until(() => state.conversation.items.some(item => item.kind === "approval" && item.status === "pending"), "second approval");
    approval = state.conversation.items.find(item => item.kind === "approval" && item.status === "pending");
    window.alysisHost.postMessage({ type: "approval", approvalId: approval.approvalId, decision: "deny" });
    await until(() => state.conversation.jobStatus === "completed", "denied completion");
    assert.match(window.document.querySelector("#conversationItems").textContent, /Denied test change/);

    await submit("cancel", "cancel");
    window.alysisHost.postMessage({ type: "task.cancel" });
    await until(() => state.conversation.jobStatus === "cancelled", "cooperative cancellation");
    assert.equal(state.conversation.running, false);
    window.alysisHost.postMessage({ type: "action", action: "new" });
    await until(() => state.conversation.sessionId === null, "new conversation");
    assert.equal(state.conversation.items.length, 0);
    assert.deepEqual(errors, []);
    process.stdout.write("PASS: shared sidebar -> JetBrains JS host -> production Java transport -> real Python bridge; stream, approve, deny, cancel and new task.\n");
  } finally {
    window.dispatchEvent(new window.Event("pagehide")); dom.window.close(); child.stdin.end();
    const timeout = setTimeout(() => child.kill(), 8000);
    await exited; clearTimeout(timeout); lines.close();
  }
}
main().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
