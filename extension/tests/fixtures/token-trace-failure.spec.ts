import { expect, test } from "@playwright/test";

import { CONTROLLED_TRACE_TOKEN } from "./token-fixture";

test("records a controlled token outside dist", async ({ page }) => {
  await page.setContent(
    `<main data-token="${CONTROLLED_TRACE_TOKEN}">${CONTROLLED_TRACE_TOKEN}</main>`,
  );
  await expect(page.locator("main")).toHaveText("intentional-failure");
});
