// Type definitions for @checkpoint/vitest

export interface CheckpointServiceConfig {
  /** Named dataset to load into the twin as it starts, e.g. `small-project`. */
  seed?: string;
}

export interface CheckpointServiceHandle {
  /** Base URL of the running twin, e.g. `http://127.0.0.1:53115`. */
  url: string;
  /** MCP endpoint for the same twin. */
  mcpUrl: string;
  /** Credential the twin accepts, to send as `Authorization`. */
  token: string;
  /** Variables this service's SDKs read the credential from. */
  tokenEnv: string[];
  /** Variable holding the twin's base URL, e.g. `CHECKPOINT_GITHUB_URL`. */
  urlEnv: string;
  /** Seed that was loaded, if any. */
  seed: string | null;
}

export interface WithCheckpointConfig {
  /**
   * Twins to start, keyed by name. Any twin the installed Checkpoint knows
   * about works, including one a project declares in its own `checkpoint.toml`
   * — the list is read from the CLI rather than fixed here.
   */
  services: Record<string, CheckpointServiceConfig | null>;
}

export interface CheckpointSession {
  /** Per-twin handles, keyed by name. */
  services: Record<string, CheckpointServiceHandle>;
  /** Stop every twin this call started. */
  stop(): void;
}

/**
 * Start the requested Checkpoint twins and return their URLs and credentials.
 *
 * Runs `checkpoint twins start --json`, so the Checkpoint CLI must be on PATH
 * (`pip install checkpoint-agents`); set `CHECKPOINT_CLI` to point at another
 * one.
 */
export function withCheckpoint(
  config: WithCheckpointConfig
): Promise<CheckpointSession>;

/**
 * Reset every twin this process started, wiping their state without paying to
 * restart them.
 */
export function resetCheckpointTwins(): Promise<void>;
