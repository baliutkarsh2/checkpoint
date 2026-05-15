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
  satisfaction: number;
  criteria: Criterion[];
  evaluator_model: string | null;
  env: Record<string, unknown> | null;
  trace: TraceEvent[];
  state: Record<string, unknown>;
  exit_code: number | null;
  final_answer: string | null;
  failure_analysis?: Record<string, string> | null;
  stderr?: string | null;
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

export interface RunSummary {
  run_id: string;
  scenario: string | null;
  satisfaction: number;
  criteria_pass: number;
  criteria_total: number;
  evaluator_model: string | null;
  timestamp: string | null;
  exit_code: number | null;
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
  runs: (params: { scenario?: string; page?: number; per_page?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.scenario) q.set("scenario", params.scenario);
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
    start: (scenario: string, opts: { docker?: boolean; harness?: string } = {}) =>
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
};

export { ApiError };
