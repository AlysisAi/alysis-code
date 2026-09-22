import { readdirSync, statSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import Mocha from "mocha";

export function run(): Promise<void> {
  const mocha = new Mocha({
    color: true,
    timeout: 20000,
    ui: "tdd"
  });
  const grep = process.env.ALYSIS_EXTENSION_HOST_TEST_GREP?.trim();
  if (grep) {
    mocha.grep(new RegExp(grep, "i"));
  }
  const testsRoot = __dirname;
  for (const file of testFiles(testsRoot)) {
    mocha.addFile(file);
  }

  return new Promise((resolveRun, rejectRun) => {
    const runner = mocha.run((failures) => {
      const resultPath = process.env.ALYSIS_TEST_RESULT_PATH;
      if (resultPath) {
        try {
          writeFileSync(resultPath, JSON.stringify({
            tests: runner.stats?.tests ?? 0,
            passes: runner.stats?.passes ?? 0,
            pending: runner.stats?.pending ?? 0,
            failures
          }), { encoding: "utf8", mode: 0o600 });
        } catch (error) { rejectRun(error); return; }
      }
      if (failures > 0) {
        rejectRun(new Error(`${failures} extension host test(s) failed.`));
        return;
      }
      resolveRun();
    });
  });
}

function* testFiles(root: string): Generator<string> {
  for (const entry of readdirSync(root)) {
    const fullPath = resolve(root, entry);
    const stat = statSync(fullPath);
    if (stat.isDirectory()) {
      yield* testFiles(fullPath);
      continue;
    }
    if (entry.endsWith(".test.js")) {
      yield fullPath;
    }
  }
}
