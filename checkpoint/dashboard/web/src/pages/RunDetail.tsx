import { useMemo, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Check,
  Copy,
  Download,
  FileJson,
  ListTree,
  TerminalSquare,
  Wrench,
} from "lucide-react";
import { ApiError, api, type RunRecord, type TelemetryReport } from "@/lib/api";
import { fmtTimestamp, scoreColor, stableStringify } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
  StatTile,
} from "@/components/bits";
import TraceTimeline from "@/components/TraceTimeline";

type Tab = "process" | "tools" | "judge" | "data";

function buildTelemetryFromRecord(r: RunRecord): TelemetryReport {
  const trace = Array.isArray(r.trace) ? r.trace : [];
  const criteria = Array.isArray(r.criteria) ? r.criteria : [];
  const messages = extractMessages(r.agent_trace);
  const toolCalls = extractToolCalls(r.agent_trace);
  const apiCalls = trace.map((ev, index) => ({
    ...ev,
    index,
    clone: ev._clone || (ev as { clone?: string }).clone || null,
    raw: ev,
  }));
  const judgeCriteria = criteria.map((c, index) => ({
    index,
    kind: c.kind,
    text: c.text,
    passed: c.passed,
    evaluator: c.evaluator,
    reasoning: c.reasoning,
    raw: c,
  }));
  const timestamp = (r.env as { timestamp?: string } | null)?.timestamp || null;
  const passed = criteria.filter((c) => c.passed).length;
  const stdout = r.stdout ?? r.final_answer ?? "";
  const stderr = r.stderr ?? "";
  const rawMetrics = r.metrics || {};
  const totalTokens =
    numberMetric(rawMetrics, "totalTokens", "total_tokens") ??
    sumMaybe(
      numberMetric(rawMetrics, "inputTokens", "input_tokens", "prompt_tokens"),
      numberMetric(rawMetrics, "outputTokens", "output_tokens", "completion_tokens"),
    );

  const timeline: TelemetryReport["timeline"] = [
    {
      kind: "run",
      label: "Run started",
      timestamp,
      status: r.error ? "error" : "ok",
      detail: r.scenario,
    },
    ...messages.map((m) => ({
      kind: "chat" as const,
      label: m.role || "message",
      timestamp: m.timestamp || null,
      status: "ok",
      detail: brief(m.content),
      ref: { section: "chat", index: m.index },
    })),
    ...toolCalls.map((call) => ({
      kind: "tool" as const,
      label: call.name,
      timestamp: call.timestamp || null,
      status: call.status || "ok",
      detail: brief(call.summary || call.name),
      ref: { section: "tool_calls", index: call.index },
    })),
    ...apiCalls.map((call) => ({
      kind: "api" as const,
      label: `${call.method || "UNKNOWN"} ${call.path || ""}`,
      timestamp: call.timestamp || null,
      status: typeof call.status === "number" && call.status >= 400 ? "error" : "ok",
      detail: `${call.status || "-"} ${call.clone || ""}`.trim(),
      ref: { section: "api_calls", index: call.index },
    })),
    ...judgeCriteria.map((c) => ({
      kind: "judge" as const,
      label: `[${c.kind}] ${c.text}`,
      timestamp: null,
      status: c.passed ? "ok" : "error",
      detail: brief(c.reasoning || ""),
      ref: { section: "judge", index: c.index },
    })),
    {
      kind: "run",
      label: "Run ended",
      timestamp: null,
      status: r.exit_code === 0 && !r.error ? "ok" : "error",
      detail: `exit ${r.exit_code ?? "-"}`,
    },
  ];

  return {
    run_id: r.run_id,
    summary: {
      scenario: r.scenario,
      scenario_path: r.scenario_path,
      satisfaction: r.satisfaction,
      criteria_passed: passed,
      criteria_total: criteria.length,
      api_call_count: apiCalls.length,
      agent_message_count: messages.length,
      agent_step_count: 0,
      tool_call_count: toolCalls.length,
      duration_ms: r.duration_ms ?? null,
      timestamp,
      exit_code: r.exit_code,
      harness: r.harness || {},
    },
    cli: buildCliCommands(r),
    chat: {
      messages,
      raw: r.agent_trace ?? null,
      capture_note:
        "Derived in-browser from the run record because the canonical telemetry endpoint was unavailable.",
    },
    transcript: {
      stdout,
      stderr,
      final_answer: r.final_answer || "",
      error: r.error || null,
      exit_code: r.exit_code,
    },
    steps: [],
    tool_calls: toolCalls,
    api_calls: apiCalls,
    judge: {
      model: r.evaluator_model,
      model_source: r.evaluator_model_source || null,
      criteria: judgeCriteria,
      failure_analysis: r.failure_analysis || {},
    },
    metrics: {
      raw: rawMetrics,
      duration_ms: r.duration_ms ?? null,
      prompt_tokens: numberMetric(rawMetrics, "inputTokens", "input_tokens", "prompt_tokens"),
      completion_tokens: numberMetric(rawMetrics, "outputTokens", "output_tokens", "completion_tokens"),
      total_tokens: totalTokens,
      llm_call_count: numberMetric(rawMetrics, "llmCallCount", "llm_call_count", "model_calls"),
      tool_call_count:
        numberMetric(rawMetrics, "toolCallCount", "tool_call_count") ?? toolCalls.length,
      api_call_count: apiCalls.length,
      error_count: apiCalls.filter((call) => typeof call.status === "number" && call.status >= 400).length,
    },
    timeline,
    state: r.state || {},
    raw: {
      record: r,
      agent_trace: r.agent_trace ?? null,
    },
  };
}

