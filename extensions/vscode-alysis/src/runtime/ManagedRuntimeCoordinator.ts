import type {
  ResolvedCliPath,
  AlysisConfig,
  AlysisRuntimeSelection
} from "../client/CliDiscovery";
import { CLI_INSTALL_COMMAND, SETUP_GUIDE_URL } from "../client/compatibility";
import {
  InstalledRuntime,
  ManagedCliError,
  ManagedCliRuntime
} from "./ManagedCliRuntime";

export interface ManagedRuntimeStore {
  getActive(signal?: AbortSignal): Promise<InstalledRuntime>;
  rollback(signal?: AbortSignal): Promise<InstalledRuntime>;
  cleanupOldVersions(maxRetained: number): Promise<string[]>;
  /**
   * Cheap identity (pointer size + mtime) used to skip a revalidation that would repeat work already
   * done in this process. Optional so a caller can supply a minimal store.
   */
  pointerFingerprint?(): Promise<string | undefined>;
}

export interface ManagedRuntimeCoordinatorOptions {
  /** PATH fallback exists only to keep an unpackaged Extension Development Host usable. */
  allowDevelopmentPathFallback?: boolean;
  report?: (message: string) => void;
  /** Reconcile the signed platform bundle on every activation, including extension upgrades. */
  reconcileBundled?: (signal?: AbortSignal) => Promise<InstalledRuntime>;
}

/** Bounded release evidence that is safe to expose without leaking an executable path. */
export interface ManagedRuntimeEvidence {
  origin: "managed" | "unavailable";
  production: boolean;
  artifactVersion?: string;
  cliVersion?: string;
  protocolVersion?: string;
  target?: InstalledRuntime["target"];
  sha256?: string;
  releaseTag?: string;
  sourceRepository?: string;
  sourceCommit?: string;
  releaseSignatureVerified?: boolean;
  message?: string;
  /** Failure classification for the unavailable case, e.g. BUNDLED_RUNTIME_MISSING (recoverable). */
  code?: string;
}

const NO_RUNTIME_MESSAGE =
  "No signed managed Alysis Code CLI runtime is installed. Install the Alysis Code CLI with " +
  `\`${CLI_INSTALL_COMMAND}\` and point the extension at it with the "Alysis Code: Locate CLI" command ` +
  "(alysis.locateCli), or install an Alysis Code release that bundles the managed CLI. Setup guide: " +
  `${SETUP_GUIDE_URL}`;

/** No bundle shipped is a recoverable install problem, not a validation failure. */
const NO_BUNDLED_RUNTIME_CODE = "BUNDLED_RUNTIME_MISSING";
const RECOVERABLE_RECONCILIATION_CODES = new Set<string>([NO_BUNDLED_RUNTIME_CODE, "DOWNGRADE_BLOCKED"]);

/**
 * Owns the small state transition between synchronous VS Code config reads and the managed runtime's
 * asynchronous, cryptographically pinned on-disk validation. It never downloads an artifact: release
 * installation must be driven by a separately authenticated manifest channel.
 */
export class ManagedRuntimeCoordinator {
  private active: InstalledRuntime | undefined;
  private failureMessage = NO_RUNTIME_MESSAGE;
  private failureCode: string | undefined;
  private healthRollbackAttempted = false;
  private reportedSelection = "";
  private pendingRefresh: { cliPath: string; promise: Promise<void> } | undefined;
  private validatedFingerprint: string | undefined;

  constructor(
    private readonly runtime: ManagedRuntimeStore,
    private readonly options: ManagedRuntimeCoordinatorOptions = {}
  ) {}

