import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { scoreColor, truncateMid } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
  ScoreBar,
  Sparkline,
  StatTile,
} from "@/components/bits";

export default function Report() {
  const [params, setParams] = useSearchParams();
  const scenarioPattern = params.get("scenario") || "";

  const q = useQuery({
    queryKey: ["report", scenarioPattern],
    queryFn: () => api.report({ scenario: scenarioPattern || undefined }),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;
  const trend = q.data;
  const flaky = new Set(trend.flaky_criteria);

  const sortedCriteria = Object.entries(trend.criteria).sort(
    (a, b) => a[1].pass_rate - b[1].pass_rate,
  );

  return (
    <>
      <PageHead
        title="Trend report"
        sub={`${trend.run_count} runs aggregated${
          scenarioPattern ? ` for "${scenarioPattern}"` : ""
        }`}
      />

      <form
        className="flex gap-2 items-center mb-7"
        onSubmit={(e) => {
          e.preventDefault();
          const v = (e.currentTarget.elements.namedItem("scenario") as HTMLInputElement)
            .value;
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
          placeholder="scenario name substring…"
          defaultValue={scenarioPattern}
        />
        <button type="submit" className="btn">Apply</button>
        {scenarioPattern && (
          <button
            type="button"
            className="btn-outline"
            onClick={() => setParams({})}
          >
            Clear
          </button>
        )}
      </form>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-8">
        <StatTile label="Runs" value={trend.run_count} />
        <StatTile
          label="Avg score"
          value={Math.round(trend.avg_score)}
          color={scoreColor(trend.avg_score)}
        />
        <StatTile
          label="Range"
          value={
            <span className="text-2xl">
              {Math.round(trend.min_score)}–{Math.round(trend.max_score)}
            </span>
          }
        />
        <StatTile
          label="Flaky criteria"
          value={trend.flaky_criteria.length}
          color={trend.flaky_criteria.length > 0 ? "#c89124" : "#0ea83b"}
        />
      </div>

      {trend.history.length > 0 && (
        <>
          <div className="section-title">Score history (oldest → newest)</div>
          <div className="card mb-6">
            <Sparkline data={trend.history.map((h) => h.score).reverse()} />
            <div className="text-xs text-ink-3 dark:text-paper-3 mt-2 font-mono">
              Hover bars for individual run scores.
            </div>
          </div>
        </>
      )}

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
