import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { ArrowLeft, Play } from "lucide-react";
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
 * One agent in detail: README, all-time runs, and per-scenario roll-up so
 * you can see "Openai Tools scored 100/100 on github-happy-path 5×, 50/100
 * on linear-issue-triage 1×" at a glance.
 */
export default function AgentDetail() {
  const { agentId = "" } = useParams();
  const q = useQuery({
    queryKey: ["agents", agentId],
    queryFn: () => api.agent(agentId),
    enabled: Boolean(agentId),
  });
  const scenariosQ = useQuery({ queryKey: ["scenarios"], queryFn: () => api.scenarios() });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const { agent, readme, runs, by_scenario, stats } = q.data;
  const sortedScenarios = Object.entries(by_scenario).sort(
    (a, b) => b[1].runs - a[1].runs,
  );

  return (
    <>
      <Link
        to="/agents"
        className="text-xs text-ink-3 dark:text-paper-3 inline-flex items-center gap-1 hover:underline"
      >
        <ArrowLeft size={12} /> back to agents
      </Link>

      <PageHead
        title={agent.name}
        sub={
          <>
            <span className="badge badge-info mr-2">{agent.source}</span>
            <code className="font-mono">{agent.path}</code>
          </>
        }
        right={<RunButton agentPath={agent.path} scenarios={scenariosQ.data?.scenarios || []} />}
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Total runs" value={stats.total_runs} />
        <StatTile
          label="Avg score"
          value={stats.total_runs ? stats.avg_score : "—"}
          color={stats.total_runs ? scoreColor(stats.avg_score) : undefined}
        />
        <StatTile
          label="Pass rate"
          value={stats.total_runs ? `${stats.pass_rate}%` : "—"}
        />
        <StatTile
          label="Last run"
          value={stats.last_at ? fmtTimestamp(stats.last_at) : "—"}
        />
      </div>

      {/* Per-scenario performance */}
      <div className="section-title">Per-scenario performance</div>
      {sortedScenarios.length === 0 ? (
        <div className="card text-center text-ink-3 dark:text-paper-3 mb-7">
          No runs yet for this agent. Use the launcher above to try it.
        </div>
      ) : (
        <div className="card-tight mb-7">
          <table className="ck-table">
            <thead>
              <tr>
                <th>Scenario</th>
                <th>Runs</th>
                <th>Avg score</th>
                <th>Last score</th>
                <th>Last at</th>
              </tr>
            </thead>
            <tbody>
              {sortedScenarios.map(([name, s]) => (
                <tr key={name} className="row-link">
                  <td>
                    <Link
                      to={`/?agent=${encodeURIComponent(agent.path)}&scenario=${encodeURIComponent(name)}`}
                      className="hover:underline"
                    >
                      {name}
                    </Link>
                  </td>
                  <td className="font-mono text-xs">{s.runs}</td>
                  <td>
                    <ScoreBar score={s.avg_score} />
                  </td>
                  <td>
                    {s.last_score !== null ? <ScoreBar score={s.last_score} /> : "—"}
                  </td>
                  <td className="text-xs">{fmtTimestamp(s.last_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Recent runs (capped) */}
      <div className="section-title">Recent runs ({runs.length})</div>
      {runs.length === 0 ? (
        <div className="card text-center text-ink-3 dark:text-paper-3 mb-7">
          (none yet)
        </div>
      ) : (
        <div className="card-tight mb-7">
          <table className="ck-table">
            <thead>
              <tr>
                <th>Run</th>
                <th>Scenario</th>
                <th>Score</th>
                <th>Criteria</th>
                <th>Mode</th>
                <th>Duration</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {runs.slice(0, 25).map((r) => (
                <tr key={r.run_id} className="row-link">
                  <td className="font-mono text-xs">
                    <Link to={`/runs/${r.run_id}`} className="hover:underline">
                      {shortId(r.run_id)}
                    </Link>
                  </td>
                  <td>{r.scenario || "—"}</td>
                  <td>
                    <ScoreBar score={r.satisfaction} />
                  </td>
                  <td className="font-mono text-xs">
                    {r.criteria_pass}/{r.criteria_total}
                  </td>
                  <td>
                    {r.mode === "docker" ? (
                      <Badge variant="info">docker</Badge>
                    ) : r.mode === "subprocess" ? (
                      <Badge>subproc</Badge>
                    ) : (
                      "—"
                    )}
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

      {/* README */}
      {readme && (
        <details className="card" open>
          <summary className="cursor-pointer font-medium">README</summary>
          <pre className="json mt-3 max-h-none">{readme}</pre>
        </details>
      )}
    </>
  );
}

function RunButton({
  agentPath,
  scenarios,
}: {
  agentPath: string;
  scenarios: { title: string; path: string }[];
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const startMut = useMutation({
    mutationFn: (scenarioPath: string) =>
      api.jobs.start(scenarioPath, { docker: true, harness: agentPath }),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/live/${job.job_id}`);
    },
  });

  if (scenarios.length === 0) {
    return <span className="text-xs text-ink-3 dark:text-paper-3">No scenarios available</span>;
  }

  return (
    <div className="flex items-center gap-2">
      <select id="scn-pick" className="input !text-xs" defaultValue={scenarios[0].path}>
        {scenarios.map((s) => (
          <option key={s.path} value={s.path}>
            {s.title}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="btn-accent"
        disabled={startMut.isPending}
        onClick={() => {
          const sel = document.getElementById("scn-pick") as HTMLSelectElement | null;
          startMut.mutate(sel?.value || scenarios[0].path);
        }}
      >
        <Play size={14} />
        {startMut.isPending ? "Starting…" : "Run"}
      </button>
    </div>
  );
}