  /**
   * Validate the managed pointer when no explicit developer override is configured.
   *
   * Single-flighted on purpose: activation, every configuration change, and each bounded reconnect
   * probe calls refresh(), and two overlapping validations contend on the same on-disk install lock
   * inside ONE process — the loser used to burn the full lock timeout and report "Timed out waiting
   * for another managed runtime installation" while nothing was installing. Callers that ask for the
   * same cliPath share the in-flight validation; a different cliPath queues behind it instead of
   * racing it.
   */
  async refresh(baseConfig: AlysisConfig, signal?: AbortSignal): Promise<void> {
    const cliPath = baseConfig.cliPath.trim();
    const pending = this.pendingRefresh;
    if (pending && pending.cliPath === cliPath) {
      await pending.promise;
      return;
    }
    const started = (async () => {
      if (pending) {
        await pending.promise.catch(() => undefined);
      }
      await this.refreshOnce(baseConfig, signal);
    })();
    const entry = { cliPath, promise: started };
    this.pendingRefresh = entry;
    try {
      await started;
    } finally {
      if (this.pendingRefresh === entry) {
        this.pendingRefresh = undefined;
      }
    }
  }

  private async refreshOnce(baseConfig: AlysisConfig, signal?: AbortSignal): Promise<void> {
    if (baseConfig.cliPath.trim()) {
      this.active = undefined;
      this.validatedFingerprint = undefined;
      this.failureMessage = NO_RUNTIME_MESSAGE;
      this.failureCode = undefined;
      this.reportOnce(
        `development:${baseConfig.cliPath}`,
        "Development CLI override is active. This executable is not managed, release-verified, or supported as a production runtime."
      );
      return;
    }
    if (await this.unchangedSinceLastValidation()) {
      return;
    }
    let reconciliationError: unknown;
    if (this.options.reconcileBundled) {
      try {
        const previousArtifact = this.active?.artifactVersion;
        this.active = await this.options.reconcileBundled(signal);
        this.failureMessage = NO_RUNTIME_MESSAGE;
        this.failureCode = undefined;
        if (previousArtifact !== this.active.artifactVersion) {
          this.healthRollbackAttempted = false;
        }
        this.reportOnce(
          `bundled:${this.active.artifactVersion}`,
          `Reconciled verified bundled Alysis Code CLI ${this.active.cliVersion} (${this.active.artifactVersion}).`
        );
        await this.recordValidation();
        void this.runtime.cleanupOldVersions(2).catch(() => undefined);
        return;
      } catch (error) {
        reconciliationError = error;
      }
    }
    try {
      const previousArtifact = this.active?.artifactVersion;
      this.active = await this.runtime.getActive(signal);
      this.failureMessage = NO_RUNTIME_MESSAGE;
      this.failureCode = undefined;
      if (previousArtifact !== this.active.artifactVersion) {
        this.healthRollbackAttempted = false;
      }
      this.reportOnce(
        `managed:${this.active.artifactVersion}`,
        `Using verified managed Alysis Code CLI ${this.active.cliVersion} (${this.active.artifactVersion}).`
      );
      await this.recordValidation();
      // Cleanup is bounded to the manager-owned versions directory and protects current + rollback.
      void this.runtime.cleanupOldVersions(2).catch((error: unknown) => {
        this.options.report?.(`Managed runtime cleanup skipped: ${safeErrorMessage(error)}`);
      });
      if (reconciliationError && !isRecoverableReconciliationError(reconciliationError)) {
        this.options.report?.(`Bundled runtime reconciliation was skipped: ${safeErrorMessage(reconciliationError)}`);
      }
      return;
    } catch (initialError) {
      // A bundle that is ABSENT says nothing about the installed runtime: letting it win here is what
      // produced "The managed Alysis Code CLI failed validation (BUNDLED_RUNTIME_INVALID)" with no
      // install link. Only a bundle that is present and invalid outranks the pointer failure.
      const error: unknown =
        reconciliationError && !isRecoverableReconciliationError(reconciliationError)
          ? reconciliationError
          : initialError;
      this.active = undefined;
      this.validatedFingerprint = undefined;
      this.failureMessage = actionableRuntimeFailure(error);
      this.failureCode = runtimeFailureCode(error, reconciliationError);
      // A corrupt/incompatible current pointer may still have a validated last-known-good runtime.
      if (initialError instanceof ManagedCliError &&
          initialError.code !== "NO_ACTIVE_RUNTIME" && initialError.code !== "CANCELLED") {
        try {
          const fallback = await this.runtime.rollback(signal);
          this.active = fallback;
          this.failureMessage = NO_RUNTIME_MESSAGE;
          this.failureCode = undefined;
          this.healthRollbackAttempted = true;
          this.reportOnce(
            `rollback:${fallback.artifactVersion}`,
            `Managed runtime validation failed; recovered with last-known-good CLI ${fallback.cliVersion} (${fallback.artifactVersion}).`
          );
          await this.recordValidation();
          return;
        } catch (rollbackError) {
          this.failureMessage = `${this.failureMessage} Automatic rollback was unavailable: ${safeErrorMessage(rollbackError)}`;
        }
      }
      this.reportOnce(`unavailable:${this.failureMessage}`, this.failureMessage);
    }
  }

