import { expect, test } from "@playwright/test";

import { CONTROLLED_TRACE_TOKEN } from "./token-fixture";

test("does not persist a controlled token in failure diagnostics", async ({ page }) => {
  await page.goto("data:text/html,<main>controlled failure</main>");
  await page.evaluate((token) => localStorage.setItem("test-token", token), CONTROLLED_TRACE_TOKEN);
  await expect(page.locator("main")).toHaveText("intentional-failure");
});