export default function RunDetail() {
  const { runId = "" } = useParams();
  const runQ = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: Boolean(runId),
  });
  const telemetryQ = useQuery({
    queryKey: ["run", runId, "telemetry"],
    queryFn: () => api.telemetry(runId),
    enabled: Boolean(runId),
    retry: (failureCount, error) =>
      error instanceof ApiError && error.status === 404 ? false : failureCount < 1,
  });
  const [tab, setTab] = useState<Tab>("process");

  if (runQ.isLoading || (telemetryQ.isLoading && !runQ.data)) return <Loading />;
  if (runQ.error) return <ErrorBox error={runQ.error} />;
  if (!runQ.data) return null;

  const r = runQ.data;
  const telemetryFallback = buildTelemetryFromRecord(r);
  const t = telemetryQ.data || telemetryFallback;
  const usingFallback = !telemetryQ.data;
  const sat = Math.round(t.summary.satisfaction);

  return (
    <>
      <Link
        to="/"
        className="text-xs text-ink-3 dark:text-paper-3 inline-flex items-center gap-1 hover:underline"
      >
        <ArrowLeft size={12} /> back to runs
      </Link>

      <PageHead
        title={t.summary.scenario || "(inline task)"}
        sub={
          <>
            Run <code className="font-mono">{t.run_id}</code> ·{" "}
            {fmtTimestamp(t.summary.timestamp)}
          </>
        }
        right={
          <div className="flex flex-wrap gap-2">
            <a href={`/api/runs/${r.run_id}`} className="btn-outline" download={`run-${r.run_id}.json`}>
              <Download size={14} /> Record
            </a>
            <a href={`/api/runs/${r.run_id}/telemetry`} className="btn-outline" download={`telemetry-${r.run_id}.json`}>
              <FileJson size={14} /> Telemetry
            </a>
            <a
              href={`/api/runs/${r.run_id}/anonymized`}
              className="btn-outline"
              download={`run-${r.run_id}-anonymized.json`}
              title="Strips emails / GitHub PATs / OpenAI keys before download — safe to share."
            >
              <Download size={14} /> Anonymized
            </a>
          </div>
        }
      />

      {usingFallback && telemetryQ.error && (
        <div className="card border-warn bg-warn-soft text-warn mb-5">
          <div className="card-title !text-warn">Telemetry endpoint fallback</div>
          <div className="text-sm">
            The dashboard could not load <code className="font-mono">/api/runs/{r.run_id}/telemetry</code>, so this view
            is derived locally from the run record. Restart <code className="font-mono">checkpoint serve</code> to expose
            the canonical telemetry API from the updated backend.
          </div>
        </div>
      )}

      <IdentityStrip report={t} />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Satisfaction" value={`${sat} / 100`} color={scoreColor(sat)} />
        <StatTile
          label="Criteria"
          value={
            <>
              <span style={{ color: "#0ea83b" }}>{t.summary.criteria_passed}</span>
              <span className="text-ink-4 text-lg"> / </span>
              <span>{t.summary.criteria_total}</span>
            </>
          }
        />
        <StatTile
          label="Process"
          value={t.timeline.length}
          sub={`${t.summary.agent_step_count} agent steps`}
        />
        <StatTile
          label="Calls"
          value={t.summary.api_call_count}
          sub={`${t.summary.tool_call_count} agent tools`}
        />
      </div>

      <CommandShelf commands={t.cli} />

      <div className="flex flex-wrap gap-2 my-6">
        <TabButton active={tab === "process"} onClick={() => setTab("process")} icon={<ListTree size={14} />}>
          Process
        </TabButton>
        <TabButton active={tab === "tools"} onClick={() => setTab("tools")} icon={<Wrench size={14} />}>
          Tool calls
        </TabButton>
        <TabButton active={tab === "judge"} onClick={() => setTab("judge")} icon={<Check size={14} />}>
          Judge
        </TabButton>
        <TabButton active={tab === "data"} onClick={() => setTab("data")} icon={<TerminalSquare size={14} />}>
          Raw data
        </TabButton>
      </div>

      {tab === "process" && <ProcessPanel report={t} />}
      {tab === "tools" && <ToolsPanel report={t} />}
      {tab === "judge" && <JudgePanel report={t} />}
      {tab === "data" && <DataPanel report={t} />}
    </>
  );
}

