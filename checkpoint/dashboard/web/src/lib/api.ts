// Types and fetch wrapper for the checkpoint dashboard API.
// The backend lives in checkpoint/dashboard/app.py — see /api/docs for the full
// OpenAPI schema. Types here mirror the JSON shapes returned by those routes.

export interface Criterion {
  text: string;
  kind: "D" | "P";
  passed: boolean;
  reasoning: string | null;
  evaluator: string | null;
}

export interface RunRecord {
  run_id: string;
  scenario: string | null;
  scenario_path: string | null;
  satisfaction: number;
  criteria: Criterion[];
  evaluator_model: string | null;
  evaluator_model_source?: string | null;
  env: Record<string, unknown> | null;
  trace: TraceEvent[];
  state: Record<string, unknown>;
  exit_code: number | null;
  final_answer: string | null;
  stdout?: string | null;
  error?: string | null;
  failure_analysis?: Record<string, string> | null;
  stderr?: string | null;
  metrics?: Record<string, unknown> | null;
  agent_trace?: unknown;
  harness?: { name?: string; dir?: string; mode?: string; cmd?: string } | null;
  duration_ms?: number | null;
}

export interface TraceEvent {
  method: string;
  path: string;
  status: number;
  _clone?: string;
  request_body?: unknown;
  response_body?: unknown;
  timestamp?: string;
  duration_ms?: number;
}

