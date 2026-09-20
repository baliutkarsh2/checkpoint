// A runnable example, shaped like a Vitest suite but executable with plain
// `node example.test.js` so it can be smoke-checked without a test runner.
// The Python suite only checks that this module's exports are what the type
// declarations promise; it does not run this.

const { withCheckpoint, resetCheckpointTwins } = require("./index.js");

async function main() {
  // Two twins in one call, each with its own URL and credential.
  const session = await withCheckpoint({
    services: {
      github: { seed: "small-project" },
      slack: { seed: "incident-active" },
    },
  });

  try {
    const github = session.services.github;
    const slack = session.services.slack;

    console.log("github:", github.url, "credential in", github.tokenEnv.join("/"));
    console.log("slack :", slack.url, "credential in", slack.tokenEnv.join("/"));

    // Point your client at these URLs the way it would point at production.
    // With Vitest:
    //
    //   test("the agent files an issue and tells the channel", async () => {
    //     await runAgent({ GITHUB_URL: github.url, SLACK_URL: slack.url });
    //     const issues = await fetch(`${github.url}/repos/acme/webapp/issues`)
    //       .then((r) => r.json());
    //     expect(issues.some((i) => i.title === "On-call alert")).toBe(true);
    //   });

    // Between tests: back to the seeded state, without restarting anything.
    await resetCheckpointTwins();
    console.log("twins reset");
  } finally {
    session.stop();
  }
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
