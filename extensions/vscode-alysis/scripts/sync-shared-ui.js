"use strict";

const fs = require("node:fs");
const path = require("node:path");
const source = path.resolve(__dirname, "../../shared-ui");
const destination = path.resolve(__dirname, "../media");
for (const name of ["startView.html", "startView.js", "startView.css", "THIRD_PARTY_NOTICES.txt"]) {
  const content = fs.readFileSync(path.join(source, name));
  const target = path.join(destination, name);
  if (process.argv.includes("--check")) {
    if (!fs.existsSync(target) || !content.equals(fs.readFileSync(target))) {
      throw new Error(`Shared sidebar asset ${name} is stale. Run npm run sync:ui.`);
    }
  } else {
    fs.writeFileSync(target, content);
  }
}
