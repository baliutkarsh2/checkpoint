import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { ArrowLeft, Play, Settings2 } from "lucide-react";
import { api } from "@/lib/api";
import { fmtTimestamp, scoreColor, shortId } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
  ScoreBar,
  StatTile,
} from "@/components/bits";

/**
 * One scenario in full: sections (prompt / setup / criteria / config),
 * raw markdown, and the run history that this scenario has produced.
 *
 * The Run button starts it against the agent in checkpoint.toml; the settings
 * panel beside it exposes the `checkpoint run` options worth changing per run.
 */
export default function ScenarioDetail() {
  const [params] = useSearchParams();
  const path = params.get("path") || "";
  const q = useQuery({
    queryKey: ["scenarios", "file", path],
    queryFn: () => api.scenarioFile(path),
    enabled: Boolean(path),
  });

  if (!path) return <ErrorBox error={new Error("No scenario path provided")} />;
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;
  const s = q.data;

  return (
    <>
      <Link
        to="/scenarios"
        className="text-xs text-ink-3 dark:text-paper-3 inline-flex items-center gap-1 hover:underline"
      >
        <ArrowLeft size={12} /> back to scenarios
      </Link>

      <PageHead
        title={s.title || "(untitled scenario)"}
        sub={
          <>
            <code className="font-mono">{s.path}</code>
            {s.twins.length > 0 && (
              <>
                {" · twins: "}
                {s.twins.map((c, i) => (
                  <span key={c}>
                    {i > 0 && ", "}
                    <span className="font-mono">{c}</span>
                  </span>
                ))}
              </>
            )}
          </>
        }
        right={<RunButton scenarioPath={s.path} />}
      />

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Total runs" value={s.stats.total_runs} />
        <StatTile
          label="Avg score"
          value={s.stats.total_runs ? s.stats.avg_score : "—"}
          color={s.stats.total_runs ? scoreColor(s.stats.avg_score) : undefined}
        />
        <StatTile
          label="Pass rate"
          value={s.stats.total_runs ? `${s.stats.pass_rate}%` : "—"}
        />
        <StatTile
          label="Last run"
          value={s.stats.last_at ? fmtTimestamp(s.stats.last_at) : "—"}
        />
      </div>

      {/* Sections */}
      <div className="grid md:grid-cols-2 gap-5 mb-7">
        <div className="card">
          <div className="card-title">Prompt</div>
          <pre className="whitespace-pre-wrap text-sm font-sans">{s.prompt || "(empty)"}</pre>
        </div>
        <div className="card">
          <div className="card-title">Setup</div>
          <pre className="whitespace-pre-wrap text-sm font-sans">{s.setup || "(no setup section)"}</pre>
        </div>
      </div>

      <div className="card mb-7">
        <div className="card-title">Success criteria ({s.criteria.length})</div>
        <ul className="space-y-1.5">
          {s.criteria.length === 0 && (
            <li className="text-sm italic text-ink-3 dark:text-paper-3">No criteria defined.</li>
          )}
          {s.criteria.map((c, i) => (
            <li key={i} className="flex items-start gap-2 text-sm">
              <Badge variant={c.kind === "P" ? "judge" : "d"}>{c.kind}</Badge>
              <span>{c.text}</span>
            </li>
          ))}
        </ul>
      </div>

      {/* Run history */}
      <div className="section-title">Recent runs ({s.runs.length})</div>
      {s.runs.length === 0 ? (
        <div className="card text-center text-ink-3 dark:text-paper-3">
          No runs yet for this scenario. Click <strong>Run</strong> above.
        </div>
      ) : (
        <div className="card-tight mb-7">
          <table className="ck-table">
            <thead>
              <tr>
                <th>Run</th>
                <th>Agent</th>
                <th>Score</th>
                <th>Criteria</th>
                <th>Duration</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {s.runs.slice(0, 25).map((r) => (
                <tr key={r.run_id} className="row-link">
                  <td className="font-mono text-xs">
                    <Link to={`/runs/${r.run_id}`} className="hover:underline">
                      {shortId(r.run_id)}
                    </Link>
                  </td>
                  <td className="font-mono text-xs">{r.harness_name || "—"}</td>
                  <td>
                    <ScoreBar score={r.satisfaction} />
                  </td>
                  <td className="font-mono text-xs">
                    {r.criteria_pass}/{r.criteria_total}
                  </td>
                  <td className="font-mono text-xs">
                    {r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}
                  </td>
                  <td className="text-xs">{fmtTimestamp(r.timestamp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Raw markdown */}
      <details className="card">
        <summary className="cursor-pointer font-medium">Raw scenario file</summary>
        <pre className="json mt-3">{s.raw}</pre>
      </details>
    </>
  );
}

function RunButton({ scenarioPath }: { scenarioPath: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [advanced, setAdvanced] = useState(false);
  const [runs, setRuns] = useState(1);
  const [timeout, setTimeoutValue] = useState("");
  const [model, setModel] = useState("");
  const [rateLimit, setRateLimit] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [keepState, setKeepState] = useState(false);
  const [explain, setExplain] = useState(false);
  const startMut = useMutation({
    mutationFn: () =>
      api.jobs.start(scenarioPath, {
        runs,
        timeout: timeout ? Number(timeout) : undefined,
        model: model || undefined,
        rate_limit: rateLimit ? Number(rateLimit) : undefined,
        read_only: readOnly,
        keep_state: keepState,
        explain,
      }),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/live/${job.job_id}`);
    },
  });

  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn-outline"
          onClick={() => setAdvanced((v) => !v)}
          title="Run settings"
        >
          <Settings2 size={14} />
        </button>
        <button
          type="button"
          className="btn-accent"
          disabled={startMut.isPending}
          onClick={() => startMut.mutate()}
        >
          <Play size={14} />
          {startMut.isPending ? "Starting…" : "Run"}
        </button>
      </div>
      {advanced && (
        <div className="card-flat !p-3 w-[min(680px,calc(100vw-2rem))]">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <label className="text-xs">
              <span className="block font-mono text-[10px] uppercase text-ink-4 dark:text-paper-3 mb-1">Runs</span>
              <input className="input w-full" type="number" min={1} value={runs} onChange={(e) => setRuns(Math.max(1, Number(e.target.value) || 1))} />
            </label>
            <label className="text-xs">
              <span className="block font-mono text-[10px] uppercase text-ink-4 dark:text-paper-3 mb-1">Timeout</span>
              <input className="input w-full" type="number" min={1} value={timeout} onChange={(e) => setTimeoutValue(e.target.value)} placeholder="seconds" />
            </label>
            <label className="text-xs">
              <span className="block font-mono text-[10px] uppercase text-ink-4 dark:text-paper-3 mb-1">Rate limit</span>
              <input className="input w-full" type="number" min={1} value={rateLimit} onChange={(e) => setRateLimit(e.target.value)} placeholder="calls per twin" />
            </label>
            <label className="text-xs">
              <span className="block font-mono text-[10px] uppercase text-ink-4 dark:text-paper-3 mb-1">Judge model</span>
              <input className="input w-full" value={model} onChange={(e) => setModel(e.target.value)} placeholder="default" />
            </label>
          </div>
          <div className="flex flex-wrap gap-3 mt-3 text-xs">
            <label className="inline-flex items-center gap-1.5"><input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} /> refuse every write</label>
            <label className="inline-flex items-center gap-1.5"><input type="checkbox" checked={keepState} onChange={(e) => setKeepState(e.target.checked)} /> do not reseed the twins</label>
            <label className="inline-flex items-center gap-1.5"><input type="checkbox" checked={explain} onChange={(e) => setExplain(e.target.checked)} /> explain failed criteria</label>
          </div>
        </div>
      )}
      {startMut.isError && <ErrorBox error={startMut.error} />}
    </div>
  );
}