  /**
   * Skip the whole validation when the pointer is byte-for-byte the one this process already
   * validated. Cryptographic validation stays mandatory for every pointer this process has not
   * already proven; this only avoids repeating a validation (and its install-lock contention) that
   * nothing on disk has invalidated.
   */
  private async unchangedSinceLastValidation(): Promise<boolean> {
    if (!this.active || this.validatedFingerprint === undefined) {
      return false;
    }
    const fingerprint = await this.pointerFingerprint();
    return fingerprint !== undefined && fingerprint === this.validatedFingerprint;
  }

  private async recordValidation(): Promise<void> {
    this.validatedFingerprint = await this.pointerFingerprint();
  }

  private async pointerFingerprint(): Promise<string | undefined> {
    try {
      return await this.runtime.pointerFingerprint?.();
    } catch {
      // An unreadable pointer simply means the next refresh revalidates from scratch.
      return undefined;
    }
  }

  /**
   * Roll back once per Extension Host lifetime after a managed executable passes at-rest validation
   * but fails the live IDE health probe. Repeated failures never toggle between two bad versions.
   */
  async recoverAfterHealthFailure(signal?: AbortSignal): Promise<boolean> {
    if (!this.active || this.healthRollbackAttempted) {
      return false;
    }
    this.healthRollbackAttempted = true;
    const failedArtifact = this.active.artifactVersion;
    try {
      const fallback = await this.runtime.rollback(signal);
      if (fallback.artifactVersion === failedArtifact) {
        return false;
      }
      this.active = fallback;
      this.reportOnce(
        `health-rollback:${failedArtifact}:${fallback.artifactVersion}`,
        `Managed CLI ${failedArtifact} failed IDE health; rolled back to ${fallback.artifactVersion}.`
      );
      void this.runtime.cleanupOldVersions(2).catch(() => undefined);
      return true;
    } catch (error) {
      this.options.report?.(`Managed CLI health rollback was unavailable: ${safeErrorMessage(error)}`);
      return false;
    }
  }

