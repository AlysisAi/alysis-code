import { redactDeep, redactForDisplay } from "../client/CliDiscovery";
import { ActionResultEntry } from "../chat/CockpitState";

// The reusable action lifecycle for the ~53 management actions: start() records a "running" entry,
// finish() transitions it to "ok" with the redacted structured payload, fail() to "error" with a
// redacted message. Each mutation fires onChange (-> refreshCockpit) so the cockpit re-renders the
// running/ok/error card. The store is the single source of truth folded into CockpitState; it does
// NOT reach the webview itself. Capped to keep the published state small. FE-12/FE-13 reuse this.
export class ActionResultStore {
  private entries: ActionResultEntry[] = [];
  private sequence = 0;

  public constructor(
    private readonly onChange: () => void,
    private readonly cap = 50
  ) {}

  /** Record a running action and return its id. Call AFTER param collection / confirmation so the
   *  running card does not appear while a modal prompt is still open. */
  public start(actionId: string, title: string, mutates: boolean): string {
    const id = `action-${++this.sequence}`;
    const entry: ActionResultEntry = {
      id,
      actionId,
      title,
      status: "running",
      mutates,
      cancellable: false, // backend actions have no cancel path — never offer a fake cancel
      payload: null,
      error: null
    };
    this.entries = [...this.entries, entry].slice(-this.cap);
    this.onChange();
    return id;
  }

  public finish(id: string, payload: unknown): void {
    this.update(id, (entry) => ({ ...entry, status: "ok", payload: redactDeep(payload), error: null }));
  }

  public fail(id: string, message: string): void {
    this.update(id, (entry) => ({ ...entry, status: "error", payload: null, error: redactForDisplay(message) }));
  }

  public list(): ActionResultEntry[] {
    return this.entries.map((entry) => ({ ...entry }));
  }

  private update(id: string, transform: (entry: ActionResultEntry) => ActionResultEntry): void {
    let changed = false;
    this.entries = this.entries.map((entry) => {
      if (entry.id !== id) {
        return entry;
      }
      changed = true;
      return transform(entry);
    });
    if (changed) {
      this.onChange();
    }
  }
}
