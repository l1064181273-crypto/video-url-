import { readFile } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import playwrightConfig from "../../playwright.config";

const EXTENSION_ROOT = resolve(import.meta.dirname, "../..");

describe("Playwright output isolation", () => {
  it("resolves outputDir outside the extension build directory", () => {
    expect(playwrightConfig.outputDir).toBe("./test-results");
    if (playwrightConfig.outputDir === undefined) {
      throw new Error("Playwright outputDir is required");
    }
    const outputDir = resolve(EXTENSION_ROOT, playwrightConfig.outputDir);
    const dist = resolve(EXTENSION_ROOT, "dist");
    const outputFromDist = relative(dist, outputDir);

    expect(outputFromDist).not.toBe("");
    expect(outputFromDist.startsWith("..") || isAbsolute(outputFromDist)).toBe(true);
  });

  it("ignores Playwright results and reports without ignoring dist safety checks", async () => {
    const gitignore = await readFile(resolve(EXTENSION_ROOT, ".gitignore"), "utf8");

    expect(gitignore.split(/\r?\n/u)).toEqual(
      expect.arrayContaining(["test-results/", "playwright-report/"]),
    );
  });
});
