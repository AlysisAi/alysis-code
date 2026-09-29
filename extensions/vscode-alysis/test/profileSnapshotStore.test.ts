import assert from "node:assert/strict";
import test from "node:test";

import { ProfileSnapshotStore } from "../src/backend/ProfileSnapshotStore";

function fakeBridge(options: { supports?: boolean; active?: string; profiles?: unknown[]; throws?: boolean }): any {
  return {
    supportsMethod: () => options.supports !== false,
    profileList: async () => {
      if (options.throws) {
        throw new Error("bridge unavailable");
      }
      return { active_profile: options.active ?? "", profiles: options.profiles ?? [] };
    }
  };
}

test("ProfileSnapshotStore.refresh maps + redacts profiles and marks the active one", async () => {
  let changes = 0;
  const store = new ProfileSnapshotStore(() => {
    changes += 1;
  });
  await store.refresh(
    fakeBridge({
      active: "openai",
      profiles: [
        { name: "openai", base_url: "https://api.openai.com", default_model: "gpt", api_key: "sk-ABCDEFGHIJ1234567890" },
        { name: "local", base_url: "http://localhost:1234", model: "llama" }
      ]
    })
  );
  const snapshot = store.snapshot();
  assert.equal(snapshot.supported, true);
  assert.equal(snapshot.activeProfile, "openai");
  assert.equal(snapshot.profiles.length, 2);
  assert.deepEqual(snapshot.profiles[0], {
    name: "openai",
    baseUrl: "https://api.openai.com",
    model: "gpt",
    active: true
  });
  assert.equal(snapshot.profiles[1].active, false);
  assert.equal(snapshot.profiles[1].model, "llama", "falls back to model when default_model is absent");
  // The api_key secret never enters the render-safe snapshot.
  assert.doesNotMatch(JSON.stringify(snapshot), /sk-[A-Za-z0-9]{12}/);
  assert.ok(changes >= 1, "onChange fired");
});

test("ProfileSnapshotStore.refresh collapses to unsupported when the bridge lacks profile.list", async () => {
  const store = new ProfileSnapshotStore(() => undefined);
  await store.refresh(fakeBridge({ supports: false, profiles: [{ name: "x" }] }));
  assert.deepEqual(store.snapshot(), { supported: false, activeProfile: "", profiles: [] });
});

test("ProfileSnapshotStore.refresh collapses to unsupported on a bridge error", async () => {
  const store = new ProfileSnapshotStore(() => undefined);
  await store.refresh(fakeBridge({ throws: true }));
  assert.equal(store.snapshot().supported, false);
});

test("ProfileSnapshotStore drops nameless profiles and returns copies", async () => {
  const store = new ProfileSnapshotStore(() => undefined);
  await store.refresh(fakeBridge({ active: "x", profiles: [{ base_url: "u" }, { name: "x" }] }));
  const snapshot = store.snapshot();
  assert.equal(snapshot.profiles.length, 1, "a profile without a name is dropped");
  (snapshot.profiles[0] as { name: string }).name = "tampered";
  assert.equal(store.snapshot().profiles[0].name, "x", "snapshot is a copy — external mutation does not leak");
});
