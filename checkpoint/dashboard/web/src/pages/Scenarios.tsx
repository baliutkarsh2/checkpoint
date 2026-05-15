import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Play, Search } from "lucide-react";
import { api } from "@/lib/api";
import { fmtTimestamp, scoreColor } from "@/lib/format";
import {
  EmptyState,
  ErrorBox,
  Loading,
  PageHead,
  StatTile,
} from "@/components/bits";

/**
 * Scenarios page — card grid that answers "what can I run, and how has it
 * been doing?" at a glance. Each card shows: title, prompt preview, clones
 * needed, criterion counts, run count + pass rate + last score, and a
 * one-click "Run now" that picks the first bundled agent and starts a job.
 */
export default function Scenarios() {
  const [params, setParams] = useSearchParams();
  const filter = (params.get("q") || "").toLowerCase();

  const q = useQuery({ queryKey: ["scenarios"], queryFn: () => api.scenarios() });
  // Pull recent runs so we can roll up per-scenario stats client-side.
  const runsQ = useQuery({
    queryKey: ["runs", "all-recent"],
    queryFn: () => api.runs({ per_page: 200 }),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const { scenarios, coverage } = q.data;
  const stats = rollupRuns(runsQ.data?.rows || []);
  const filtered = filter
    ? scenarios.filter(
        (s) =>
          s.title.toLowerCase().includes(filter) ||
          s.path.toLowerCase().includes(filter) ||
          s.clones.toLowerCase().includes(filter) ||
          s.tags.toLowerCase().includes(filter),
      )
    : scenarios;

  return (
    <>
      <PageHead
        title="Scenarios"
        sub={`${scenarios.length} scenario${scenarios.length === 1 ? "" : "s"}${
          filter ? ` · matching "${filter}"` : ""
        }`}
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Scenarios" value={scenarios.length} />
        <StatTile
          label="Total [D] criteria"
          value={coverage.total_d}
          sub={`${coverage.stage1_pct}% stage-1 hits`}
        />
        <StatTile
          label="Total [P] criteria"
          value={coverage.total_p}
          color="#7a4ec6"
          sub="LLM-judged"
        />
        <StatTile
          label="Runs across all scenarios"
          value={runsQ.data?.total ?? 0}
          sub="recent"
        />
      </div>

      <form
        className="flex gap-2 mb-6"
        onSubmit={(e) => {
          e.preventDefault();
          const v = (e.currentTarget.elements.namedItem("q") as HTMLInputElement).value;
          const p = new URLSearchParams(params);
          if (v) p.set("q", v); else p.delete("q");
          setParams(p);
        }}
      >
        <div className="flex items-center gap-2 input flex-1 max-w-md !pl-3">
          <Search size={14} className="text-ink-3" />
          <input
            name="q"
            type="search"
            className="bg-transparent outline-none flex-1 text-sm"
            placeholder="search by title, clone, or tag…"
            defaultValue={filter}
          />
        </div>
        <button type="submit" className="btn">Filter</button>
        {filter && (
          <button type="button" className="btn-outline" onClick={() => setParams({})}>
            Clear
          </button>
        )}
      </form>

      {filtered.length === 0 ? (
        <EmptyState
          title={scenarios.length === 0 ? "No scenarios found" : "No scenarios match"}
          hint={
            scenarios.length === 0
              ? "Pass --scenarios <dir> to checkpoint serve, or place .md files under your project."
              : "Try a different search term, or clear the filter."
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {filtered.map((s) => (
            <ScenarioCard key={s.path} scenario={s} stats={stats[s.title]} />
          ))}
        </div>
      )}
    </>
  );
}

function ScenarioCard({
  scenario: s,
  stats,
}: {
  scenario: {
    title: string;
    path: string;
    clones: string;
    tags: string;
    d_count: number;
    p_count: number;
    coverage_pct: number;
  };
  stats?: { runs: number; avg_score: number; last_score: number | null; last_at: string | null };
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const detailUrl = `/scenarios/file?path=${encodeURIComponent(s.path)}`;
  const runsUrl = `/?scenario=${encodeURIComponent(s.title)}`;

  // Auto-pick the first bundled agent for the one-click "Run now" button.
  const agentsQ = useQuery({ queryKey: ["agents"], queryFn: api.agents, staleTime: 30_000 });
  const defaultAgent = agentsQ.data?.find((a) => a.source === "bundled") || agentsQ.data?.[0];

  const startMut = useMutation({
    mutationFn: () =>
      api.jobs.start(s.path, {
        docker: true,
        harness: defaultAgent?.path,
      }),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      navigate(`/live/${job.job_id}`);
    },
  });

  return (
    <div className="card flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <Link
            to={detailUrl}
            className="block font-bold text-base hover:underline truncate"
          >
            {s.title || "(untitled)"}
          </Link>
          <div className="text-xs font-mono text-ink-4 dark:text-paper-3 truncate mt-0.5">
            {s.path}
          </div>
        </div>
        <button
          type="button"
          className="btn-accent !h-8 !text-xs"
          onClick={() => startMut.mutate()}
          disabled={startMut.isPending || !defaultAgent}
          title={defaultAgent ? `Run with ${defaultAgent.name}` : "No agents discovered"}
        >
          <Play size={12} />
          {startMut.isPending ? "Starting…" : "Run"}
        </button>
      </div>

      <div className="flex flex-wrap gap-1.5 text-[10px] font-mono uppercase tracking-wider">
        {s.clones &&
          s.clones.split(",").map((c) => (
            <span key={c} className="badge badge-info">
              {c.trim()}
            </span>
          ))}
        {s.tags &&
          s.tags.split(",").map((t) => (
            <span key={t} className="badge">
              {t.trim()}
            </span>
          ))}
      </div>

      <div className="grid grid-cols-3 gap-2 pt-2 border-t border-paper-3 dark:border-ink-3">
        <Stat label="[D]" value={s.d_count} />
        <Stat label="[P]" value={s.p_count} color="#7a4ec6" />
        <Stat label="Coverage" value={`${s.coverage_pct}%`} color={scoreColor(s.coverage_pct)} />
      </div>

      <div className="grid grid-cols-3 gap-2 pt-2 border-t border-paper-3 dark:border-ink-3">
        <Stat label="Runs" value={stats?.runs ?? 0} />
        <Stat
          label="Avg score"
          value={stats?.runs ? stats.avg_score.toFixed(0) : "—"}
          color={stats?.runs ? scoreColor(stats.avg_score) : undefined}
        />
        <Stat
          label="Last"
          value={
            stats?.last_score !== null && stats?.last_score !== undefined
              ? stats.last_score.toFixed(0)
              : "—"
          }
          sub={stats?.last_at ? fmtTimestamp(stats.last_at) : ""}
          color={
            stats?.last_score !== null && stats?.last_score !== undefined
              ? scoreColor(stats.last_score)
              : undefined
          }
        />
      </div>

      {startMut.isError && <ErrorBox error={startMut.error} />}

      <div className="flex justify-between items-center pt-2 border-t border-paper-3 dark:border-ink-3 text-xs">
        <Link to={detailUrl} className="text-ink-3 hover:text-ink dark:text-paper-3 hover:underline">
          View scenario →
        </Link>
        {stats?.runs ? (
          <Link to={runsUrl} className="text-ink-3 hover:text-ink dark:text-paper-3 hover:underline">
            View {stats.runs} run{stats.runs === 1 ? "" : "s"} →
          </Link>
        ) : null}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: number | string;
  sub?: string;
  color?: string;
}) {
  return (
    <div>
      <div className="text-[10px] font-mono uppercase text-ink-4 dark:text-paper-3 tracking-wider">
        {label}
      </div>
      <div
        className="font-mono text-base font-semibold"
        style={color ? { color } : undefined}
      >
        {value}
      </div>
      {sub && (
        <div className="text-[10px] text-ink-4 dark:text-paper-3 truncate">{sub}</div>
      )}
    </div>
  );
}

function rollupRuns(
  rows: { scenario: string | null; satisfaction: number; timestamp: string | null }[],
): Record<string, { runs: number; avg_score: number; last_score: number; last_at: string | null }> {
  const out: Record<
    string,
    { runs: number; sum: number; last_score: number; last_at: string | null }
  > = {};
  for (const r of rows) {
    const key = r.scenario || "";
    if (!key) continue;
    if (!out[key]) {
      out[key] = { runs: 0, sum: 0, last_score: r.satisfaction, last_at: r.timestamp };
    }
    out[key].runs += 1;
    out[key].sum += r.satisfaction;
  }
  const final: Record<string, { runs: number; avg_score: number; last_score: number; last_at: string | null }> = {};
  for (const [k, v] of Object.entries(out)) {
    final[k] = {
      runs: v.runs,
      avg_score: v.runs ? v.sum / v.runs : 0,
      last_score: v.last_score,
      last_at: v.last_at,
    };
  }
  return final;
}
