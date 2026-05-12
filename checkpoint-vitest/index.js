// @checkpoint/vitest — thin wrapper around the `checkpoint` CLI.
//
// `withCheckpoint({ services })` spins up long-lived twin sessions via
// `checkpoint clone start <id>` and exposes their bootstrap URLs + tokens
// to the test suite. `resetCheckpointTwins()` calls `/_reset` on each
// running twin to wipe state without restarting the processes.
//
// The Vitest integration is deliberately optional: this module exports
// plain async functions you can call from any test framework's setup hook
// (Vitest beforeAll, Mocha before, Jest beforeAll, etc.).

const { execFileSync, spawnSync } = require("node:child_process");
const http = require("node:http");

const SUPPORTED_SERVICES = new Set(["github", "slack", "stripe"]);
const DEFAULT_CLI = process.env.CHECKPOINT_CLI || "checkpoint";

// Tracks every clone this process started so `resetCheckpointTwins()` and
// shutdown hooks can act on them. Keyed by service id.
const _started = new Map();

function _runCli(args, { timeoutMs = 30_000 } = {}) {
  const out = spawnSync(DEFAULT_CLI, args, {
    encoding: "utf8",
    timeout: timeoutMs,
  });
  if (out.error) {
    throw new Error(
      `checkpoint CLI not runnable (${DEFAULT_CLI}): ${out.error.message}`
    );
  }
  if (out.status !== 0) {
    throw new Error(
      `checkpoint ${args.join(" ")} exited ${out.status}: ${out.stderr || out.stdout}`
    );
  }
  return out.stdout;
}

function _parseCloneStart(stdout) {
  // The CLI prints a rich-panel block; extract URL + Token via regex.
  const url = stdout.match(/URL:\s*(\S+)/)?.[1];
  const mcpUrl = stdout.match(/MCP URL:\s*(\S+)/)?.[1];
  const token = stdout.match(/Token:\s*(\S+)/)?.[1];
  if (!url || !token) {
    throw new Error(
      `unable to parse \`checkpoint clone start\` output:\n${stdout}`
    );
  }
  return { url, mcpUrl, token };
}

function _resetClone(serviceId, baseUrl) {
  return new Promise((resolve, reject) => {
    const u = new URL("/_reset", baseUrl);
    const req = http.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname,
        method: "POST",
        headers: { "Content-Length": "0" },
      },
      (res) => {
        res.resume();
        res.on("end", () =>
          res.statusCode < 400
            ? resolve()
            : reject(
                new Error(`reset ${serviceId} returned ${res.statusCode}`)
              )
        );
      }
    );
    req.on("error", reject);
    req.end();
  });
}

/**
 * Spin up the requested services and return a handle with URLs + tokens.
 *
 * @param {{ services: Record<string, { mode?: string, seed?: string }> }} config
 * @returns {Promise<{ services: Record<string, { url: string, mcpUrl: string, token: string, mode: string, seed: string | null }>, stop: () => void }>}
 */
async function withCheckpoint(config = {}) {
  const services = config.services || {};
  const handles = {};

  for (const [id, opts] of Object.entries(services)) {
    if (!SUPPORTED_SERVICES.has(id)) {
      throw new Error(
        `Unsupported Checkpoint service: ${id}. Supported: ${[...SUPPORTED_SERVICES].join(", ")}`
      );
    }
    const mode = (opts && opts.mode) || "route";
    const seed = (opts && opts.seed) || null;

    const stdout = _runCli(["clone", "start", id]);
    const parsed = _parseCloneStart(stdout);
    _started.set(id, parsed);

    // Seed if requested — same /_seed-file dispatch the runner uses.
    if (seed) {
      try {
        execFileSync(
          "curl",
          [
            "-fsS",
            "-X",
            "POST",
            `${parsed.url}/_seed-named`,
            "-H",
            "Content-Type: application/json",
            "-d",
            JSON.stringify({ name: seed }),
          ],
          { stdio: "ignore" }
        );
      } catch {
        // Seeding failures are non-fatal; the URL is still usable empty.
      }
    }

    handles[id] = { ...parsed, mode, seed };
  }

  return {
    services: handles,
    stop() {
      for (const id of Object.keys(handles)) {
        try {
          _runCli(["clone", "stop", id]);
        } catch {
          // best-effort
        }
        _started.delete(id);
      }
    },
  };
}

/**
 * Reset every clone started in this process. Returns a Promise that resolves
 * once all `/_reset` calls have completed (or rejects on the first failure).
 *
 * @returns {Promise<void>}
 */
async function resetCheckpointTwins() {
  for (const [id, info] of _started.entries()) {
    await _resetClone(id, info.url);
  }
}

module.exports = { withCheckpoint, resetCheckpointTwins };