export interface TelemetryReport {
  run_id: string;
  summary: {
    scenario: string | null;
    scenario_path: string | null;
    satisfaction: number;
    criteria_passed: number;
    criteria_total: number;
    api_call_count: number;
    agent_message_count: number;
    agent_step_count: number;
    tool_call_count: number;
    duration_ms: number | null;
    timestamp: string | null;
    exit_code: number | null;
    harness: Record<string, unknown>;
  };
  cli: Record<string, string>;
  chat: {
    messages: {
      index: number;
      role: string;
      content: string;
      timestamp?: string | null;
      name?: string | null;
      raw?: unknown;
    }[];
    raw: unknown;
    capture_note: string;
  };
  transcript: {
    stdout: string;
    stderr: string;
    final_answer: string;
    error: string | null;
    exit_code: number | null;
  };
  steps: {
    index: number;
    type?: string | null;
    name?: string | null;
    status?: string | null;
    timestamp?: string | null;
    summary?: string | null;
    raw?: unknown;
  }[];
  tool_calls: {
    index: number;
    name: string;
    type?: string | null;
    status?: string | null;
    timestamp?: string | null;
    input?: unknown;
    output?: unknown;
    summary?: string | null;
    raw?: unknown;
  }[];
  api_calls: (TraceEvent & { index: number; clone?: string | null; raw?: unknown })[];
  judge: {
    model: string | null;
    model_source: string | null;
    criteria: {
      index: number;
      kind: "D" | "P";
      text: string;
      passed: boolean;
      evaluator: string | null;
      reasoning: string | null;
      raw?: unknown;
    }[];
    failure_analysis: Record<string, string>;
  };
  metrics: Record<string, unknown>;
  timeline: {
    kind: "run" | "chat" | "agent" | "tool" | "api" | "judge";
    label: string;
    timestamp?: string | null;
    status: string;
    detail?: string | null;
    ref?: { section: string; index?: number };
  }[];
  state: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export interface RunSummary {
  run_id: string;
  scenario: string | null;
  scenario_path: string | null;
  satisfaction: number;
  criteria_pass: number;
  criteria_total: number;
  evaluator_model: string | null;
  timestamp: string | null;
  exit_code: number | null;
  // Agent + mode + duration. Older records may have these as null.
  harness_name: string | null;
  harness_dir: string | null;
  mode: "docker" | "subprocess" | null;
  duration_ms: number | null;
}

export interface ScenarioDetail {
  path: string;
  abs_path: string;
  title: string;
  prompt: string;
  setup: string;
  expected: string;
  criteria: { text: string; kind: "D" | "P" }[];
  config: Record<string, unknown>;
  clones: string[];
  raw: string;
  runs: RunSummary[];
  stats: { total_runs: number; avg_score: number; pass_rate: number; last_at: string | null };
}

export interface AgentDetail {
  agent: AgentInfo;
  readme: string;
  runs: RunSummary[];
  by_scenario: Record<
    string,
    { runs: number; avg_score: number; last_score: number | null; last_at: string | null }
  >;
  stats: { total_runs: number; avg_score: number; pass_rate: number; last_at: string | null };
}

export interface SupportedClone {
  id: string;
  module: string;
}

export interface RunsPage {
  rows: RunSummary[];
  total: number;
  page: number;
  per_page: number;
}

export interface DashboardSummary {
  total_runs: number;
  avg_score_30d: number;
  pass_rate_30d: number;
  recent_fail_count: number;
}

export interface CloneInfo {
  id: string;
  url: string;
  mcp_url: string;
  started_at: string | null;
  pid: number;
}

export interface ScenarioSummary {
  title: string;
  path: string;
  clones: string;
  tags: string;
  d_count: number;
  p_count: number;
  coverage_pct: number;
}

export interface CoverageSummary {
  total_d: number;
  stage1_hits: number;
  stage1_pct: number;
  total_p: number;
}

export interface ReportTrend {
  run_count: number;
  avg_score: number;
  min_score: number;
  max_score: number;
  history: { run_id: string; score: number; timestamp: string | null }[];
  criteria: Record<
    string,
    { kind: "D" | "P"; total: number; passed: number; pass_rate: number }
  >;
  flaky_criteria: string[];
}

export interface CompareDiff {
  baseline_score: number;
  candidate_score: number;
  delta: number;
  regressions: { text: string; change: string }[];
  fixes: { text: string; change: string }[];
  added: { text: string; change: string }[];
  removed: { text: string; change: string }[];
  same: { text: string; change: string }[];
  criteria: { text: string; change: string }[];
}

export interface RunJob {
  job_id: string;
  scenario: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  started_at: string;
  ended_at: string | null;
  exit_code: number | null;
  run_id: string | null;
  cmd: string[];
}

export interface AppMeta {
  version: string;
  host: string;
  runs_dir: string;
  scenarios_dir: string;
  judge_model_default: string;
}

export interface AgentInfo {
  id: string;
  name: string;
  path: string;
  abs_path: string;
  description: string;
  source: "bundled" | "init" | "local";
}

class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(path, {
    headers: { Accept: "application/json", ...(init.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      body = await res.text().catch(() => null);
    }
    throw new ApiError(
      `${init.method || "GET"} ${path} -> ${res.status}`,
      res.status,
      body,
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  meta: () => request<AppMeta>("/api/meta"),
  summary: () => request<DashboardSummary>("/api/summary"),
  runs: (params: { scenario?: string; agent?: string; mode?: string; page?: number; per_page?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.scenario) q.set("scenario", params.scenario);
    if (params.agent) q.set("agent", params.agent);
    if (params.mode) q.set("mode", params.mode);
    if (params.page) q.set("page", String(params.page));
    if (params.per_page) q.set("per_page", String(params.per_page));
    const qs = q.toString();
    return request<RunsPage>(`/api/runs${qs ? `?${qs}` : ""}`);
  },
  run: (runId: string) => request<RunRecord>(`/api/runs/${runId}`),
  clones: () => request<CloneInfo[]>("/api/clones"),
  scenarios: (path?: string) => {
    const q = path ? `?path=${encodeURIComponent(path)}` : "";
    return request<{ scenarios: ScenarioSummary[]; coverage: CoverageSummary }>(
      `/api/scenarios${q}`,
    );
  },
  agents: () => request<AgentInfo[]>("/api/agents"),
  agent: (id: string) => request<AgentDetail>(`/api/agents/${encodeURIComponent(id)}`),
  scenarioFile: (path: string) =>
    request<ScenarioDetail>(`/api/scenarios/file?path=${encodeURIComponent(path)}`),
  clonesSupported: () => request<SupportedClone[]>("/api/clones/supported"),
  clone: {
    start: (id: string) =>
      request<CloneInfo>(`/api/clones/${encodeURIComponent(id)}`, { method: "POST" }),
    stop: (id: string) =>
      request<{ id: string; was_running: boolean }>(
        `/api/clones/${encodeURIComponent(id)}`,
        { method: "DELETE" },
      ),
    seed: (id: string, name: string) =>
      request<{ ok: boolean; status?: number; error?: string }>(
        `/api/clones/${encodeURIComponent(id)}/seed/${encodeURIComponent(name)}`,
        { method: "POST" },
      ),
    reset: (id: string) =>
      request<{ ok: boolean; status?: number; error?: string }>(
        `/api/clones/${encodeURIComponent(id)}/reset`,
        { method: "POST" },
      ),
    tools: (id: string) =>
      request<{ ok: boolean; tools: { name: string; description?: string }[] }>(
        `/api/clones/${encodeURIComponent(id)}/tools`,
      ),
  },
  report: (params: { scenario?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.scenario) q.set("scenario", params.scenario);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<ReportTrend>(`/api/report${qs ? `?${qs}` : ""}`);
  },
  compare: (a: string, b: string) =>
    request<{ rec_a: RunRecord; rec_b: RunRecord; diff: CompareDiff }>(
      `/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`,
    ),
  jobs: {
    start: (scenario: string, opts: {
      docker?: boolean;
      harness?: string;
      model?: string;
      timeout?: number;
      clone?: string;
      runs?: number;
      rate_limit?: number;
      read_only?: boolean;
      no_failure_analysis?: boolean;
      seed_file?: string;
      setup_file?: string;
      keep_state?: boolean;
      fresh_seed?: boolean;
      docker_logs?: boolean;
    } = {}) =>
      request<RunJob>("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario, ...opts }),
      }),
    list: () => request<RunJob[]>("/api/jobs"),
    get: (jobId: string) => request<RunJob>(`/api/jobs/${jobId}`),
    cancel: (jobId: string) =>
      request<RunJob>(`/api/jobs/${jobId}`, { method: "DELETE" }),
  },
  telemetry: (runId: string) => request<TelemetryReport>(`/api/runs/${runId}/telemetry`),
  anonymizedRun: (runId: string) => request<RunRecord>(`/api/runs/${runId}/anonymized`),
  doctor: () => request<DoctorReport>("/api/doctor"),
  config: {
    get: (revealEnv = false) =>
      request<ConfigReport>(`/api/config${revealEnv ? "?reveal_env=true" : ""}`),
    set: (key: string, value: unknown) =>
      request<{ key: string; value: unknown }>(
        `/api/config/${encodeURIComponent(key)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value }),
        },
      ),
    unset: (key: string) =>
      request<{ key: string; removed: boolean }>(
        `/api/config/${encodeURIComponent(key)}`,
        { method: "DELETE" },
      ),
  },
  validateScenario: (body: { raw?: string; path?: string }) =>
    request<ValidateScenarioReport>("/api/scenarios/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};

export interface DoctorReport {
  all_passed: boolean;
  checks: { name: string; ok: boolean; detail: string; fix: string | null }[];
}

export interface ConfigReport {
  path: string;
  exists: boolean;
  values: Record<string, unknown>;
  known_keys: Record<string, string>;
}

export interface ValidateScenarioReport {
  ok: boolean;
  errors: string[];
  warnings: string[];
  scenario: {
    title: string;
    prompt: string;
    setup: string;
    expected: string;
    criteria: { text: string; kind: "D" | "P" }[];
    clones: string[];
    config: Record<string, unknown>;
  } | null;
}

export { ApiError };
