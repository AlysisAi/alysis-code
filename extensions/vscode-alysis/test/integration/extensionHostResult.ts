export function assertSuccessfulExtensionHostResult(value: unknown): void {
  if (typeof value !== "object" || value === null) throw new Error("Invalid Extension Host test result.");
  const result = value as Record<string, unknown>;
  for (const key of ["tests", "passes", "pending", "failures"]) {
    if (typeof result[key] !== "number" || !Number.isSafeInteger(result[key]) || result[key] < 0) {
      throw new Error("Invalid Extension Host test result.");
    }
  }
  const { tests, passes, pending, failures } = result as Record<string, number>;
  if (failures > 0) throw new Error(`${failures} Extension Host test(s) failed despite the editor exiting successfully.`);
  if (tests < 1 || passes < 1 || passes + pending !== tests) {
    throw new Error("Extension Host did not complete any passing tests or reported inconsistent totals.");
  }
}
