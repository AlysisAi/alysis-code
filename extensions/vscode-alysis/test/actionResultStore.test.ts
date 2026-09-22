import assert from "node:assert/strict";
import test from "node:test";

import { ActionResultStore } from "../src/backend/ActionResultStore";

test("start records a running, non-cancellable entry and returns its id", () => {
  let changes = 0;
  const store = new ActionResultStore(() => {
    changes += 1;
  });
  const id = store.start("session.usage", "Session usage", false);
  const entries = store.list();
  assert.equal(entries.length, 1);
  assert.equal(entries[0].id, id);
  assert.equal(entries[0].status, "running");
  assert.equal(entries[0].cancellable, false, "backend actions are not cancellable (cancel_not_supported)");
  assert.equal(entries[0].payload, null);
  assert.equal(changes, 1, "onChange fired on start");
});

test("finish redacts the structured payload field-by-field and transitions to ok", () => {
  const store = new ActionResultStore(() => undefined);
  const id = store.start("config.get", "Get config", false);
  store.finish(id, {
    model: "opus",
    api_key: "sk-ABCDEFGHIJ1234567890",
    nested: { authorization: "Bearer ABCDEFGHIJKLMN", note: "ok" },
    profiles: [{ name: "default", token: "sk-ZZZZZZZZZZ9999999999" }]
  });
  const entry = store.list()[0];
  assert.equal(entry.status, "ok");
  const payload = entry.payload as any;
  // Structure preserved, secrets redacted at every depth.
  assert.equal(payload.model, "opus");
  assert.equal(payload.api_key, "<redacted>");
  assert.match(payload.nested.authorization, /<redacted>/);
  assert.equal(payload.nested.note, "ok");
  assert.equal(payload.profiles[0].name, "default");
  assert.equal(payload.profiles[0].token, "<redacted>");
  // No raw secret survives anywhere in the serialized payload.
  assert.doesNotMatch(JSON.stringify(payload), /sk-[A-Za-z0-9]{12}/);
});

test("fail redacts the message and transitions to error", () => {
  const store = new ActionResultStore(() => undefined);
  const id = store.start("config.set", "Set config", true);
  store.fail(id, "request failed: authorization: sk-ABCDEFGHIJ1234567890");
  const entry = store.list()[0];
  assert.equal(entry.status, "error");
  assert.equal(entry.payload, null);
  assert.match(entry.error ?? "", /request failed/);
  assert.match(entry.error ?? "", /<redacted>/);
  assert.doesNotMatch(entry.error ?? "", /sk-[A-Za-z0-9]{12}/);
});

test("onChange fires on every lifecycle transition", () => {
  let changes = 0;
  const store = new ActionResultStore(() => {
    changes += 1;
  });
  const id = store.start("tool.list", "List tools", false);
  store.finish(id, { tools: [] });
  store.fail("does-not-exist", "ignored"); // no matching entry -> no fire
  assert.equal(changes, 2, "start + finish fired; a no-op update does not");
});

test("the store is capped so the published state stays small", () => {
  const store = new ActionResultStore(() => undefined, 5);
  for (let i = 0; i < 12; i += 1) {
    store.start("action." + i, "Action " + i, false);
  }
  const entries = store.list();
  assert.equal(entries.length, 5, "cap enforced");
  // The most recent entries are retained.
  assert.equal(entries[entries.length - 1].actionId, "action.11");
});

test("list returns copies — callers cannot mutate stored entries", () => {
  const store = new ActionResultStore(() => undefined);
  const id = store.start("skill.list", "List skills", false);
  const snapshot = store.list();
  (snapshot[0] as any).status = "tampered";
  store.finish(id, {});
  assert.equal(store.list()[0].status, "ok", "external mutation did not leak into the store");
});
