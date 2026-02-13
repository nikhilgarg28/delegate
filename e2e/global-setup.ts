/**
 * Playwright global setup — runs once before all tests.
 *
 * The temp dir and port are already created/set by playwright.config.ts.
 * This script only seeds test data into that directory.
 */

import { execSync } from "child_process";
import * as path from "path";
import * as fs from "fs";

async function globalSetup() {
  const tmpDir = process.env.DELEGATE_E2E_HOME;
  if (!tmpDir) {
    throw new Error("DELEGATE_E2E_HOME not set — config should have set it");
  }

  // Find the git common directory (main repo) to access .venv
  // This handles both regular repos and git worktrees
  let projectRoot = path.resolve(__dirname, "..");
  const gitFile = path.join(projectRoot, ".git");
  if (fs.existsSync(gitFile) && fs.statSync(gitFile).isFile()) {
    const gitContent = fs.readFileSync(gitFile, "utf-8").trim();
    if (gitContent.startsWith("gitdir: ")) {
      // This is a worktree — extract main repo path
      const gitDir = gitContent.replace("gitdir: ", "");
      // gitDir is something like /path/to/repo/.git/worktrees/T0047
      // We want /path/to/repo
      projectRoot = path.resolve(gitDir, "../../..");
    }
  }

  const seedScript = path.resolve(__dirname, "seed.py");
  const venvPython = path.join(projectRoot, ".venv", "bin", "python");

  try {
    execSync(`${venvPython} ${seedScript} ${tmpDir}`, {
      cwd: projectRoot,
      stdio: "pipe",
      env: { ...process.env, PYTHONPATH: projectRoot },
    });
  } catch (err: any) {
    console.error("Seed script failed:");
    console.error(err.stderr?.toString() || err.message);
    throw err;
  }

  console.log(
    `\n  E2E setup: home=${tmpDir}, port=${process.env.DELEGATE_E2E_PORT}\n`
  );
}

export default globalSetup;
