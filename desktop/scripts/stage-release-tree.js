#!/usr/bin/env node
// Make the unbundled release directory runnable with the same layout as NSIS.
const fs = require("node:fs");
const path = require("node:path");

const src = path.resolve(__dirname, "../src-tauri/resources/runtime");
const dest = path.resolve(__dirname, "../src-tauri/target/release/resources/runtime");
if (!fs.existsSync(path.join(src, "python.exe"))) {
  throw new Error("Missing staged Python runtime; run the release build first.");
}
fs.rmSync(dest, { recursive: true, force: true });
fs.mkdirSync(path.dirname(dest), { recursive: true });
fs.cpSync(src, dest, { recursive: true });
console.log("Unbundled runtime ready:", dest);
