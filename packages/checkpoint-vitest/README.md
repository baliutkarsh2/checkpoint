# @checkpoint/vitest

Start Checkpoint's stateful service twins from a JavaScript test suite. Your
code calls what looks like GitHub, Slack, Stripe, Linear, Supabase, Discord or
Google Workspace, and the twin remembers what it did — so a test can assert on
the state that was left, not on a mock's recorded reply.

```js
// example.test.js
import { beforeAll, afterAll, beforeEach, test, expect } from "vitest";
import { withCheckpoint, resetCheckpointTwins } from "@checkpoint/vitest";
import { Octokit } from "octokit";

let session;

beforeAll(async () => {
  session = await withCheckpoint({
    services: { github: { seed: "small-project" } },
  });
});

afterAll(() => session.stop());
beforeEach(() => resetCheckpointTwins());

test("creates an issue", async () => {
  const { url, token } = session.services.github;
  const octokit = new Octokit({ auth: token, baseUrl: url });
  const { data } = await octokit.rest.issues.create({
    owner: "acme",
    repo: "webapp",
    title: "Add login button",
  });
  expect(data.number).toBeGreaterThan(0);
});
```

Resetting between tests costs a request rather than a process start, so a suite
of a hundred tests pays for one twin, not a hundred.

## Install

```bash
npm install --save-dev @checkpoint/vitest
pip install checkpoint-agents
```

The Checkpoint CLI must be on `PATH` — this package runs
`checkpoint twins start --json` and `checkpoint twins stop`. Set
`CHECKPOINT_CLI=/path/to/checkpoint` to point at a specific one.

## API

- **`withCheckpoint({ services })`** — start one twin per entry and return
  `{ services, stop() }`. Each handle carries `url`, `mcpUrl`, `token`,
  `tokenEnv`, `urlEnv` and `seed`. A `seed` that fails to load fails the start,
  rather than leaving the test running against an empty twin it believes is
  seeded.
- **`resetCheckpointTwins()`** — wipe the state of every twin this process
  started, without restarting them.

## Which twins

Whatever the installed Checkpoint has: `checkpoint twins list`. That is read
from the CLI at run time rather than fixed in this package, so a twin your own
project declares in its `checkpoint.toml` works here too.

For testing a whole agent rather than a client library, use the CLI directly —
`checkpoint run` intercepts calls to the real hostnames, so the agent needs no
knowledge of any of this. See the [Checkpoint README](../../README.md).

## License

Apache-2.0
