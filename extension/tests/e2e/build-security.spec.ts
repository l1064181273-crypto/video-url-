import { readdir, readFile } from "node:fs/promises";
import { extname, relative, resolve } from "node:path";

import { expect, test } from "@playwright/test";

const DIST = resolve(import.meta.dirname, "../../dist");

test("built extension contains no remote or executable inline code", async () => {
  const files = await listFiles(DIST);
  const relativeFiles = files.map((file) => relative(DIST, file)).sort();
  expect(files).toContain(resolve(DIST, "manifest.json"));
  expect(files).toContain(resolve(DIST, "background.js"));
  expect(files).toContain(resolve(DIST, "sidepanel.html"));
  expect(
    relativeFiles.every(
      (file) =>
        ["background.js", "manifest.json", "sidepanel.html", "sidepanel.js"].includes(file) ||
        (file.startsWith("assets/") && [".css", ".js"].includes(extname(file))),
    ),
  ).toBe(true);
  expect(
    relativeFiles.some(
      (file) =>
        file.includes("test-results") ||
        ["error-context.md", "trace.zip"].includes(file) ||
        [".map", ".md", ".zip"].includes(extname(file)),
    ),
  ).toBe(false);

  for (const file of files.filter((path) => /\.(?:css|html|js|json)$/u.test(path))) {
    const content = await readFile(file, "utf8");
    const remoteUrls = [...content.matchAll(/https?:\/\/[^\s"'`<>)]+/gu)]
      .map((match) => match[0])
      .filter((url) => !url.startsWith("http://127.0.0.1/"));
    expect(remoteUrls, file).toEqual([]);
    expect(content, file).not.toMatch(/\beval\s*\(/u);
    expect(content, file).not.toMatch(/\bnew\s+Function\s*\(/u);
    for (const forbiddenToken of ["LVTSecretToken123", "CheckpointOneRuntimeToken"]) {
      expect(content, file).not.toContain(forbiddenToken);
    }
  }

  const html = await readFile(resolve(DIST, "sidepanel.html"), "utf8");
  for (const script of html.matchAll(/<script\b([^>]*)>/giu)) {
    expect(script[1], "Every built script must use src").toMatch(/\bsrc=/u);
  }
});

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
