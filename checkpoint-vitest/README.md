# @checkpoint/vitest

Spin up stateful GitHub / Slack / Stripe / Linear / Supabase / Discord / Google Workspace twins for your JS test suite.

```js
// example.test.js
import { beforeAll, afterAll, beforeEach, test, expect } from "vitest";
import { withCheckpoint, resetCheckpointTwins } from "@checkpoint/vitest";
import { Octokit } from "octokit";

let session;

beforeAll(async () => {
  session = await withCheckpoint({
    services: {
      github: { mode: "route", seed: "small-project" },
    },
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

## Install

```bash
npm install --save-dev @checkpoint/vitest
```

The Python `checkpoint` CLI must also be installed and on `PATH` — this
package shells out to `checkpoint clone start` / `checkpoint clone stop`.
Set `CHECKPOINT_CLI=/path/to/checkpoint` to override.

## API

- `withCheckpoint({ services })` — start one twin per entry in `services` and
  return `{ services: { <id>: { url, mcpUrl, token, mode, seed } }, stop() }`.
- `resetCheckpointTwins()` — wipe state on every twin started in this process.

## Supported services

`github`, `slack`, `stripe`, `linear`, `supabase`, `discord`, `google-workspace`.
See the parent [Checkpoint README](../README.md) for the underlying twin surface.

## License

MIT
