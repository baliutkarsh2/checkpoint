import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { scoreColor } from "@/lib/format";
import {
  EmptyState,
  ErrorBox,
  Loading,
  PageHead,
  ScoreBar,
  StatTile,
} from "@/components/bits";

export default function Scenarios() {
  const [params] = useSearchParams();
  const path = params.get("path") || undefined;
  const focus = params.get("focus") || "";

  const q = useQuery({
    queryKey: ["scenarios", path],
    queryFn: () => api.scenarios(path),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;
  const { scenarios, coverage } = q.data;

  return (
    <>
      <PageHead
        title="Scenarios"
        sub={`${scenarios.length} scenario${scenarios.length === 1 ? "" : "s"} discovered`}
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-8">
        <StatTile label="Total [D] criteria" value={coverage.total_d} />
        <StatTile
          label="Stage-1 hits"
          value={coverage.stage1_hits}
          sub={`${coverage.stage1_pct}% of [D]`}
          color={scoreColor(coverage.stage1_pct)}
        />
        <StatTile
          label="Total [P] criteria"
          value={coverage.total_p}
          sub="LLM-judged"
          color="#7a4ec6"
        />
        <StatTile label="Scenarios" value={scenarios.length} />
      </div>

      {scenarios.length === 0 ? (
        <EmptyState
          title="No scenarios found"
          hint="Pass --scenarios <dir> to checkpoint serve, or place .md files in your project."
        />
      ) : (
        <div className="card-tight">
          <table className="ck-table">
            <thead>
              <tr>
                <th>Title</th>
                <th>Clones</th>
                <th>Tags</th>
                <th className="!w-16">[D]</th>
                <th className="!w-16">[P]</th>
                <th className="!w-36">Stage-1 coverage</th>
                <th>Path</th>
              </tr>
            </thead>
            <tbody>
              {scenarios.map((s) => {
                const isFocused = focus === s.path;
                return (
                  <tr
                    key={s.path}
                    className={isFocused ? "bg-paper-2 dark:bg-ink" : ""}
                  >
                    <td>
                      <strong>{s.title || "(untitled)"}</strong>
                    </td>
                    <td className="font-mono text-xs">{s.clones || "—"}</td>
                    <td className="text-xs text-ink-3 dark:text-paper-3">
                      {s.tags || "—"}
                    </td>
                    <td className="font-mono text-xs">{s.d_count}</td>
                    <td className="font-mono text-xs">{s.p_count}</td>
                    <td>
                      <ScoreBar score={s.coverage_pct} />
                    </td>
                    <td className="font-mono text-[11px] text-ink-4 dark:text-paper-3">
                      {s.path}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
