import { readdir, readFile } from "node:fs/promises";
import { dirname, extname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_ROOT = dirname(fileURLToPath(import.meta.url));
const DEFAULT_DIST = resolve(SCRIPT_ROOT, "../dist");
const ROOT_FILES = new Set(["background.js", "manifest.json", "sidepanel.html", "sidepanel.js"]);
const FORBIDDEN_NAMES = new Set(["error-context.md", "trace.zip"]);
const TEST_TOKENS = [
  "LVTSecretToken123",
  "CheckpointOneRuntimeToken",
  "ControlledTraceTokenABC123",
];

export async function verifyDist(distPath = DEFAULT_DIST) {
  const files = await listFiles(distPath);
  for (const file of files) {
    const relativePath = relative(distPath, file);
    if (!isAllowedBuildFile(relativePath)) {
      throw new Error(`Unexpected file in extension dist: ${relativePath}`);
    }
    if (
      FORBIDDEN_NAMES.has(relativePath) ||
      relativePath.includes("test-results") ||
      [".map", ".md", ".zip"].includes(extname(relativePath))
    ) {
      throw new Error(`Forbidden test or diagnostic artifact in extension dist: ${relativePath}`);
    }
    const content = await readFile(file, "utf8");
    for (const token of TEST_TOKENS) {
      if (content.includes(token)) {
        throw new Error(`Test token found in extension dist: ${relativePath}`);
      }
    }
  }
  return files.map((file) => relative(distPath, file)).sort();
}

function isAllowedBuildFile(relativePath) {
  if (ROOT_FILES.has(relativePath)) {
    return true;
  }
  return relativePath.startsWith("assets/") && [".css", ".js"].includes(extname(relativePath));
}

async function listFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const path = resolve(directory, entry.name);
      return entry.isDirectory() ? listFiles(path) : [path];
    }),
  );
  return nested.flat();
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const files = await verifyDist();
  process.stdout.write(`Verified extension dist files:\n${files.join("\n")}\n`);
}
