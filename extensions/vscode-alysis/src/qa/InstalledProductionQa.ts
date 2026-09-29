export const INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT =
  "ALYSIS_INSTALLED_PRODUCTION_QA_ONCE";

/**
 * Consume the installed-production QA opt-in before activation performs any asynchronous work.
 * Merely setting the flag in an Extension Development/Test host never exposes the production QA
 * API, and every value (including an invalid one) is deleted after this single check.
 */
export function consumeInstalledProductionQaFlag(
  productionMode: boolean,
  environment: NodeJS.ProcessEnv = process.env
): boolean {
  const value = environment[INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT];
  delete environment[INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT];
  return productionMode && value === "1";
}
