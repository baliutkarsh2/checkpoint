import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, X, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { ErrorBox, Loading, PageHead, StatTile } from "@/components/bits";

/** Dashboard equivalent of `checkpoint doctor` — environment readiness.
 *  Pass `headless` when embedding inside the Setup hub. */
export default function Doctor({ headless = false }: { headless?: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["doctor"], queryFn: api.doctor });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const d = q.data;
  const failed = d.checks.filter((c) => !c.ok);

  const refreshBtn = (
    <button
      type="button"
      className="btn-outline"
      onClick={() => qc.invalidateQueries({ queryKey: ["doctor"] })}
    >
      <RefreshCw size={14} /> Re-run
    </button>
  );

  return (
    <>
      {!headless ? (
        <PageHead
          title="Doctor"
          sub="Environment readiness — same as `checkpoint doctor` on the CLI."
          right={refreshBtn}
        />
      ) : (
        <div className="flex justify-end mb-4">{refreshBtn}</div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Total checks" value={d.checks.length} />
        <StatTile
          label="Passing"
          value={d.checks.length - failed.length}
          color="#0ea83b"
        />
        <StatTile
          label="Failing"
          value={failed.length}
          color={failed.length > 0 ? "#d73838" : undefined}
        />
        <StatTile
          label="Overall"
          value={d.all_passed ? "ready" : "not ready"}
          color={d.all_passed ? "#0ea83b" : "#d73838"}
        />
      </div>

      <div className="card-tight">
        <table className="ck-table">
          <thead>
            <tr>
              <th className="!w-16">Status</th>
              <th>Check</th>
              <th>Detail</th>
              <th>Fix</th>
            </tr>
          </thead>
          <tbody>
            {d.checks.map((c, i) => (
              <tr key={i}>
                <td>
                  {c.ok ? (
                    <span className="inline-flex items-center gap-1 text-pass">
                      <Check size={14} /> ok
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-fail">
                      <X size={14} /> fail
                    </span>
                  )}
                </td>
                <td className="font-medium">{c.name}</td>
                <td className="text-sm text-ink-3 dark:text-paper-3">{c.detail}</td>
                <td className="text-xs text-ink-3 dark:text-paper-3 italic">
                  {c.fix || ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
