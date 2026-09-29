import { ProfileOption, ProviderProfileState } from "../chat/CockpitState";
import { redactDeep } from "../client/CliDiscovery";
import { AlysisBridgeClient } from "../client/AlysisBridgeClient";

// Caches the bridge's profile.list (the provider/profile picker is a synchronous render off cockpit
// state, so it cannot fetch on open). refresh() pulls + redacts the list; snapshot() returns the
// cached, render-safe projection; each change fires onChange (-> refreshCockpit) so the header pill
// reflects the active profile after a switch. profile.list is read-only (no trust/confirm needed).
export class ProfileSnapshotStore {
  private state: ProviderProfileState = { supported: false, activeProfile: "", profiles: [] };

  public constructor(private readonly onChange: () => void) {}

  public snapshot(): ProviderProfileState {
    return { ...this.state, profiles: this.state.profiles.map((profile) => ({ ...profile })) };
  }

  public async refresh(bridge: AlysisBridgeClient): Promise<void> {
    // Only attempt when the bridge advertises profile.list — avoids spawning/erroring on a CLI that
    // does not support it. A failure (bridge down, unsupported) collapses to an unsupported snapshot.
    if (!bridge.supportsMethod("profile.list")) {
      this.set({ supported: false, activeProfile: "", profiles: [] });
      return;
    }
    try {
      const result = await bridge.profileList();
      const active = typeof result.active_profile === "string" ? result.active_profile : "";
      const rawProfiles = Array.isArray(result.profiles) ? result.profiles : [];
      const profiles: ProfileOption[] = rawProfiles
        .map((entry) => {
          // Redact each profile before it crosses to the webview (defense-in-depth; base_url/model
          // are not redacted by redactDeep so they still render).
          const redacted = (redactDeep(entry) ?? {}) as Record<string, unknown>;
          const name = stringField(redacted, "name");
          return {
            name,
            baseUrl: stringField(redacted, "base_url"),
            model: stringField(redacted, "default_model") || stringField(redacted, "model"),
            active: name.length > 0 && name === active
          };
        })
        .filter((profile) => profile.name.length > 0);
      this.set({ supported: true, activeProfile: active, profiles });
    } catch {
      this.set({ supported: false, activeProfile: "", profiles: [] });
    }
  }

  public clear(): void {
    this.set({ supported: false, activeProfile: "", profiles: [] });
  }

  private set(state: ProviderProfileState): void {
    this.state = state;
    this.onChange();
  }
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}
