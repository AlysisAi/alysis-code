// Bounded, backoff-driven bridge reconnect. After a recoverable bridge-health failure the
// extension re-checks health a few times with increasing delay; when a check passes the cockpit
// clears its recovery cards and flips back to ready with no manual action. The bound prevents
// hammering — once it is exhausted the user falls back to the always-available manual Reconnect.
//
// The timer source is injectable so the bound/backoff is unit-testable without real timers, and so
// the probe can stay side-effect-isolated (it never spawns the CLI itself — refreshBridgeStatus
// re-checks the Workspace-Trust / executable-origin guard on every attempt and bails when blocked).

export interface ReconnectTimers {
  schedule(callback: () => void, delayMs: number): unknown;
  cancel(handle: unknown): void;
}

const realTimers: ReconnectTimers = {
  schedule: (callback, delayMs) => setTimeout(callback, delayMs),
  cancel: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>)
};

export class BoundedBridgeReconnect {
  private handle: unknown = null;
  private attempt = 0;
  // Invalidates callbacks/probes that began before reset(). A timer cancellation cannot stop a
  // callback that has already fired, so without a generation guard an old failed probe can finish
  // after a successful manual reconnect (or disposal) and silently re-arm the retry loop.
  private generation = 0;

  public constructor(
    // Resolves true once the bridge is healthy again. Must NOT throw for an expected failure
    // (it is wrapped defensively), and must itself honor trust/origin gating.
    private readonly probe: () => Promise<boolean>,
    // One delay per retry; its length is the hard attempt bound (e.g. [2000, 4000, 8000] = 3 tries).
    private readonly delaysMs: readonly number[],
    private readonly timers: ReconnectTimers = realTimers
  ) {}

  public get attempts(): number {
    return this.attempt;
  }

  public get armed(): boolean {
    return this.handle !== null;
  }

  /** Schedule the next bounded attempt. No-op if already waiting or the bound is reached. */
  public arm(): void {
    if (this.handle !== null || this.attempt >= this.delaysMs.length) {
      return;
    }
    const delayMs = this.delaysMs[this.attempt];
    const generation = this.generation;
    this.handle = this.timers.schedule(() => {
      if (generation !== this.generation) {
        return;
      }
      this.handle = null;
      this.attempt += 1;
      void this.run(generation);
    }, delayMs);
  }

  /** Cancel any pending attempt and clear the counter — call on a manual reconnect or on success. */
  public reset(): void {
    this.generation += 1;
    if (this.handle !== null) {
      this.timers.cancel(this.handle);
      this.handle = null;
    }
    this.attempt = 0;
  }

  private async run(generation: number): Promise<void> {
    let recovered = false;
    try {
      recovered = await this.probe();
    } catch {
      recovered = false;
    }
    if (generation !== this.generation) {
      return;
    }
    if (recovered) {
      this.reset();
      return;
    }
    this.arm();
  }
}
