import * as path from "node:path";

import Mocha from "mocha";
import { glob } from "glob";

export async function run(): Promise<void> {
  const mocha = new Mocha({ ui: "tdd", color: true, timeout: 180_000 });
  const testsRoot = path.resolve(__dirname);
  const files = await glob("**/*.test.js", { cwd: testsRoot });
  for (const file of files) {
    mocha.addFile(path.resolve(testsRoot, file));
  }
  await new Promise<void>((resolvePromise, reject) => {
    mocha.run((failures) => {
      if (failures > 0) {
        reject(new Error(`${failures} live preflight test(s) failed.`));
      } else {
        resolvePromise();
      }
    });
  });
}
