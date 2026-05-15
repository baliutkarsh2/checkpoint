import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type RunSummary } from "@/lib/api";
import { fmtTimestamp, scoreColor, truncateMid } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
  ScoreBar,
  Sparkline,
  StatTile,
} from "@/components/bits";

/**
 * Report page — designed to answer 4 questions at a glance:
 *   1. How is everything doing?               (top stat tiles)
 *   2. Which scenarios are failing the most?  (per-scenario leaderboard)
 *   3. Which agents are best?                  (per-agent leaderboard)
 *   4. Which criteria are flaky?               (criterion table)
 *
 * The legacy /api/report endpoint already gives us the criterion + history
 * trend. We layer per-scenario / per-agent leaderboards on top using the
 * recent-runs feed.
 */
export default function Report() {
  const [params, setParams] = useSearchParams();
  const scenarioPattern = params.get("scenario") || "";

  const trendQ = useQuery({
    queryKey: ["report", scenarioPattern],
    queryFn: () => api.report({ scenario: scenarioPattern || undefined }),
  });
  const summaryQ = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const allRunsQ = useQuery({
    queryKey: ["runs", "report-rollup"],
    queryFn: () => api.runs({ per_page: 200 }),
  });

  if (trendQ.isLoading) return <Loading />;
  if (trendQ.error) return <ErrorBox error={trendQ.error} />;
  if (!trendQ.data) return null;
  const trend = trendQ.data;
  const flaky = new Set(trend.flaky_criteria);

  const allRuns = allRunsQ.data?.rows || [];
  const byScenario = rollupBy(allRuns, (r) => r.scenario);
  const byAgent = rollupBy(allRuns, (r) => r.harness_name);

  const sortedCriteria = Object.entries(trend.criteria).sort(
    (a, b) => a[1].pass_rate - b[1].pass_rate,
  );

  return (
    <>
      <PageHead
        title="Report"
        sub={
          scenarioPattern
            ? `Filtered to scenarios matching "${scenarioPattern}"`
            : "Aggregate health across all scenarios + agents"
        }
      />

      {/* Top tiles */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile
          label="Total runs (all-time)"
          value={summaryQ.data?.total_runs ?? "—"}
        />
        <StatTile
          label="Avg score · 30d"
          value={summaryQ.data?.avg_score_30d ?? "—"}
          color={summaryQ.data ? scoreColor(summaryQ.data.avg_score_30d) : undefined}
        />
        <StatTile
          label="Pass rate · 30d"
          value={summaryQ.data ? `${summaryQ.data.pass_rate_30d}%` : "—"}
        />
        <StatTile
          label="Recent failures · 7d"
          value={summaryQ.data?.recent_fail_count ?? "—"}
          color="#d73838"
        />
      </div>

      {/* Filter */}
      <form
        className="flex gap-2 items-center mb-7"
        onSubmit={(e) => {
          e.preventDefault();
          const v = (e.currentTarget.elements.namedItem("scenario") as HTMLInputElement).value;
          const next = new URLSearchParams(params);
          if (v) next.set("scenario", v);
          else next.delete("scenario");
          setParams(next);
        }}
      >
        <input
          name="scenario"
          type="search"
          className="input flex-1 max-w-md"
          placeholder="filter scenario substring  ( / )"
          defaultValue={scenarioPattern}
        />
        <button type="submit" className="btn">Apply</button>
        {scenarioPattern && (
          <button type="button" className="btn-outline" onClick={() => setParams({})}>
            Clear
          </button>
        )}
      </form>

      {/* Score history */}
      {trend.history.length > 0 && (
        <>
          <div className="section-title">Score history (oldest → newest)</div>
          <div className="card mb-7">
            <Sparkline data={trend.history.map((h) => h.score).reverse()} />
            <div className="text-xs text-ink-3 dark:text-paper-3 mt-2 font-mono">
              {trend.run_count} runs · avg {trend.avg_score} · range{" "}
              {trend.min_score}–{trend.max_score}. Hover bars for individual scores.
            </div>
          </div>
        </>
      )}

      {/* Two leaderboards side by side */}
      <div className="grid lg:grid-cols-2 gap-6 mb-7">
        <Leaderboard
          title="Scenarios — by pass rate (worst first)"
          rows={byScenario}
          emptyHint="Run some scenarios first."
          link={(name) => `/?scenario=${encodeURIComponent(name)}`}
        />
        <Leaderboard
          title="Agents — by pass rate (worst first)"
          rows={byAgent}
          emptyHint="Run some scenarios with an agent first."
          link={(name) => `/?agent=${encodeURIComponent(name)}`}
        />
      </div>

      {/* Per-criterion table */}
      <div className="section-title">Criteria pass rates</div>
      <div className="card-tight">
        <table className="ck-table">
          <thead>
            <tr>
              <th className="!w-10">Kind</th>
              <th>Criterion</th>
              <th className="!w-16">Runs</th>
              <th className="!w-36">Pass rate</th>
              <th className="!w-24">Status</th>
            </tr>
          </thead>
          <tbody>
            {sortedCriteria.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center py-6 text-ink-3 dark:text-paper-3">
                  No criteria data yet.
                </td>
              </tr>
            )}
            {sortedCriteria.map(([text, s]) => {
              const ratePct = Math.round(s.pass_rate * 100);
              const isFlaky = flaky.has(text);
              return (
                <tr key={text}>
                  <td>
                    <Badge variant={s.kind === "P" ? "judge" : "d"}>{s.kind}</Badge>
                  </td>
                  <td>{truncateMid(text, 80)}</td>
                  <td className="font-mono text-xs">{s.total}</td>
                  <td>
                    <ScoreBar score={ratePct} />
                  </td>
                  <td>
                    {isFlaky ? (
                      <Badge variant="warn">flaky</Badge>
                    ) : s.pass_rate >= 0.95 ? (
                      <Badge variant="pass">stable</Badge>
                    ) : s.pass_rate < 0.5 ? (
                      <Badge variant="fail">failing</Badge>
                    ) : (
                      <Badge>—</Badge>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

interface RollupRow {
  key: string;
  runs: number;
  pass_rate: number;
  avg_score: number;
  last_at: string | null;
}

function rollupBy(
  rows: RunSummary[],
  keyFn: (r: RunSummary) => string | null | undefined,
): RollupRow[] {
  const buckets: Record<string, { runs: number; sum: number; pass: number; last_at: string | null }> = {};
  for (const r of rows) {
    const k = keyFn(r);
    if (!k) continue;
    if (!buckets[k]) buckets[k] = { runs: 0, sum: 0, pass: 0, last_at: r.timestamp };
    buckets[k].runs += 1;
    buckets[k].sum += r.satisfaction;
    if (r.satisfaction >= 100) buckets[k].pass += 1;
  }
  return Object.entries(buckets)
    .map(([k, v]) => ({
      key: k,
      runs: v.runs,
      avg_score: v.runs ? v.sum / v.runs : 0,
      pass_rate: v.runs ? (100 * v.pass) / v.runs : 0,
      last_at: v.last_at,
    }))
    .sort((a, b) => a.pass_rate - b.pass_rate || b.runs - a.runs);
}

function Leaderboard({
  title,
  rows,
  emptyHint,
  link,
}: {
  title: string;
  rows: RollupRow[];
  emptyHint: string;
  link: (key: string) => string;
}) {
  return (
    <div>
      <div className="section-title">{title}</div>
      <div className="card-tight">
        {rows.length === 0 ? (
          <div className="p-5 text-center text-ink-3 dark:text-paper-3 text-sm">{emptyHint}</div>
        ) : (
          <table className="ck-table">
            <thead>
              <tr>
                <th>Name</th>
                <th className="!w-14">Runs</th>
                <th className="!w-32">Pass rate</th>
                <th className="!w-24">Avg</th>
                <th>Last</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 10).map((r) => (
                <tr key={r.key} className="row-link">
                  <td>
                    <Link to={link(r.key)} className="hover:underline">
                      {r.key}
                    </Link>
                  </td>
                  <td className="font-mono text-xs">{r.runs}</td>
                  <td>
                    <ScoreBar score={r.pass_rate} />
                  </td>
                  <td className="font-mono text-xs" style={{ color: scoreColor(r.avg_score) }}>
                    {r.avg_score.toFixed(0)}
                  </td>
                  <td className="text-xs">{fmtTimestamp(r.last_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
