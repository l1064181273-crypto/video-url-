import { execFile } from "node:child_process";
import { readdir, readFile, rm } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";
import { promisify } from "node:util";

import { expect, test } from "@playwright/test";

import { CONTROLLED_TRACE_TOKEN } from "../fixtures/token-fixture";

const execFileAsync = promisify(execFile);
const EXTENSION_ROOT = resolve(import.meta.dirname, "../..");
const DIST = resolve(EXTENSION_ROOT, "dist");
const CONTROLLED_OUTPUT = resolve(EXTENSION_ROOT, "test-results/controlled-token-failure");
const CONTROLLED_CONFIG = resolve(EXTENSION_ROOT, "tests/fixtures/playwright-token.config.ts");
const PLAYWRIGHT_CLI = resolve(EXTENSION_ROOT, "node_modules/@playwright/test/cli.js");

test("a failing trace containing a token remains outside dist", async () => {
  await rm(CONTROLLED_OUTPUT, { force: true, recursive: true });
  try {
    let exitCode: number | undefined;
    try {
      await execFileAsync(
        process.execPath,
        [PLAYWRIGHT_CLI, "test", "--config", CONTROLLED_CONFIG],
        {
          cwd: EXTENSION_ROOT,
        },
      );
    } catch (error) {
      exitCode = getExitCode(error);
    }
    expect(exitCode).toBe(1);

    const outputFiles = await listFiles(CONTROLLED_OUTPUT);
    const trace = outputFiles.find((file) => file.endsWith("trace.zip"));
    const errorContext = outputFiles.find((file) => file.endsWith("error-context.md"));
    expect(trace).toBeDefined();
    expect(errorContext).toBeDefined();
    if (trace === undefined || errorContext === undefined) {
      throw new Error("Controlled Playwright failure did not retain diagnostics");
    }
    expect(isInside(DIST, trace)).toBe(false);
    expect(isInside(DIST, errorContext)).toBe(false);

    const context = await readFile(errorContext, "utf8");
    const extractedTrace = await execFileAsync("unzip", ["-p", trace]);
    expect(context).toContain(CONTROLLED_TRACE_TOKEN);
    expect(extractedTrace.stdout).toContain(CONTROLLED_TRACE_TOKEN);

    const tokenBytes = Buffer.from(CONTROLLED_TRACE_TOKEN);
    for (const file of await listFiles(DIST)) {
      expect((await readFile(file)).includes(tokenBytes), file).toBe(false);
    }
  } finally {
    await rm(CONTROLLED_OUTPUT, { force: true, recursive: true });
  }
});

function getExitCode(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null || !("code" in error)) {
    return undefined;
  }
  return typeof error.code === "number" ? error.code : undefined;
}

function isInside(parent: string, child: string): boolean {
  const childFromParent = relative(parent, child);
  return (
    childFromParent !== "" && !childFromParent.startsWith("..") && !isAbsolute(childFromParent)
  );
}

async function listFiles(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const path = resolve(directory, entry.name);
      return entry.isDirectory() ? listFiles(path) : [path];
    }),
  );
  return nested.flat();
}