function IdentityStrip({ report: t }: { report: TelemetryReport }) {
  const h = t.summary.harness || {};
  return (
    <div className="card mb-5">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
        <Identity label="Scenario" value={t.summary.scenario || "(inline)"} sub={t.summary.scenario_path} />
        <Identity label="Agent" value={String(h.name || "unknown")} sub={String(h.dir || h.cmd || "")} />
        <Identity label="Mode" value={String(h.mode || "-")} />
        <Identity label="Judge" value={t.judge.model || "-"} sub={t.judge.model_source} />
        <Identity
          label="Duration"
          value={t.summary.duration_ms ? `${(t.summary.duration_ms / 1000).toFixed(1)}s` : "-"}
          sub={t.summary.exit_code === null ? "" : `exit ${t.summary.exit_code}`}
        />
      </div>
    </div>
  );
}

function buildCliCommands(r: RunRecord): Record<string, string> {
  const runId = r.run_id || "<run-id>";
  const scenarioPath = r.scenario_path || "<scenario.md>";
  const h = r.harness || {};
  const replay = `checkpoint replay ${runId}`;
  const rerun = ["checkpoint", "run", scenarioPath];
  if (h.mode === "docker") {
    rerun.push("--docker");
    if (h.dir) rerun.push("--harness-dir", h.dir);
  } else if (h.cmd) {
    rerun.push("--harness", h.cmd, "--no-docker");
  }
  return {
    detail: `checkpoint traces detail ${runId}`,
    telemetry: `checkpoint traces telemetry ${runId}`,
    replay,
    replay_json: `${replay} --json`,
    export: `checkpoint traces export ${runId} --output ${runId}.json`,
    rerun: rerun.join(" "),
  };
}

function extractMessages(agentTrace: unknown): TelemetryReport["chat"]["messages"] {
  const candidates = keyedArrays(agentTrace, new Set(["messages", "conversation", "chat", "transcript"]));
  const out: TelemetryReport["chat"]["messages"] = [];
  for (const value of candidates.flatMap((v) => v)) {
    const msg = messageFromValue(value, out.length);
    if (msg) out.push(msg);
  }
  if (out.length > 0) return out;
  for (const value of eventLike(agentTrace)) {
    const msg = messageFromValue(value, out.length);
    if (msg) out.push(msg);
  }
  return out;
}

function extractToolCalls(agentTrace: unknown): TelemetryReport["tool_calls"] {
  const candidates = keyedArrays(agentTrace, new Set(["tool_calls", "toolCalls", "tools", "calls"]));
  const out: TelemetryReport["tool_calls"] = [];
  for (const value of candidates.flatMap((v) => v)) {
    const call = toolCallFromValue(value, out.length);
    if (call) out.push(call);
  }
  for (const value of eventLike(agentTrace)) {
    const call = toolCallFromValue(value, out.length);
    if (call && !out.some((existing) => existing.raw === call.raw)) out.push(call);
  }
  return out;
}

function keyedArrays(value: unknown, keys: Set<string>, depth = 0): unknown[][] {
  if (depth > 8 || value === null || value === undefined) return [];
  if (Array.isArray(value)) {
    return value.flatMap((item) => keyedArrays(item, keys, depth + 1));
  }
  if (typeof value !== "object") return [];
  const out: unknown[][] = [];
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    if (keys.has(key) && Array.isArray(child)) out.push(child);
    out.push(...keyedArrays(child, keys, depth + 1));
  }
  return out;
}

