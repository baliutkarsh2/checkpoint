import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, X } from "lucide-react";
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
import { comparePicks, useComparePicks } from "@/lib/store";

export default function Runs() {
  const [params, setParams] = useSearchParams();
  const scenario = params.get("scenario") || "";
  const page = Number(params.get("page") || 1);
  const navigate = useNavigate();

  const summaryQ = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const clonesQ = useQuery({ queryKey: ["clones"], queryFn: api.clones });
  const runsQ = useQuery({
    queryKey: ["runs", { scenario, page }],
    queryFn: () => api.runs({ scenario: scenario || undefined, page }),
  });
  const scenariosQ = useQuery({ queryKey: ["scenarios"], queryFn: () => api.scenarios() });

  const picks = useComparePicks();
  const [openLauncher, setOpenLauncher] = useState(false);

  return (
    <>
      <PageHead
        title="Run history"
        sub={
          runsQ.data
            ? `${runsQ.data.total} total run${runsQ.data.total === 1 ? "" : "s"}${
                scenario ? `, filtered to "${scenario}"` : ""
              }`
            : "Loading…"
        }
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

      {/* Live clones */}
      {clonesQ.data && clonesQ.data.length > 0 && (
        <>
          <div className="section-title">Live clones</div>
          <div className="card-tight mb-6">
            <table className="ck-table">
              <thead>
                <tr>
                  <th />
                  <th>Clone</th>
                  <th>URL</th>
                  <th>MCP URL</th>
                  <th>Started</th>
                  <th>PID</th>
                </tr>
              </thead>
              <tbody>
                {clonesQ.data.map((c) => (
                  <tr key={c.id}>
                    <td className="w-4">
                      <span className="inline-block w-2 h-2 bg-accent border border-ink animate-blip" />
                    </td>
                    <td>
                      <strong>{c.id}</strong>
                    </td>
                    <td className="font-mono text-xs">{c.url}</td>
                    <td className="font-mono text-xs">{c.mcp_url}</td>
                    <td className="text-xs">{fmtTimestamp(c.started_at)}</td>
                    <td className="font-mono text-xs">{c.pid}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Filter form */}
      <form
        className="flex gap-2 items-center mb-6"
        onSubmit={(e) => {
          e.preventDefault();
          const v = (e.currentTarget.elements.namedItem("scenario") as HTMLInputElement).value;
          const p = new URLSearchParams(params);
          if (v) p.set("scenario", v);
          else p.delete("scenario");
          p.delete("page");
          setParams(p);
        }}
      >
        <input
          name="scenario"
          type="search"
          className="input flex-1 max-w-md"
          placeholder="filter by scenario name…  ( / to focus )"
          defaultValue={scenario}
        />
        <button type="submit" className="btn">Filter</button>
        {scenario && (
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
                  <th>Run ID</th>
                  <th>Scenario</th>
                  <th>Score</th>
                  <th>Criteria</th>
                  <th>Model</th>
                  <th>Timestamp</th>
                </tr>
              </thead>
              <tbody>
                {runsQ.data.rows.length === 0 && (
                  <tr>
                    <td colSpan={7}>
                      <EmptyState
                        title="No runs yet"
                        hint={
                          <>
                            Run <code className="font-mono">checkpoint run scenarios/</code> or click <strong>New run</strong> above.
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
                        // Don't navigate if the click was on the checkbox.
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
                        <ScoreBar score={r.satisfaction} />
                      </td>
                      <td className="font-mono text-xs">
                        {r.criteria_pass}/{r.criteria_total}
                      </td>
                      <td className="font-mono text-xs">
                        {r.evaluator_model || "—"}
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
  const [docker, setDocker] = useState(false);
  const [harness, setHarness] = useState("");

  const startMut = useMutation({
    mutationFn: () => api.jobs.start(scenario, { docker, harness: harness || undefined }),
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

          <label className="flex items-center gap-3 text-sm">
            <input
              type="checkbox"
              checked={docker}
              onChange={(e) => setDocker(e.target.checked)}
            />
            Use docker mode
          </label>

          {docker && (
            <label className="block">
              <div className="card-title">Harness directory (optional)</div>
              <input
                className="input w-full"
                placeholder="harness/"
                value={harness}
                onChange={(e) => setHarness(e.target.value)}
              />
            </label>
          )}

          {startMut.isError && <ErrorBox error={startMut.error} />}
        </div>
        <div className="border-t border-paper-3 dark:border-ink-3 px-5 py-3 flex justify-end gap-2">
          <button onClick={onClose} className="btn-outline">
            Cancel
          </button>
          <Link to="/" className="btn-ghost text-xs">
            Watch <span className="kbd">jobs</span>
          </Link>
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
