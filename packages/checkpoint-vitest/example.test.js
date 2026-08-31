// Example test (Vitest-shaped, but runs with plain `node` for smoke-checks).
// Intentionally NOT executed by Python regression tests — the Python test
// only verifies the module exports are correct.

const { withCheckpoint, resetCheckpointTwins } = require("./index.js");

async function main() {
  // Spin up two twins in one call. Each gets its own URL + bootstrap token.
  const session = await withCheckpoint({
    services: {
      github: { mode: "route", seed: "small-project" },
      slack: { mode: "route", seed: "incident-active" },
    },
  });

  try {
    const gh = session.services.github;
    const sl = session.services.slack;

    console.log("github twin URL:", gh.url);
    console.log("github token  :", gh.token.slice(0, 10) + "...");
    console.log("slack twin URL:", sl.url);
    console.log("slack token   :", sl.token.slice(0, 10) + "...");

    // Your agent test logic here — call gh.url / sl.url like real APIs.
    // e.g. with vitest:
    //
    //   it("agent creates an issue and posts to Slack", async () => {
    //     await runAgent({ GITHUB_URL: gh.url, SLACK_URL: sl.url });
    //     const issues = await fetch(`${gh.url}/repos/acme/webapp/issues`).then(r => r.json());
    //     expect(issues.some(i => i.title === "On-call alert")).toBe(true);
    //   });

    // Between tests: reset state without restarting processes.
    await resetCheckpointTwins();
    console.log("twins reset OK");
  } finally {
    session.stop();
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
