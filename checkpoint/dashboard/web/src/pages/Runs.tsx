import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, ShieldCheck, X } from "lucide-react";
import { api } from "@/lib/api";
import { fmtTimestamp, scoreColor, shortId } from "@/lib/format";
import {
  EmptyState,
  ErrorBox,
  Loading,
  PageHead,
  ScoreBar,
  StatTile,
} from "@/components/bits";
import OnboardingGuide from "@/components/OnboardingGuide";
import { comparePicks, useComparePicks } from "@/lib/store";

export default function Runs() {
  const [params, setParams] = useSearchParams();
  const scenario = params.get("scenario") || "";
  const agent = params.get("agent") || "";
  const mode = params.get("mode") || "";
  const page = Number(params.get("page") || 1);
  const navigate = useNavigate();

  const summaryQ = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const twinsQ = useQuery({ queryKey: ["twins"], queryFn: api.twins });
  const runsQ = useQuery({
    queryKey: ["runs", { scenario, agent, mode, page }],
    queryFn: () =>
      api.runs({
        scenario: scenario || undefined,
        agent: agent || undefined,
        mode: mode || undefined,
        page,
      }),
  });
  const scenariosQ = useQuery({ queryKey: ["scenarios"], queryFn: () => api.scenarios() });

  const picks = useComparePicks();
  const [openLauncher, setOpenLauncher] = useState(false);

  // First-time experience: zero historical runs AND no filters → show the
  // onboarding guide instead of an empty table. Skips the noise of stat
  // tiles + filter chrome for users who haven't done anything yet.
  const noFilters = !scenario && !agent && !mode;
  const showOnboarding =
    summaryQ.data?.total_runs === 0 &&
    runsQ.data?.total === 0 &&
    noFilters;

  if (showOnboarding) {
    return (
      <>
        <PageHead
          title="Welcome to Checkpoint"
          sub="No runs yet — let's get you started."
          right={
            <button
              type="button"
              className="btn-accent"
              onClick={() => setOpenLauncher(true)}
            >
              <Play size={14} /> New run
            </button>
          }
        />
        <OnboardingGuide />
        {openLauncher && (
          <RunLauncher
            scenarios={scenariosQ.data?.scenarios || []}
            onClose={() => setOpenLauncher(false)}
          />
        )}
      </>
    );
  }

  return (
    <>
      <PageHead
        title="Run history"
        sub={
          runsQ.data
            ? `${runsQ.data.total} run${runsQ.data.total === 1 ? "" : "s"}${
                scenario ? ` · scenario ~ "${scenario}"` : ""
              }${agent ? ` · agent ~ "${agent}"` : ""}${
                mode ? ` · mode = ${mode}` : ""
              }`
            : "Loading…"
        }
        right={
          <div className="flex gap-2">
            <Link to="/gates" className="btn-outline">
              <ShieldCheck size={14} /> Gate results
            </Link>
            <button
              type="button"
              className="btn-accent"
              onClick={() => setOpenLauncher(true)}
            >
              <Play size={14} /> New run
            </button>
          </div>
        }
      />

      {/* Summary tiles */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-8">
        <StatTile
          label="Total runs"
          value={summaryQ.data?.total_runs ?? "—"}
          sub="all time"
        />
        <StatTile
          label="Avg score · 30d"
          value={summaryQ.data?.avg_score_30d ?? "—"}
          sub="/ 100"
          color={
            summaryQ.data
              ? scoreColor(summaryQ.data.avg_score_30d)
              : undefined
          }
        />
        <StatTile
          label="Pass rate · 30d"
          value={
            summaryQ.data ? (
              <>
                {summaryQ.data.pass_rate_30d}
                <span className="text-lg text-ink-3"> %</span>
              </>
            ) : (
              "—"
            )
          }
          sub="runs at 100"
        />
        <StatTile
          label="Recent failures"
          value={summaryQ.data?.recent_fail_count ?? "—"}
          sub="last 7 days"
          color="#d73838"
        />
      </div>

      {/* Twins running right now */}
      {twinsQ.data && twinsQ.data.length > 0 && (
        <>
          <div className="section-title">Running twins</div>
          <div className="card-tight mb-6">
            <table className="ck-table">
              <thead>
                <tr>
                  <th />
                  <th>Twin</th>
                  <th>URL</th>
                  <th>MCP URL</th>
                  <th>Started</th>
                  <th>PID</th>
                </tr>
              </thead>
              <tbody>
                {twinsQ.data.map((t) => (
                  <tr key={t.id}>
                    <td className="w-4">
                      <span className="inline-block w-2 h-2 bg-accent border border-ink animate-blip" />
                    </td>
                    <td>
                      <strong>{t.id}</strong>
                    </td>
                    <td className="font-mono text-xs">{t.url}</td>
                    <td className="font-mono text-xs">{t.mcp_url}</td>
                    <td className="text-xs">{fmtTimestamp(t.started_at)}</td>
                    <td className="font-mono text-xs">{t.pid}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Filter form */}
      <form
        className="flex gap-2 items-center mb-6 flex-wrap"
        onSubmit={(e) => {
          e.preventDefault();
          const f = e.currentTarget.elements;
          const sv = (f.namedItem("scenario") as HTMLInputElement).value;
          const av = (f.namedItem("agent") as HTMLInputElement).value;
          const p = new URLSearchParams(params);
          if (sv) p.set("scenario", sv); else p.delete("scenario");
          if (av) p.set("agent", av); else p.delete("agent");
          p.delete("page");
          setParams(p);
        }}
      >
        <input
          name="scenario"
          type="search"
          className="input flex-1 min-w-[180px] max-w-xs"
          placeholder="scenario substring  ( / )"
          defaultValue={scenario}
        />
        <input
          name="agent"
          type="search"
          className="input min-w-[160px]"
          placeholder="agent substring"
          defaultValue={agent}
        />
        <button type="submit" className="btn">Filter</button>
        {(scenario || agent || mode) && (
          <button
            type="button"
            className="btn-outline"
            onClick={() => setParams({})}
          >
            Clear
          </button>
        )}
      </form>

      {/* Runs table */}
      {runsQ.isLoading && <Loading />}
      {runsQ.error && <ErrorBox error={runsQ.error} />}
      {runsQ.data && (
        <>
          <div className="card-tight">
            <table className="ck-table">
              <thead>
                <tr>
                  <th className="!w-8" title="Pick 2 to compare">⊕</th>
                  <th>Run</th>
                  <th>Scenario</th>
                  <th>Agent</th>
                  <th>Score</th>
                  <th>Criteria</th>
                  <th>Duration</th>
                  <th>Timestamp</th>
                </tr>
              </thead>
              <tbody>
                {runsQ.data.rows.length === 0 && (
                  <tr>
                    <td colSpan={8}>
                      <EmptyState
                        title="No runs yet"
                        hint={
                          <>
                            Click <strong>New run</strong> above, or run <code className="font-mono">checkpoint run</code> from the CLI.
                          </>
                        }
                      />
                    </td>
                  </tr>
                )}
                {runsQ.data.rows.map((r) => {
                  const checked = picks.includes(r.run_id);
                  return (
                    <tr
                      key={r.run_id}
                      className="row-link"
                      onClick={(e) => {
                        if ((e.target as HTMLElement).closest("input")) return;
                        navigate(`/runs/${r.run_id}`);
                      }}
                    >
                      <td className="w-8">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => comparePicks.toggle(r.run_id)}
                          aria-label={`pick run ${shortId(r.run_id)} to compare`}
                        />
                      </td>
                      <td className="font-mono text-xs">{shortId(r.run_id)}</td>
                      <td>{r.scenario || "—"}</td>
                      <td>
                        {r.harness_name ? (
                          <span className="font-mono text-xs">{r.harness_name}</span>
                        ) : (
                          <span className="text-ink-4 text-xs italic">unknown</span>
                        )}
                      </td>
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
                  );
                })}
              </tbody>
            </table>
          </div>

          <Pager
            page={page}
            perPage={runsQ.data.per_page}
            total={runsQ.data.total}
            onPage={(p) => {
              const next = new URLSearchParams(params);
              if (p === 1) next.delete("page");
              else next.set("page", String(p));
              setParams(next);
            }}
          />
        </>
      )}

      {openLauncher && (
        <RunLauncher
          scenarios={scenariosQ.data?.scenarios || []}
          onClose={() => setOpenLauncher(false)}
        />
      )}
    </>
  );
}

function Pager({
  page,
  perPage,
  total,
  onPage,
}: {
  page: number;
  perPage: number;
  total: number;
  onPage: (p: number) => void;
}) {
  if (total <= perPage) return null;
  const start = (page - 1) * perPage + 1;
  const end = Math.min(page * perPage, total);
  return (
    <div className="flex justify-between items-center mt-4 text-ink-3 dark:text-paper-3 text-xs">
      <div>
        Showing {start}–{end} of {total}
      </div>
      <div className="flex gap-1.5">
        {page > 1 && (
          <button
            className="px-2.5 py-1 border border-ink-5 hover:border-ink hover:bg-paper-2 dark:hover:bg-ink"
            onClick={() => onPage(page - 1)}
          >
            ‹ prev
          </button>
        )}
        {page * perPage < total && (
          <button
            className="px-2.5 py-1 border border-ink-5 hover:border-ink hover:bg-paper-2 dark:hover:bg-ink"
            onClick={() => onPage(page + 1)}
          >
            next ›
          </button>
        )}
      </div>
    </div>
  );
}

function RunLauncher({
  scenarios,
  onClose,
}: {
  scenarios: { title: string; path: string }[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [scenario, setScenario] = useState(scenarios[0]?.path || "");
  const [runs, setRuns] = useState(1);

  // What runs is [agent] in checkpoint.toml — the same command the CLI and CI
  // use, so a run started here and a run started there are the same run.
  const configQ = useQuery({
    queryKey: ["config"],
    queryFn: api.config,
    staleTime: 30_000,
  });
  const agentCommand = String(
    configQ.data?.sections?.agent?.command ??
      configQ.data?.sections?.agent?.url ??
      "",
  );

  const startMut = useMutation({
    mutationFn: () => api.jobs.start(scenario, { runs }),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/live/${job.job_id}`);
    },
  });

  return (
    <div
      className="fixed inset-0 z-[200] bg-ink/40 backdrop-blur-sm flex items-center justify-center"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md bg-paper dark:bg-ink-2 border border-ink dark:border-paper-3 shadow-offset"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-ink dark:border-paper-3 px-5 py-3 flex items-center justify-between">
          <h2 className="font-bold">New run</h2>
          <button onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="p-5 space-y-4">
          <label className="block">
            <div className="card-title">Scenario</div>
            <select
              className="input w-full"
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
            >
              {scenarios.length === 0 && <option value="">No scenarios found</option>}
              {scenarios.map((s) => (
                <option key={s.path} value={s.path}>
                  {s.title} — {s.path}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <div className="card-title">Runs</div>
            <input
              className="input w-full"
              type="number"
              min={1}
              max={100}
              value={runs}
              onChange={(e) => setRuns(Math.max(1, Number(e.target.value) || 1))}
            />
            <div className="text-xs text-ink-3 dark:text-paper-3 mt-1">
              One run tells you what happened once. For a pass rate you can act
              on, use <code className="font-mono">checkpoint gate</code>.
            </div>
          </label>

          <div>
            <div className="card-title">Agent</div>
            {agentCommand ? (
              <code className="font-mono text-xs break-all">{agentCommand}</code>
            ) : (
              <div className="text-xs text-ink-3 dark:text-paper-3">
                No <code className="font-mono">[agent]</code> in checkpoint.toml —
                the run will stop and tell you how to set one.
              </div>
            )}
            <div className="text-xs text-ink-3 dark:text-paper-3 mt-1">
              From checkpoint.toml.{" "}
              <Link to="/setup?tab=config" className="underline">
                See the config
              </Link>
              .
            </div>
          </div>

          {startMut.isError && <ErrorBox error={startMut.error} />}
        </div>
        <div className="border-t border-paper-3 dark:border-ink-3 px-5 py-3 flex justify-end gap-2">
          <button onClick={onClose} className="btn-outline">
            Cancel
          </button>
          <button
            type="button"
            className="btn-accent"
            disabled={!scenario || startMut.isPending}
            onClick={() => startMut.mutate()}
          >
            <Play size={14} />
            {startMut.isPending ? "Starting…" : "Start run"}
          </button>
        </div>
      </div>
    </div>
  );
}
