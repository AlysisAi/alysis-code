import { resolve } from "node:path";

import Mocha from "mocha";

export function run(): Promise<void> {
  const mocha = new Mocha({ color: true, timeout: 3_900_000, ui: "tdd" });
  mocha.addFile(resolve(__dirname, "soak.test.js"));
  return new Promise((resolveRun, rejectRun) => {
    mocha.run((failures) => {
      if (failures > 0) {
        rejectRun(new Error(`${failures} Extension Host soak test(s) failed.`));
      } else {
        resolveRun();
      }
    });
  });
}
