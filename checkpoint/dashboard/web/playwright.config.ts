import { defineConfig, devices } from "@playwright/test";

// Headless smoke test for the *built* dashboard. The SPA can only be built on
// CI (Vite 8 / rolldown needs Node 22), so this is the automated stand-in for a
// human click-through: it serves the freshly built bundle with `vite preview`
// and asserts the app actually mounts, routes, and is styled by Tailwind —
// catching "compiles but renders blank/unstyled" regressions that a type-check
// and a green build can't. Runs against a static route that needs no backend.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // Bind explicitly to IPv4 so Playwright's 127.0.0.1 health check matches
    // (vite preview otherwise binds localhost, which can resolve to IPv6 ::1).
    command: "npm run preview -- --host 127.0.0.1 --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
