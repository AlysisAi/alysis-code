import assert from "node:assert/strict";
import test from "node:test";
import { consumeWorktreeHandoff, worktreeHandoffKey } from "../src/workspace/WorktreeHandoff";

const memory = () => {
  const values = new Map<string, any>();
  return { keys: () => [...values.keys()], get: <T>(key: string) => values.get(key) as T,
    update: async (key: string, value: any) => { if (value === undefined) values.delete(key); else values.set(key, value); } };
};

test("worktree handoff survives a storage failure and is consumed exactly once by its destination", async () => {
  const global = memory();
  const workspace = memory();
  const root = "/worktree/example";
  const value = { root, createdAt: Date.now(), reference: { sessionId: "session-1", mode: "readonly", workspace: { root, scheme: "file", authority: "" } } };
  await global.update(worktreeHandoffKey(root), value);
  await assert.rejects(consumeWorktreeHandoff(global, { ...workspace, update: async () => { throw new Error("disk full"); } }, root), /disk full/);
  assert.equal(global.get(worktreeHandoffKey(root)), value);
  assert.equal(await consumeWorktreeHandoff(global, workspace, "/wrong/worktree"), undefined);
  assert.deepEqual(await consumeWorktreeHandoff(global, workspace, root), value);
  assert.deepEqual(workspace.get("alysis.activeSessionId"), value.reference);
  assert.equal(await consumeWorktreeHandoff(global, workspace, root), undefined);
});

test("worktree handoff refuses a mismatched session root and expired records", async () => {
  const global = memory();
  const workspace = memory();
  const root = "/worktree/example";
  await global.update(worktreeHandoffKey(root), { root, createdAt: Date.now(), reference: { sessionId: "session-1", workspace: { root: "/unrelated", scheme: "file", authority: "" } } });
  assert.equal(await consumeWorktreeHandoff(global, workspace, root), undefined);
  assert.deepEqual(workspace.keys(), []);
  await global.update(worktreeHandoffKey(root), { root, createdAt: 0 });
  assert.equal(await consumeWorktreeHandoff(global, workspace, root), undefined);
  assert.deepEqual(global.keys(), []);
});
