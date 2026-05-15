import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { api, type AgentInfo, type RunSummary } from "@/lib/api";
import { fmtTimestamp, scoreColor } from "@/lib/format";
import {
  EmptyState,
  ErrorBox,
  Loading,
  PageHead,
  StatTile,
} from "@/components/bits";

/**
 * Agents page — card grid showing every harness directory the dashboard
 * auto-discovered, with a roll-up of the runs each one has produced.
 */
export default function Agents() {
  const [params, setParams] = useSearchParams();
  const filter = (params.get("q") || "").toLowerCase();

  const agentsQ = useQuery({ queryKey: ["agents"], queryFn: api.agents });
  const runsQ = useQuery({
    queryKey: ["runs", "all-recent"],
    queryFn: () => api.runs({ per_page: 200 }),
  });

  if (agentsQ.isLoading) return <Loading />;
  if (agentsQ.error) return <ErrorBox error={agentsQ.error} />;
  if (!agentsQ.data) return null;

  const agents = agentsQ.data;
  const stats = rollupByAgent(runsQ.data?.rows || []);
  const filtered = filter
    ? agents.filter(
        (a) =>
          a.name.toLowerCase().includes(filter) ||
          a.path.toLowerCase().includes(filter) ||
          a.description.toLowerCase().includes(filter),
      )
    : agents;

  const totalRuns = Object.values(stats).reduce((acc, s) => acc + s.runs, 0);
  const avgPassRate =
    Object.values(stats).length > 0
      ? Object.values(stats).reduce((acc, s) => acc + s.pass_rate, 0) /
        Object.values(stats).length
      : 0;

  return (
    <>
      <PageHead
        title="Agents"
        sub={`${agents.length} agent${agents.length === 1 ? "" : "s"} discovered${
          filter ? ` · matching "${filter}"` : ""
        }`}
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Agents" value={agents.length} />
        <StatTile
          label="Bundled"
          value={agents.filter((a) => a.source === "bundled").length}
        />
        <StatTile
          label="Total runs across agents"
          value={totalRuns}
        />
        <StatTile
          label="Avg pass rate"
          value={totalRuns ? `${avgPassRate.toFixed(0)}%` : "—"}
          color={totalRuns ? scoreColor(avgPassRate) : undefined}
        />
      </div>

      <form
        className="flex gap-2 mb-6"
        onSubmit={(e) => {
          e.preventDefault();
          const v = (e.currentTarget.elements.namedItem("q") as HTMLInputElement).value;
          const p = new URLSearchParams(params);
          if (v) p.set("q", v);
          else p.delete("q");
          setParams(p);
        }}
      >
        <div className="flex items-center gap-2 input flex-1 max-w-md !pl-3">
          <Search size={14} className="text-ink-3" />
          <input
            name="q"
            type="search"
            className="bg-transparent outline-none flex-1 text-sm"
            placeholder="search by name or description…"
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
          title={agents.length === 0 ? "No agents discovered" : "No agents match"}
          hint={
            agents.length === 0
              ? "Add a directory under examples/agents/ with a Dockerfile + harness.py."
              : "Try a different search term."
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {filtered.map((a) => (
            <AgentCard key={a.id} agent={a} stats={stats[a.path]} />
          ))}
        </div>
      )}
    </>
  );
}

function AgentCard({
  agent: a,
  stats,
}: {
  agent: AgentInfo;
  stats?: { runs: number; avg_score: number; pass_rate: number; last_at: string | null };
}) {
  return (
    <div className="card flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <Link
            to={`/agents/${encodeURIComponent(a.id)}`}
            className="block font-bold text-base hover:underline truncate"
          >
            {a.name}
          </Link>
          <div className="text-xs font-mono text-ink-4 dark:text-paper-3 truncate mt-0.5">
            {a.path}
          </div>
        </div>
        <span
          className={
            "badge " +
            (a.source === "bundled"
              ? "badge-info"
              : a.source === "init"
                ? "badge-d"
                : "badge")
          }
        >
          {a.source}
        </span>
      </div>

      {a.description && (
        <p className="text-sm text-ink-3 dark:text-paper-3 line-clamp-2">{a.description}</p>
      )}

      <div className="grid grid-cols-3 gap-2 pt-2 border-t border-paper-3 dark:border-ink-3">
        <div>
          <div className="text-[10px] font-mono uppercase text-ink-4 dark:text-paper-3 tracking-wider">
            Runs
          </div>
          <div className="font-mono text-base font-semibold">{stats?.runs ?? 0}</div>
        </div>
        <div>
          <div className="text-[10px] font-mono uppercase text-ink-4 dark:text-paper-3 tracking-wider">
            Avg score
          </div>
          <div
            className="font-mono text-base font-semibold"
            style={stats?.runs ? { color: scoreColor(stats.avg_score) } : undefined}
          >
            {stats?.runs ? stats.avg_score.toFixed(0) : "—"}
          </div>
        </div>
        <div>
          <div className="text-[10px] font-mono uppercase text-ink-4 dark:text-paper-3 tracking-wider">
            Pass rate
          </div>
          <div
            className="font-mono text-base font-semibold"
            style={stats?.runs ? { color: scoreColor(stats.pass_rate) } : undefined}
          >
            {stats?.runs ? `${stats.pass_rate.toFixed(0)}%` : "—"}
          </div>
        </div>
      </div>

      <div className="flex justify-between items-center pt-2 border-t border-paper-3 dark:border-ink-3 text-xs">
        <Link
          to={`/agents/${encodeURIComponent(a.id)}`}
          className="text-ink-3 hover:text-ink dark:text-paper-3 hover:underline"
        >
          View detail →
        </Link>
        {stats?.last_at && (
          <span className="text-ink-4 dark:text-paper-3">
            last: {fmtTimestamp(stats.last_at)}
          </span>
        )}
      </div>
    </div>
  );
}

function rollupByAgent(
  rows: RunSummary[],
): Record<string, { runs: number; avg_score: number; pass_rate: number; last_at: string | null }> {
  const buckets: Record<string, { runs: number; sum: number; pass: number; last_at: string | null }> = {};
  for (const r of rows) {
    if (!r.harness_dir) continue;
    if (!buckets[r.harness_dir]) {
      buckets[r.harness_dir] = { runs: 0, sum: 0, pass: 0, last_at: r.timestamp };
    }
    buckets[r.harness_dir].runs += 1;
    buckets[r.harness_dir].sum += r.satisfaction;
    if (r.satisfaction >= 100) buckets[r.harness_dir].pass += 1;
  }
  const out: Record<string, { runs: number; avg_score: number; pass_rate: number; last_at: string | null }> = {};
  for (const [k, v] of Object.entries(buckets)) {
    out[k] = {
      runs: v.runs,
      avg_score: v.runs ? v.sum / v.runs : 0,
      pass_rate: v.runs ? (100 * v.pass) / v.runs : 0,
      last_at: v.last_at,
    };
  }
  return out;
}