function eventLike(value: unknown, depth = 0): unknown[] {
  if (depth > 8 || value === null || value === undefined) return [];
  if (Array.isArray(value)) return value.flatMap((item) => eventLike(item, depth + 1));
  if (typeof value !== "object") return [];
  const obj = value as Record<string, unknown>;
  const out: unknown[] = ["role", "content", "tool", "tool_name", "function"].some((key) =>
    Object.prototype.hasOwnProperty.call(obj, key),
  )
    ? [value]
    : [];
  for (const child of Object.values(obj)) out.push(...eventLike(child, depth + 1));
  return out;
}

function messageFromValue(value: unknown, index: number): TelemetryReport["chat"]["messages"][number] | null {
  if (typeof value === "string") return { index, role: "message", content: value, raw: value };
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  const role = stringValue(obj.role ?? obj.speaker ?? obj.author ?? obj.type) || "message";
  const content = textContent(obj.content ?? obj.text ?? obj.message);
  if (!content && !obj.role && !obj.speaker && !obj.author) return null;
  return {
    index,
    role,
    content,
    timestamp: stringValue(obj.timestamp ?? obj.ts),
    name: stringValue(obj.name),
    raw: value,
  };
}

function toolCallFromValue(value: unknown, index: number): TelemetryReport["tool_calls"][number] | null {
  if (value === null || typeof value !== "object") return null;
  const obj = value as Record<string, unknown>;
  const fn = obj.function && typeof obj.function === "object" ? (obj.function as Record<string, unknown>) : {};
  const kind = String(obj.type ?? obj.event ?? obj.kind ?? "").toLowerCase();
  const name = stringValue(obj.tool ?? obj.tool_name ?? obj.name ?? fn.name ?? obj.id);
  if (!name && !kind.includes("tool") && !kind.includes("function")) return null;
  return {
    index,
    name: name || "tool",
    type: stringValue(obj.type ?? obj.event ?? obj.kind),
    status: stringValue(obj.status),
    timestamp: stringValue(obj.timestamp ?? obj.ts),
    input: obj.input ?? obj.arguments ?? obj.args,
    output: obj.output ?? obj.result ?? obj.response,
    summary: stringValue(obj.summary ?? obj.text),
    raw: value,
  };
}

function numberMetric(metrics: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = metrics[key];
    if (typeof value === "number") return value;
  }
  return null;
}

function sumMaybe(a: number | null, b: number | null): number | null {
  if (a === null && b === null) return null;
  return (a || 0) + (b || 0);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" ? value : value === undefined || value === null ? null : String(value);
}

function textContent(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => textContent(item)).filter(Boolean).join("\n");
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return textContent(obj.text ?? obj.content ?? stableStringify(obj));
  }
  return String(value);
}

