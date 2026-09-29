// Ambient types for the cockpit webview scripts — checked via tsconfig.webview.json. This file is a
// dev-only artifact (excluded from the VSIX by .vscodeignore) describing the browser globals the
// webview runs against: the VS Code webview API injected as a global, the cockpit's own helper
// modules exposed on `window`, and the optional CommonJS `module` the dual browser/Node helper
// modules feature-detect for their require()-based unit tests.

declare function acquireVsCodeApi(): {
  postMessage(message: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
};

// The helper modules (chatPanelViewModel/Dom/Slash) expose dynamically-shaped APIs; typed loosely
// here so the consuming render code is null/DOM-checked without over-constraining the helpers.
interface Window {
  alysisHost?: ReturnType<typeof acquireVsCodeApi>;
  AlysisCockpitView: Record<string, (...args: any[]) => any>;
  AlysisCockpitDom: Record<string, (...args: any[]) => any>;
  AlysisSlash: Record<string, (...args: any[]) => any>;
}

// The helper modules guard on `typeof module !== "undefined"` to support require() in tests.
declare const module: { exports: unknown } | undefined;
