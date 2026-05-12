// Example test (Vitest-shaped, but runs with plain `node` for smoke-checks).
// Intentionally NOT executed by Python regression tests — the Python test
// only verifies the module exports are correct.

const { withCheckpoint, resetCheckpointTwins } = require("./index.js");

async function main() {
  const session = await withCheckpoint({
    services: {
      github: { mode: "route", seed: "small-project" },
    },
  });
  try {
    const gh = session.services.github;
    console.log("github twin URL:", gh.url);
    console.log("github twin token:", gh.token.slice(0, 10) + "...");
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
