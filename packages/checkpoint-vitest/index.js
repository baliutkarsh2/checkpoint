// @checkpoint/vitest — start Checkpoint's service twins from a JavaScript test.
//
// `withCheckpoint({ services })` starts a long-lived twin per service and hands
// back its URL, MCP endpoint and credential. `resetCheckpointTwins()` wipes
// their state between tests without paying to restart the processes.
//
// It shells out to `checkpoint twins start --json`, which exists so this does
// not have to scrape a terminal panel with regexes — the previous version did,
// and a single relabelled line in the CLI silently broke every consumer.
//
// The Vitest integration is deliberately optional: these are plain async
// functions, callable from any framework's setup hook (Vitest `beforeAll`,
// Mocha `before`, Jest `beforeAll`).

const { spawnSync } = require("node:child_process");
const http = require("node:http");

const DEFAULT_CLI = process.env.CHECKPOINT_CLI || "checkpoint";

// Every twin started by this process, so reset and shutdown can reach them.
const _started = new Map();

function _runCli(args, { timeoutMs = 60_000 } = {}) {
  const out = spawnSync(DEFAULT_CLI, args, { encoding: "utf8", timeout: timeoutMs });
  if (out.error) {
    throw new Error(
      `the checkpoint CLI is not runnable (${DEFAULT_CLI}): ${out.error.message}. ` +
        "Install it with `pip install checkpoint-agents`, or set CHECKPOINT_CLI."
    );
  }
  if (out.status !== 0) {
    throw new Error(
      `checkpoint ${args.join(" ")} exited ${out.status}: ${out.stderr || out.stdout}`
    );
  }
  return out.stdout;
}

function _json(args) {
  const stdout = _runCli(args);
  try {
    return JSON.parse(stdout);
  } catch (cause) {
    throw new Error(
      `checkpoint ${args.join(" ")} did not return JSON:\n${stdout}`,
      { cause }
    );
  }
}

function _supportedServices() {
  // Asked of the CLI rather than hard-coded here, so a twin the installed
  // Checkpoint has — including one a project declares itself — is usable
  // without waiting for this package to be republished.
  return new Set(_json(["twins", "list", "--json"]).map((twin) => twin.name));
}

function _reset(service, baseUrl) {
  return new Promise((resolve, reject) => {
    const target = new URL("/_reset", baseUrl);
    const request = http.request(
      {
        hostname: target.hostname,
        port: target.port,
        path: target.pathname,
        method: "POST",
        headers: { "Content-Length": "0" },
      },
      (response) => {
        response.resume();
        response.on("end", () =>
          response.statusCode < 400
            ? resolve()
            : reject(new Error(`resetting ${service} returned ${response.statusCode}`))
        );
      }
    );
    request.on("error", reject);
    request.end();
  });
}

/**
 * Start the requested twins and return their URLs, MCP endpoints and credentials.
 *
 * @param {{ services: Record<string, { seed?: string } | null> }} config
 * @returns {Promise<{ services: Record<string, object>, stop: () => void }>}
 */
async function withCheckpoint(config = {}) {
  const services = config.services || {};
  const supported = _supportedServices();
  const handles = {};

  for (const [name, options] of Object.entries(services)) {
    if (!supported.has(name)) {
      throw new Error(
        `unknown Checkpoint twin: ${name}. Available: ${[...supported].sort().join(", ")}`
      );
    }
    const args = ["twins", "start", name, "--json"];
    const seed = (options && options.seed) || null;
    // Seeding is part of starting, so a failed seed fails the start rather
    // than leaving a test running against an empty twin it thinks is seeded.
    if (seed) args.push("--seed", seed);

    const started = _json(args);
    const handle = {
      url: started.url,
      mcpUrl: started.mcp_url,
      token: started.token,
      tokenEnv: started.token_env,
      urlEnv: started.url_env,
      seed: started.seed,
    };
    _started.set(name, handle);
    handles[name] = handle;
  }

  return {
    services: handles,
    stop() {
      for (const name of Object.keys(handles)) {
        try {
          _runCli(["twins", "stop", name]);
        } catch {
          // A twin that is already gone is the outcome we wanted.
        }
        _started.delete(name);
      }
    },
  };
}

/**
 * Reset every twin this process started, wiping their state without restarting
 * them. Resolves when they have all answered, rejects on the first failure.
 *
 * @returns {Promise<void>}
 */
async function resetCheckpointTwins() {
  for (const [name, handle] of _started.entries()) {
    await _reset(name, handle.url);
  }
}

module.exports = { withCheckpoint, resetCheckpointTwins };
