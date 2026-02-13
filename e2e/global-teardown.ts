/**
 * Playwright global teardown — runs once after all tests.
 *
 * Removes the temporary Delegate home directory.
 */

import * as fs from "fs";

async function globalTeardown() {
  const tmpDir = process.env.DELEGATE_E2E_HOME;
  if (tmpDir && fs.existsSync(tmpDir) && tmpDir.includes("delegate-e2e-")) {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    console.log(`\n  E2E teardown: removed ${tmpDir}\n`);
  }
}

export default globalTeardown;