function brief(value: unknown, limit = 220): string {
  const text = textContent(value).replace(/\s+/g, " ").trim();
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

function CommandShelf({ commands }: { commands: Record<string, string> }) {
  const rows = ["detail", "telemetry", "replay", "replay_json", "export", "rerun"]
    .map((key) => [key, commands[key]] as const)
    .filter(([, value]) => value);
  if (rows.length === 0) return null;
  return (
    <div className="card-tight mb-5">
      <div className="px-4 py-2 border-b border-ink bg-paper-2 dark:bg-ink text-label font-mono uppercase text-ink-3 dark:text-paper-3">
        CLI parity
      </div>
      <div className="divide-y divide-paper-3 dark:divide-ink-3">
        {rows.map(([key, cmd]) => (
          <div key={key} className="grid md:grid-cols-[120px_1fr_auto] gap-2 px-4 py-2 items-center">
            <span className="text-xs font-mono text-ink-3 dark:text-paper-3">{key}</span>
            <code className="font-mono text-xs break-all">{cmd}</code>
            <CopyBtn text={cmd} />
          </div>
        ))}
      </div>
    </div>
  );
}

function ProcessPanel({ report: t }: { report: TelemetryReport }) {
  // Failure-first: when criteria failed, lead with a diagnosis card so the
  // user sees "what went wrong" before drowning in the full timeline.
  const failed = (t.judge.criteria || []).filter((c) => !c.passed);
  const failureAnalysis = t.judge.failure_analysis || {};
  const hasFailures = failed.length > 0 || !!t.transcript.error;

  return (
    <div className="grid xl:grid-cols-[1fr_360px] gap-5">
      <div className="space-y-5">
        {hasFailures && (
          <FailureDiagnosis
            failed={failed}
            failureAnalysis={failureAnalysis}
            error={t.transcript.error}
            finalAnswer={t.transcript.final_answer}
          />
        )}

        <div>
          <div className="card-title">Process timeline ({t.timeline.length} steps)</div>
          <div className="card-tight">
            <table className="ck-table">
              <thead>
                <tr>
                  <th className="!w-20">Kind</th>
                  <th>Step</th>
                  <th className="!w-24">Status</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {t.timeline.map((step, i) => (
                  <tr key={i}>
                    <td>
                      <Badge variant={step.status === "error" ? "fail" : step.kind === "judge" ? "judge" : "info"}>
                        {step.kind}
                      </Badge>
                    </td>
                    <td>
                      <div className="font-medium">{step.label}</div>
                      {step.timestamp && (
                        <div className="text-[11px] font-mono text-ink-4 dark:text-paper-3">
                          {fmtTimestamp(step.timestamp)}
                        </div>
                      )}
                    </td>
                    <td className="font-mono text-xs">{step.status}</td>
                    <td className="text-xs text-ink-3 dark:text-paper-3">{step.detail || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      <div className="space-y-4">
        <MetricBlock metrics={t.metrics} />
        <details className="card" open>
          <summary className="cursor-pointer font-medium">Final answer</summary>
          <pre className="json mt-3">{t.transcript.final_answer || "(empty)"}</pre>
        </details>
      </div>
    </div>
  );
}

/**
 * The "what went wrong" card that leads ProcessPanel for any failed run.
 * Two-tier layout: each failed criterion + its judge reasoning, then any
 * LLM-generated failure_analysis prose underneath.
 */
function FailureDiagnosis({
  failed,
  failureAnalysis,
  error,
  finalAnswer,
}: {
  failed: TelemetryReport["judge"]["criteria"];
  failureAnalysis: Record<string, string>;
  error: string | null;
  finalAnswer: string;
}) {
  return (
    <div className="card border-fail bg-fail-soft">
      <div className="card-title !text-fail">What went wrong</div>

      {error && (
        <div className="mb-3 p-2 bg-fail/10 border-l-2 border-fail">
          <div className="text-[10px] uppercase font-mono tracking-wider text-fail">
            Run error
          </div>
          <div className="font-mono text-xs mt-1">{error}</div>
        </div>
      )}

      {failed.length === 0 && !error && (
        <div className="text-sm text-ink-3 dark:text-paper-3">No failed criteria.</div>
      )}

      <ul className="space-y-3">
        {failed.map((c, i) => (
          <li key={i} className="border-l-2 border-fail pl-3">
            <div className="flex items-start gap-2">
              <Badge variant={c.kind === "P" ? "judge" : "d"}>{c.kind}</Badge>
              <div className="flex-1 min-w-0">
                <div className="font-medium text-sm">{c.text}</div>
                {c.reasoning && (
                  <div className="text-xs text-ink-3 dark:text-paper-3 mt-1 italic">
                    Judge said: {c.reasoning}
                  </div>
                )}
                {c.evaluator && (
                  <div className="text-[10px] font-mono text-ink-4 dark:text-paper-3 mt-1">
                    evaluator: {c.evaluator}
                  </div>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>

      {Object.keys(failureAnalysis).length > 0 && (
        <details className="mt-3 border-t border-fail/30 pt-2">
          <summary className="cursor-pointer text-xs font-medium text-fail">
            LLM root-cause analysis
          </summary>
          <div className="mt-2 space-y-2">
            {Object.entries(failureAnalysis).map(([crit, why]) => (
              <div key={crit} className="text-xs">
                <strong className="block">{crit}</strong>
                <p className="text-ink-2 dark:text-paper-2 whitespace-pre-wrap">{why}</p>
              </div>
            ))}
          </div>
        </details>
      )}

      {finalAnswer && (
        <details className="mt-3 border-t border-fail/30 pt-2">
          <summary className="cursor-pointer text-xs font-medium text-fail">
            What the agent said at the end
          </summary>
          <pre className="text-xs whitespace-pre-wrap mt-2 font-mono">{finalAnswer}</pre>
        </details>
      )}
    </div>
  );
}

function ToolsPanel({ report: t }: { report: TelemetryReport }) {
  return (
    <div className="space-y-7">
      <div>
        <div className="section-title">Agent tool calls ({t.tool_calls.length})</div>
        {t.tool_calls.length === 0 ? (
          <div className="card text-sm text-ink-3 dark:text-paper-3">No structured agent tool calls were emitted.</div>
        ) : (
          <div className="grid md:grid-cols-2 gap-4">
            {t.tool_calls.map((call) => (
              <details key={call.index} className="card" open={call.index < 4}>
                <summary className="cursor-pointer font-medium">
                  <span className="font-mono text-xs text-ink-3 dark:text-paper-3">#{call.index}</span>{" "}
                  {call.name}
                </summary>
                <div className="grid gap-3 mt-3">
                  <pre className="json">{stableStringify({ input: call.input, output: call.output, raw: call.raw })}</pre>
                </div>
              </details>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="section-title">Twin/API trace ({t.api_calls.length})</div>
        <TraceTimeline events={t.api_calls} />
      </div>
    </div>
  );
}

function JudgePanel({ report: t }: { report: TelemetryReport }) {
  return (
    <>
      <div className="card-tight mb-6">
        <table className="ck-table">
          <thead>
            <tr>
              <th className="!w-10">Kind</th>
              <th>Criterion</th>
              <th className="!w-24">Result</th>
              <th className="!w-32">Evaluator</th>
              <th>Reasoning</th>
            </tr>
          </thead>
          <tbody>
            {t.judge.criteria.map((c) => (
              <tr key={c.index}>
                <td><Badge variant={c.kind === "P" ? "judge" : "d"}>{c.kind}</Badge></td>
                <td>{c.text}</td>
                <td><Badge variant={c.passed ? "pass" : "fail"}>{c.passed ? "pass" : "fail"}</Badge></td>
                <td className="font-mono text-xs">{c.evaluator || "-"}</td>
                <td className="text-ink-3 dark:text-paper-3 text-xs">{c.reasoning || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {Object.keys(t.judge.failure_analysis || {}).length > 0 && (
        <>
          <div className="section-title">Failure analysis</div>
          {Object.entries(t.judge.failure_analysis).map(([crit, why]) => (
            <div className="card mb-4" key={crit}>
              <div className="card-title">Failed criterion</div>
              <strong className="block mb-2">{crit}</strong>
              <p className="text-ink-2 dark:text-paper-2 whitespace-pre-wrap text-sm">{why}</p>
            </div>
          ))}
        </>
      )}
    </>
  );
}

function DataPanel({ report: t }: { report: TelemetryReport }) {
  return (
    <div className="grid xl:grid-cols-2 gap-5">
      <details className="card" open>
        <summary className="cursor-pointer font-medium">Twin state</summary>
        <pre className="json mt-3">{stableStringify(t.state)}</pre>
      </details>
      <details className="card" open>
        <summary className="cursor-pointer font-medium">Raw telemetry report</summary>
        <pre className="json mt-3">{stableStringify(t.raw.record)}</pre>
      </details>
    </div>
  );
}

function MetricBlock({ metrics }: { metrics: Record<string, unknown> }) {
  const rows = useMemo(
    () =>
      [
        ["duration", metrics.duration_ms ? `${(Number(metrics.duration_ms) / 1000).toFixed(1)}s` : "-"],
        ["tokens", metrics.total_tokens ?? "-"],
        ["llm calls", metrics.llm_call_count ?? "-"],
        ["tool calls", metrics.tool_call_count ?? "-"],
        ["api errors", metrics.error_count ?? 0],
      ] as [string, unknown][],
    [metrics],
  );
  return (
    <div className="card">
      <div className="card-title">Telemetry</div>
      <div className="grid grid-cols-2 gap-3">
        {rows.map(([label, value]) => (
          <div key={label}>
            <div className="text-[10px] font-mono uppercase text-ink-4 dark:text-paper-3 tracking-wider">{label}</div>
            <div className="font-mono text-lg font-semibold">{String(value)}</div>
          </div>
        ))}
      </div>
      <details className="mt-4">
        <summary className="cursor-pointer text-sm">Raw metrics</summary>
        <pre className="json mt-3">{stableStringify(metrics.raw || {})}</pre>
      </details>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button type="button" className={active ? "btn-accent" : "btn-outline"} onClick={onClick}>
      {icon}
      {children}
    </button>
  );
}

function Identity({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string | null;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase font-mono tracking-wider text-ink-3 dark:text-paper-3">
        {label}
      </div>
      <div className="font-medium text-base mt-0.5 truncate">{value}</div>
      {sub && (
        <div className="text-[11px] font-mono text-ink-4 dark:text-paper-3 truncate mt-0.5">
          {sub}
        </div>
      )}
    </div>
  );
}

function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn-ghost !h-7 !text-[11px]"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          /* ignore */
        }
      }}
      title="Copy"
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </button>
  );
}
