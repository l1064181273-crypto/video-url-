import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

export default defineConfig({
  testDir: import.meta.dirname,
  testMatch: "token-trace-failure.spec.ts",
  outputDir: resolve(import.meta.dirname, "../../test-results/controlled-token-failure"),
  reporter: [["line"]],
  retries: 0,
  use: {
    trace: "off",
  },
  workers: 1,
});
