// ESLint flat config (CommonJS — the package is CommonJS). Lean + predictable:
// TypeScript parser over the .ts sources, with a small set of high-value rules
// that complement the strict tsconfig (which already does the heavy type checking).
const tseslint = require("typescript-eslint");

const sharedRules = {
  "@typescript-eslint/no-unused-vars": [
    "error",
    { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" }
  ],
  "prefer-const": ["error", { ignoreReadBeforeAssign: true }],
  "no-var": "error",
  eqeqeq: ["error", "smart"]
};

module.exports = tseslint.config(
  {
    // media/** is no longer ignored (FE-9) — the webview JS is now linted; only build output,
    // the dev-only webview type stub, and config files are excluded.
    ignores: ["out/**", ".vscode-test/**", ".qa-ui-out/**", "node_modules/**", "*.vsix", "eslint.config.js", "media/**/*.d.ts"]
  },
  {
    files: ["src/**/*.ts", "test/**/*.ts"],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { ecmaVersion: 2022, sourceType: "module" }
    },
    plugins: { "@typescript-eslint": tseslint.plugin },
    rules: sharedRules
  },
  {
    // The webview scripts are classic browser <script>s (no import/export); type-checked separately
    // via tsconfig.webview.json. Same high-value rules apply.
    files: ["media/**/*.js"],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: { ecmaVersion: 2022, sourceType: "script" }
    },
    plugins: { "@typescript-eslint": tseslint.plugin },
    rules: sharedRules
  }
);
