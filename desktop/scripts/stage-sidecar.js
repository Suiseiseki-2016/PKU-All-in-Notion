#!/usr/bin/env node
/**
 * Cross-platform entry for `npm run stage-sidecar`.
 * Windows production: PowerShell stages relocatable runtime + externalBin.
 * Elsewhere: bash stages the Rust sidecar (and optional Linux runtime).
 *
 * Env:
 *   PKU_SIDECAR_DEV_STUB=1  — sidecar only (PATH fallback); used by tauri dev helpers
 *   PKU_SIDECAR_SKIP_RUNTIME=1 — build sidecar, skip Python tree
 */
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const scriptsDir = __dirname;
const isWin = process.platform === "win32";
// Windows Node invoked from WSL often lacks cargo on PATH; prefer the bash stager.
const inWsl = Boolean(process.env.WSL_DISTRO_NAME || process.env.WSLENV);
const argv = new Set(process.argv.slice(2));
const devStub =
  process.env.PKU_SIDECAR_DEV_STUB === "1" || argv.has("--dev-stub-only");
const skipRuntime =
  process.env.PKU_SIDECAR_SKIP_RUNTIME === "1" || argv.has("--skip-runtime");

let cmd;
let args;
if (isWin && !inWsl) {
  cmd = "powershell";
  args = [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    path.join(scriptsDir, "stage-sidecar.ps1"),
  ];
  if (devStub) args.push("-DevStubOnly");
  if (skipRuntime) args.push("-SkipRuntime");
} else {
  cmd = "bash";
  args = [path.join(scriptsDir, "stage-sidecar.sh")];
  if (devStub) args.push("--dev-stub-only");
  if (skipRuntime) args.push("--skip-runtime");
}

const result = spawnSync(cmd, args, { stdio: "inherit", shell: false });
if (result.error) {
  console.error(result.error);
  process.exit(1);
}
process.exit(result.status ?? 1);
