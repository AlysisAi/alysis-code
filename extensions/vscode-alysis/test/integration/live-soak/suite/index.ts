import { resolve } from "node:path";

import Mocha from "mocha";

export function run(): Promise<void> {
  const mocha = new Mocha({ color: true, timeout: 1_200_000, ui: "tdd" });
  mocha.addFile(resolve(__dirname, "liveSoak.test.js"));
  return new Promise((resolveRun, rejectRun) => {
    mocha.run((failures) => {
      if (failures > 0) {
        rejectRun(new Error(`${failures} live-provider soak test(s) failed.`));
      } else {
        resolveRun();
      }
    });
  });
}
