"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { PortableChat } = require("../portable-chat.js");
const { JSDOM, VirtualConsole } = require("../../vscode-alysis/node_modules/jsdom");
const METHODS = ["session.create", "chat.send", "session.cancel", "approval.respond", "job.status", "session.setMode"];
function fixture(override) {
  const calls = [], messages = [];
  const controller = new PortableChat(async (method, params) => {
    calls.push({ method, params });
    if (override) { const value = await override(method, params); if (value !== undefined) return value; }
    if (method === "initialize") return { protocol_version: "1", capabilities: { methods: METHODS } };
    if (method === "session.create") return { session_id: "session-1", mode: params.mode };
    if (method === "chat.send") return { job_id: "job-1", status: "running" };
    if (method === "session.cancel") return { status: "cancellation_requested" };
    if (method === "approval.respond") return { status: "applied" };
    if (method === "job.status") return { status: "running" };
    if (method === "session.setMode") return { mode: params.mode };
    throw new Error(method);
  }, value => messages.push(value), async () => true);
  return { controller, calls, messages };
}
const task = (id = "request-1", instruction = "Inspect the project") => ({ type: "task.submit", requestId: id, instruction, mode: "review" });
function event(sequence, type, payload, session_id = "session-1", job_id = "job-1") {
  return { protocol_version: "1", sequence, type, payload, session_id, job_id };
}
test("startup is passive and requires an explicit connect before sending", async () => {
  const { controller, calls, messages } = fixture();
  await controller.handle({ type: "ready" });
  assert.equal(calls.length, 0);
  await controller.handle(task());
  assert.equal(calls.length, 0);
  assert.equal(messages.find(m => m.type === "task.result").started, false);
  controller.dispose();
});
test("incompatible capabilities cannot enable chat", async () => {
  const { controller } = fixture(method => method === "initialize" ? { protocol_version: "1", capabilities: { methods: ["session.create"] } } : undefined);
  await controller.connect(); assert.equal(controller.connected, false); controller.dispose();
});
test("deduplicates submissions and ignores foreign or replayed events", async () => {
  const { controller, calls } = fixture();
  await controller.connect(); await Promise.all([controller.handle(task()), controller.handle(task())]);
  assert.equal(calls.filter(c => c.method === "chat.send").length, 1);
  controller.onEvent(event(1, "message_delta", { text: "foreign" }, "other"));
  controller.onEvent(event(1, "message_delta", { text: "Hello " }));
  controller.onEvent(event(1, "message_delta", { text: "duplicate" }));
  controller.onEvent(event(2, "message_delta", { text: "other job" }, "session-1", "other-job"));
  controller.onEvent(event(2, "message_end", { text: "Hello world" }));
  assert.equal(controller.items.find(i => i.kind === "assistant").text, "Hello world");
  controller.dispose();
});
test("rejecting an overlapping send must not make a running task idle", async () => {
  const { controller, calls } = fixture();
  await controller.connect(); await controller.handle(task()); await controller.handle(task("second"));
  assert.equal(controller.busy, true); assert.equal(controller.jobId, "job-1");
  assert.equal(calls.filter(c => c.method === "chat.send").length, 1); controller.dispose();
});
test("approvals require an owned pending prompt and respect session-scope support", async () => {
  const { controller, calls } = fixture();
  await controller.connect(); await controller.handle(task());
  const approval = { kind: "approval", approval_kind: "write_file", approval_id: "approval-1", reason: "Write README", allow_for_session_supported: false };
  await controller.handle({ type: "approval", approvalId: "foreign", decision: "allow_once" });
  controller.onEvent(event(1, "prompt_for_input", approval));
  await controller.handle({ type: "approval", approvalId: "approval-1", decision: "allow_for_session" });
  assert.equal(calls.filter(c => c.method === "approval.respond").length, 0);
  await controller.handle({ type: "approval", approvalId: "approval-1", decision: "deny" });
  assert.deepEqual(calls.find(c => c.method === "approval.respond").params, { session_id: "session-1", approval_id: "approval-1", allow: false, allow_for_session: false });
  await controller.handle({ type: "approval", approvalId: "approval-1", decision: "allow_once" });
  assert.equal(calls.filter(c => c.method === "approval.respond").length, 1); controller.dispose();
});
test("cancellation remains active until the backend reports a terminal state", async () => {
  const { controller } = fixture(); await controller.connect(); await controller.handle(task());
  await controller.cancel(); assert.equal(controller.busy, true); assert.equal(controller.status, "cancellation_requested");
  controller.finish("cancelled"); assert.equal(controller.busy, false); controller.dispose();
});
test("disconnect expires approvals and fences delayed events", async () => {
  const { controller } = fixture(); await controller.connect(); await controller.handle(task());
  controller.onEvent(event(1, "prompt_for_input", { kind: "approval", approval_id: "a" }));
  controller.disconnected("Disconnected");
  controller.onEvent(event(2, "message_delta", { text: "stale" }));
  assert.equal(controller.items.find(i => i.kind === "approval").status, "expired");
  assert.equal(controller.items.some(i => i.text === "stale"), false); controller.dispose();
});
test("a delayed connection cannot enable chat after disconnect", async () => {
  let complete;
  const { controller } = fixture(method => method === "initialize" ? new Promise(resolve => { complete = resolve; }) : undefined);
  const pending = controller.connect();
  controller.disconnected("Connection replaced");
  complete({ protocol_version: "1", capabilities: { methods: METHODS } });
  await pending;
  assert.equal(controller.connected, false);
  assert.equal(controller.error, "Connection replaced");
  controller.dispose();
});
test("a follow-up task applies its permissions before sending", async () => {
  const { controller, calls } = fixture();
  await controller.connect(); await controller.handle(task()); controller.finish("completed");
  await controller.handle({ ...task("read-only-followup"), mode: "readonly" });
  const change = calls.findIndex(c => c.method === "session.setMode");
  assert.ok(change > 0);
  assert.equal(calls[change].params.mode, "readonly");
  assert.equal(calls[change + 1].method, "chat.send");
  assert.equal(controller.mode, "readonly");
  controller.dispose();
});
test("the production renderer mounts with the portable host, with no VS Code API", async () => {
  const errors = [];
  const virtualConsole = new VirtualConsole(); virtualConsole.on("jsdomError", e => errors.push(e));
  const markup = readFileSync(resolve(__dirname, "../startView.html"), "utf8").replace(/<script[\s\S]*?<\/script>/g, "");
  const dom = new JSDOM(markup, { runScripts: "outside-only", pretendToBeVisual: true, virtualConsole });
  const { controller } = fixture();
  controller.emit = data => dom.window.dispatchEvent(new dom.window.MessageEvent("message", { data }));
  let saved;
  dom.window.alysisHost = { postMessage: m => { void controller.handle(m); }, getState: () => saved, setState: v => { saved = v; } };
  try {
    dom.window.eval(readFileSync(resolve(__dirname, "../startView.js"), "utf8"));
    await controller.connect(); await controller.handle(task());
    controller.onEvent(event(1, "message_end", { text: "Shared sidebar works" }));
    assert.match(dom.window.document.querySelector("#conversationItems").textContent, /Shared sidebar works/);
    assert.equal(dom.window.acquireVsCodeApi, undefined); assert.deepEqual(errors, []);
  } finally { controller.dispose(); dom.window.close(); }
});

test("Cockpit Git changes button names structured review and emits only the existing review action", () => {
  const markup = readFileSync(resolve(__dirname, "../startView.html"), "utf8").replace(/<script[\s\S]*?<\/script>/g, "");
  const dom = new JSDOM(markup, { runScripts: "outside-only", pretendToBeVisual: true });
  const messages = [];
  dom.window.alysisHost = { postMessage: message => messages.push(message), getState: () => undefined, setState: () => undefined };
  try {
    dom.window.eval(readFileSync(resolve(__dirname, "../startView.js"), "utf8"));
    const button = dom.window.document.querySelector("#barGitChanges");
    assert.equal(button.getAttribute("aria-label"), "Review Git Changes");
    assert.match(button.title, /structured code review/);
    messages.length = 0;
    button.click();
    assert.deepEqual(JSON.parse(JSON.stringify(messages)), [{ type: "worktree", action: "review" }]);
    assert.doesNotMatch(button.title, /Source Control/);
  } finally { dom.window.close(); }
});
