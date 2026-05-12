// Type definitions for @checkpoint/vitest

export type CheckpointServiceId = "github" | "slack" | "stripe";

export interface CheckpointServiceConfig {
  /** How the twin should be reached. Currently only `route` is supported. */
  mode?: "route";
  /** Named seed to load into the twin after start (e.g. `small-project`). */
  seed?: string;
}

export interface CheckpointServiceHandle {
  /** Base URL of the running twin, e.g. `http://127.0.0.1:53115`. */
  url: string;
  /** MCP transport URL for the same twin. */
  mcpUrl: string;
  /** Bootstrap token to send as `Authorization`. */
  token: string;
  /** Resolved mode (defaults to `route`). */
  mode: string;
  /** Seed that was loaded, if any. */
  seed: string | null;
}

export interface WithCheckpointConfig {
  services: Partial<Record<CheckpointServiceId, CheckpointServiceConfig>>;
}

export interface CheckpointSession {
  /** Per-service handles keyed by service id. */
  services: Partial<Record<CheckpointServiceId, CheckpointServiceHandle>>;
  /** Stop every twin this call started. */
  stop(): void;
}

/**
 * Spin up the requested Checkpoint twins and return per-service URLs + tokens.
 *
 * Shells out to `checkpoint clone start <id>` under the hood, so the
 * Checkpoint CLI must be on PATH (set `CHECKPOINT_CLI=/path/to/checkpoint`
 * to override).
 */
export function withCheckpoint(
  config: WithCheckpointConfig
): Promise<CheckpointSession>;

/**
 * Call `/_reset` on every twin started in this process to wipe state without
 * restarting the processes.
 */
export function resetCheckpointTwins(): Promise<void>;