  apply(baseConfig: AlysisConfig): AlysisConfig {
    const explicitPath = baseConfig.cliPath.trim();
    if (explicitPath) {
      return withSelection(baseConfig, {
        origin: "development-override",
        production: false,
        executablePath: baseConfig.security?.cliPath.resolvedExecutablePath ?? explicitPath,
        message: "Development CLI override (not managed or release-verified)."
      });
    }
    if (this.active) {
      const selected = this.active;
      const cliPath: ResolvedCliPath = {
        value: selected.executablePath,
        resolvedExecutablePath: selected.executablePath,
        source: "global",
        trusted: true,
        executionAllowed: true,
        apiKeyForwardingAllowed: true
      };
      return {
        ...baseConfig,
        cliPath: selected.executablePath,
        security: baseConfig.security ? { ...baseConfig.security, cliPath } : undefined,
        runtimeSelection: {
          origin: "managed",
          production: true,
          executablePath: selected.executablePath,
          artifactVersion: selected.artifactVersion,
          cliVersion: selected.cliVersion,
          protocolVersion: selected.protocolVersion,
          message: `Verified managed CLI ${selected.cliVersion} (${selected.artifactVersion}).`
        }
      };
    }
    if (this.options.allowDevelopmentPathFallback) {
      return withSelection(baseConfig, {
        origin: "development-path",
        production: false,
        executablePath: "alysis",
        message: "Extension Development Host PATH fallback (not managed or release-verified)."
      });
    }
    const blockedPath: ResolvedCliPath = {
      value: "alysis",
      resolvedExecutablePath: null,
      source: "default",
      trusted: false,
      executionAllowed: false,
      apiKeyForwardingAllowed: false,
      reason: this.failureMessage
    };
    return {
      ...baseConfig,
      security: baseConfig.security ? { ...baseConfig.security, cliPath: blockedPath } : undefined,
      runtimeSelection: {
        origin: "unavailable",
        production: false,
        message: this.failureMessage
      }
    };
  }

  isManaged(config: AlysisConfig): boolean {
    return config.runtimeSelection?.origin === "managed";
  }

  evidence(): ManagedRuntimeEvidence {
    if (!this.active) {
      return {
        origin: "unavailable",
        production: false,
        message: this.failureMessage,
        ...(this.failureCode ? { code: this.failureCode } : {})
      };
    }
    return {
      origin: "managed",
      production: true,
      artifactVersion: this.active.artifactVersion,
      cliVersion: this.active.cliVersion,
      protocolVersion: this.active.protocolVersion,
      target: this.active.target,
      sha256: this.active.sha256,
      ...(this.active.release
        ? {
            releaseTag: this.active.release.tag,
            sourceRepository: this.active.release.sourceRepository,
            sourceCommit: this.active.release.sourceCommit
          }
        : {}),
      releaseSignatureVerified: this.active.signatureVerified
    };
  }

  private reportOnce(key: string, message: string): void {
    if (this.reportedSelection === key) {
      return;
    }
    this.reportedSelection = key;
    this.options.report?.(message);
  }
}

export function createManagedRuntimeCoordinator(
  runtime: ManagedCliRuntime,
  options?: ManagedRuntimeCoordinatorOptions
): ManagedRuntimeCoordinator {
  return new ManagedRuntimeCoordinator(runtime, options);
}

function withSelection(baseConfig: AlysisConfig, selection: AlysisRuntimeSelection): AlysisConfig {
  return { ...baseConfig, runtimeSelection: selection };
}

function actionableRuntimeFailure(error: unknown): string {
  if (error instanceof ManagedCliError) {
    if (error.code === "NO_ACTIVE_RUNTIME" || error.code === NO_BUNDLED_RUNTIME_CODE) {
      return NO_RUNTIME_MESSAGE;
    }
    return `The managed Alysis Code CLI failed validation (${error.code}): ${error.message}`;
  }
  return `The managed Alysis Code CLI could not be validated: ${safeErrorMessage(error)}`;
}

/** Absent bundle + absent runtime is the recoverable "install the CLI" state, not a broken bundle. */
function isRecoverableReconciliationError(error: unknown): boolean {
  return error instanceof ManagedCliError && RECOVERABLE_RECONCILIATION_CODES.has(error.code);
}

function runtimeFailureCode(error: unknown, reconciliationError: unknown): string {
  if (error instanceof ManagedCliError) {
    if (error.code === "NO_ACTIVE_RUNTIME" &&
        reconciliationError instanceof ManagedCliError &&
        reconciliationError.code === NO_BUNDLED_RUNTIME_CODE) {
      return NO_BUNDLED_RUNTIME_CODE;
    }
    return error.code;
  }
  return "RUNTIME_VALIDATION_FAILED";
}

function safeErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
