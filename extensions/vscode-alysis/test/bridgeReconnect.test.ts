import assert from "node:assert/strict";
import test from "node:test";

import { BoundedBridgeReconnect, ReconnectTimers } from "../src/client/bridgeReconnect";

// A deterministic timer: it records each scheduled delay and stores the pending callback so the
// test drives the backoff loop by hand (no real timers, no flakiness).
function fakeTimers(): ReconnectTimers & { delays: number[]; fire(): boolean; cancelled: number } {
  let pending: (() => void) | null = null;
  const harness = {
    delays: [] as number[],
    cancelled: 0,
    schedule(callback: () => void, delayMs: number): unknown {
      harness.delays.push(delayMs);
      pending = callback;
      return 1;
    },
    cancel(): void {
      harness.cancelled += 1;
      pending = null;
    },
    fire(): boolean {
      if (!pending) {
        return false;
      }
      const callback = pending;
      pending = null;
      callback();
      return true;
    }
  };
  return harness;
}

async function drain(timers: { fire(): boolean }): Promise<void> {
  // Fire the pending callback, then flush the microtasks the async run()/probe chain queues so the
  // next attempt is scheduled before the next fire().
  for (let i = 0; i < 20 && timers.fire(); i += 1) {
    await Promise.resolve();
    await Promise.resolve();
  }
}

test("BoundedBridgeReconnect retries with increasing backoff and stops at the bound", async () => {
  const timers = fakeTimers();
  let probes = 0;
  const reconnect = new BoundedBridgeReconnect(
    async () => {
      probes += 1;
      return false; // never recovers
    },
    [10, 20, 40],
    timers
  );

  reconnect.arm();
  await drain(timers);

  assert.equal(probes, 3, "exactly three attempts — the bound");
  assert.deepEqual(timers.delays, [10, 20, 40], "increasing backoff, one per attempt");
  assert.equal(reconnect.armed, false, "stops once the bound is exhausted");
});

test("BoundedBridgeReconnect stops and resets the moment a check passes", async () => {
  const timers = fakeTimers();
  let probes = 0;
  const reconnect = new BoundedBridgeReconnect(
    async () => {
      probes += 1;
      return probes >= 2; // recovers on the second attempt
    },
    [10, 20, 40],
    timers
  );

  reconnect.arm();
  await drain(timers);

  assert.equal(probes, 2, "no further attempts after recovery");
  assert.equal(reconnect.attempts, 0, "counter resets on success");
  assert.equal(reconnect.armed, false);
});

test("BoundedBridgeReconnect.reset cancels a pending attempt (manual reconnect wins)", () => {
  const timers = fakeTimers();
  const reconnect = new BoundedBridgeReconnect(async () => false, [10], timers);

  reconnect.arm();
  assert.equal(reconnect.armed, true);

  reconnect.reset();
  assert.equal(timers.cancelled, 1);
  assert.equal(reconnect.armed, false);
  assert.equal(reconnect.attempts, 0);
});

test("BoundedBridgeReconnect.arm is idempotent while a check is pending", () => {
  const timers = fakeTimers();
  const reconnect = new BoundedBridgeReconnect(async () => false, [10, 20], timers);

  reconnect.arm();
  reconnect.arm();
  reconnect.arm();

  assert.deepEqual(timers.delays, [10], "double-arming never stacks timers");
});

test("BoundedBridgeReconnect.reset invalidates an in-flight failed probe", async () => {
  const timers = fakeTimers();
  let finishProbe: ((recovered: boolean) => void) | undefined;
  const reconnect = new BoundedBridgeReconnect(
    () => new Promise<boolean>((resolve) => {
      finishProbe = resolve;
    }),
    [10, 20],
    timers
  );

  reconnect.arm();
  assert.equal(timers.fire(), true, "first retry starts its probe");
  await Promise.resolve();
  assert.ok(finishProbe, "probe is in flight");

  // A manual reconnect or extension disposal resets the automatic loop while the old probe is
  // still awaiting I/O. Its eventual failure must not resurrect a retry timer.
  reconnect.reset();
  finishProbe!(false);
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(reconnect.attempts, 0);
  assert.equal(reconnect.armed, false);
  assert.deepEqual(timers.delays, [10], "stale completion does not schedule the second delay");
});
