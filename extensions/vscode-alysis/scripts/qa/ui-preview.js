/* Local visual QA of the production webview. Fixture bridge; never contacts a provider. */
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const port = Number(process.env.ALYSIS_UI_PORT || 4317);
const dark = {
  "sideBar-background": "#181818", "sideBar-foreground": "#cccccc", foreground: "#cccccc",
  "editor-background": "#1f1f1f", "descriptionForeground": "#9d9d9d", "input-background": "#242424",
  "input-foreground": "#cccccc", "input-placeholderForeground": "#989898", "widget-border": "#363636",
  "focusBorder": "#0078d4", "button-background": "#0078d4", "button-foreground": "#ffffff",
  "button-hoverBackground": "#026ec1", "list-hoverBackground": "#2a2d2e", "textLink-foreground": "#4daafc",
  "textCodeBlock-background": "#202020", "editorWarning-foreground": "#cca700", "errorForeground": "#f48771",
  "testing-iconPassed": "#89d185", "gitDecoration-addedResourceForeground": "#81b88b",
  "gitDecoration-deletedResourceForeground": "#c74e39", "diffEditor-insertedLineBackground": "#9ccc2c20",
  "diffEditor-removedLineBackground": "#ff000020", "scrollbarSlider-background": "#79797966"
};
const light = {
  ...dark, "sideBar-background": "#f8f8f8", "sideBar-foreground": "#3b3b3b", foreground: "#3b3b3b",
  "editor-background": "#ffffff", "descriptionForeground": "#616161", "input-background": "#ffffff",
  "input-foreground": "#3b3b3b", "input-placeholderForeground": "#767676", "widget-border": "#d4d4d4",
  "list-hoverBackground": "#e8e8e8", "textLink-foreground": "#005fb8", "textCodeBlock-background": "#f0f0f0",
  "testing-iconPassed": "#388a34", "errorForeground": "#b5200d", "editorWarning-foreground": "#856404",
  "gitDecoration-addedResourceForeground": "#287b31", "gitDecoration-deletedResourceForeground": "#a1260d"
};
const contrast = {
  ...dark, "sideBar-background": "#000000", "editor-background": "#000000", foreground: "#ffffff",
  "sideBar-foreground": "#ffffff", "descriptionForeground": "#ffffff", "input-background": "#000000",
  "input-foreground": "#ffffff", "widget-border": "#ffffff", "contrastBorder": "#ffffff",
  "focusBorder": "#f38518", "button-background": "#000000", "button-foreground": "#ffffff"
};
function previewHtml(theme) {
  const source = fs.readFileSync(path.join(root, "media/startView.html"), "utf8");
  let html = source;
  html = html.replace(/<meta http-equiv="Content-Security-Policy"[^>]+>/, "")
    .replaceAll("${styleUri}", "/media/startView.css").replaceAll("${scriptUri}", "/media/startView.js")
    .replaceAll("${markUri}", "/resources/alysis-logo.png").replaceAll("${assetVersion}", "preview").replaceAll("${nonce}", "preview");
  const colors = theme === "light" ? light : theme === "contrast" ? contrast : dark;
  const variables = Object.entries(colors).map(([key, value]) => `--vscode-${key}:${value}`).join(";");
  html = html.replace("</head>", `<style>:root{${variables};--vscode-font-family:'Segoe UI',sans-serif;--vscode-font-size:13px;--vscode-editor-font-family:Consolas,monospace;--vscode-editor-font-size:13px}</style><script src="/fixture.js"></script></head>`)
    .replace("<body>", `<body class="vscode-${theme === "contrast" ? "high-contrast" : theme}">`);
  return html;
}
const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${port}`);
  if (url.pathname === "/") {
    res.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
    res.end(previewHtml(url.searchParams.get("theme") || "dark"));
    return;
  }
  const files = {
    "/media/startView.js": ["media/startView.js", "text/javascript"],
    "/media/startView.css": ["media/startView.css", "text/css"],
    "/resources/alysis-logo.png": ["resources/alysis-logo.png", "image/png"],
    "/fixture.js": ["scripts/qa/ui-fixture.js", "text/javascript"]
  };
  const file = files[url.pathname];
  if (!file) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "Content-Type": file[1], "Cache-Control": "no-store" });
  res.end(fs.readFileSync(path.join(root, file[0])));
});
server.listen(port, "127.0.0.1", () => process.stdout.write(`Alysis UI fixture preview: http://127.0.0.1:${port}\n`));
