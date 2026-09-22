import { AlysisConfig, evaluateCliExecution } from "../client/CliDiscovery";

export type ProcessExecutionCommand =
  | "showBridgeHealth"
  | "runDoctor"
  | "openChat"
  | "runTask"
  | "bridgeStart"
  | "autoStartBridge"
  | "runtimeEvidence";

export interface CommandGuardResult {
  allowed: boolean;
  reason?: string;
}

export const PROCESS_EXECUTION_COMMANDS: readonly ProcessExecutionCommand[] = [
  "showBridgeHealth",
  "runDoctor",
  "openChat",
  "runTask",
  "bridgeStart",
  "autoStartBridge",
  "runtimeEvidence"
];

export function evaluateProcessExecutionCommand(
  config: AlysisConfig,
  _command: ProcessExecutionCommand
): CommandGuardResult {
  const guard = evaluateCliExecution(config);
  if (!guard.allowed) {
    return {
      allowed: false,
      reason: guard.reason ?? "Alysis Code CLI execution is blocked by Workspace Trust."
    };
  }
  return { allowed: true };
}
