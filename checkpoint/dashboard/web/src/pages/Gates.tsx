import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Verdict } from "@/lib/api";
import { fmtTimestamp } from "@/lib/format";
import { EmptyState, ErrorBox, Loading, PageHead } from "@/components/bits";

/** The verdicts `checkpoint gate` has issued here, newest first. */
export default function Gates() {
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ["gates"], queryFn: () => api.gates() });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const rows = q.data.rows;

  return (
    <>
      <PageHead
        title="Gate"
        sub="Every scenario run N times, and one decision about whether the build ships."
      />

      {rows.length === 0 ? (
        <EmptyState
          title="No verdicts yet"
          hint={
            <>
              Run <code className="font-mono">checkpoint gate</code> — it runs
              every scenario enough times for the pass rate to mean something,
              then exits 0 only on SHIP. Each verdict it issues appears here.
            </>
          }
        />
      ) : (
        <div className="card-tight">
          <table className="ck-table">
            <thead>
              <tr>
                <th>Verdict</th>
                <th>Target</th>
                <th>Gate</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((g) => (
                <tr
                  key={g.gate_id}
                  className="row-link"
                  onClick={() => navigate(`/gates/${g.gate_id}`)}
                >
                  <td>
                    <VerdictBadge verdict={g.verdict} />
                  </td>
                  <td className="font-mono text-xs">{g.target || "—"}</td>
                  <td className="font-mono text-xs">
                    <Link to={`/gates/${g.gate_id}`} className="hover:underline">
                      {g.gate_id}
                    </Link>
                  </td>
                  <td className="text-xs">{fmtTimestamp(g.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-ink-3 dark:text-paper-3 mt-4 font-mono">
        store: {q.data.store}
      </p>
    </>
  );
}

/** SHIP reads green; everything else — including "cannot decide" — does not. */
export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const variant =
    verdict === "SHIP"
      ? "pass"
      : verdict === "BLOCK" || verdict === "ERROR"
        ? "fail"
        : "warn";
  return <span className={`badge badge-${variant}`}>{verdict}</span>;
}
