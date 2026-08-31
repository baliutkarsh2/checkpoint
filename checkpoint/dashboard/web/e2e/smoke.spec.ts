import { test, expect } from "@playwright/test";

// An unknown route renders the static Layout shell + the NotFound page, neither
// of which calls the backend — so these assertions are deterministic without a
// running API server.
const ROUTE = "/__smoke__/not-a-real-route";

test("dashboard mounts, routes, and is styled by Tailwind", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto(ROUTE);

  // 1. React actually mounted something.
  await expect(page.locator("#root")).not.toBeEmpty();

  // 2. The nav shell (Layout) rendered — brand + nav links prove routing and
  //    the component tree are alive.
  await expect(page.getByRole("link", { name: "checkpoint" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Runs", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Scenarios" })).toBeVisible();

  // 3. Tailwind v4 compiled our custom design tokens. The accent "blip" dot uses
  //    `bg-accent` (#2dff5c). If Tailwind failed to build or the theme tokens in
  //    tailwind.config.ts didn't carry through @config, this would not be green.
  const blip = page.locator(".animate-blip").first();
  await expect(blip).toBeVisible();
  const bg = await blip.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(bg).toBe("rgb(45, 255, 92)");

  // 4. No uncaught exceptions during mount of the static shell (would catch a
  //    React 19 / react-router 7 runtime break that still type-checks).
  expect(errors, `page errors:\n${errors.join("\n")}`).toHaveLength(0);
});

test("class-based dark mode variant compiles and toggles", async ({ page }) => {
  await page.goto(ROUTE);

  await page.getByRole("button", { name: "Toggle theme" }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);

  // `html.dark body` applies bg-ink-2 (#1f1d18 = rgb(31, 29, 24)); this only
  // holds if the Tailwind v4 `@custom-variant dark` + dark tokens compiled.
  const bodyBg = await page.evaluate(
    () => getComputedStyle(document.body).backgroundColor,
  );
  expect(bodyBg).toBe("rgb(31, 29, 24)");
});
