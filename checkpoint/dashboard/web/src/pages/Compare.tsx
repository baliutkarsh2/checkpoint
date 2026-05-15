import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type CompareDiff } from "@/lib/api";
import { fmtTimestamp, scoreColor, shortId } from "@/lib/format";
import { Badge, ErrorBox, Loading, PageHead } from "@/components/bits";

export default function Compare() {
  const [params] = useSearchParams();
  const a = params.get("a") || "";
  const b = params.get("b") || "";

  const q = useQuery({
    queryKey: ["compare", a, b],
    queryFn: () => api.compare(a, b),
    enabled: Boolean(a && b),
  });

  if (!a || !b)
    return (
      <ErrorBox
        error={
          new Error(
            "Provide ?a=<run_id>&b=<run_id> in the URL, or pick 2 runs from the runs table.",
          )
        }
      />
    );
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const { rec_a, rec_b, diff } = q.data;

  return (
    <>
      <PageHead
        title="Compare runs"
        sub={
          <>
            Baseline <code className="font-mono">{shortId(rec_a.run_id)}</code>{" "}
            vs candidate <code className="font-mono">{shortId(rec_b.run_id)}</code>
          </>
        }
      />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
        <PaneCard
          label="Baseline"
          score={diff.baseline_score}
          scenario={rec_a.scenario || "—"}
          ts={(rec_a.env as { timestamp?: string } | null)?.timestamp || null}
        />
        <PaneCard
          label="Candidate"
          score={diff.candidate_score}
          scenario={rec_b.scenario || "—"}
          ts={(rec_b.env as { timestamp?: string } | null)?.timestamp || null}
        />
      </div>

      <DeltaCard delta={diff.delta} />

      <DiffSection title="Regressions" items={diff.regressions} variant="fail" />
      <DiffSection title="Fixes" items={diff.fixes} variant="pass" />
      <DiffSection title="Added" items={diff.added} variant="info" />
      <DiffSection title="Removed" items={diff.removed} variant="warn" />

      {diff.regressions.length === 0 &&
        diff.fixes.length === 0 &&
        diff.added.length === 0 &&
        diff.removed.length === 0 && (
          <div className="card text-center text-ink-3 dark:text-paper-3">
            No criterion-level changes between these runs.
          </div>
        )}
    </>
  );
}

function PaneCard({
  label,
  score,
  scenario,
  ts,
}: {
  label: string;
  score: number;
  scenario: string;
  ts: string | null;
}) {
  return (
    <div className="card">
      <div className="card-title">{label}</div>
      <div className="stat-num" style={{ color: scoreColor(score) }}>
        {Math.round(score)}
      </div>
      <div className="stat-sub">
        {scenario} · {fmtTimestamp(ts)}
      </div>
    </div>
  );
}

function DeltaCard({ delta }: { delta: number }) {
  const cls = delta > 0 ? "text-pass" : delta < 0 ? "text-fail" : "text-ink-3";
  const sign = delta > 0 ? "+" : "";
  return (
    <div className="card text-center mb-8">
      <div className="card-title">Delta</div>
      <div className={`text-5xl font-mono font-semibold ${cls}`}>
        {sign}
        {delta}
      </div>
    </div>
  );
}

function DiffSection({
  title,
  items,
  variant,
}: {
  title: string;
  items: CompareDiff["regressions"];
  variant: "pass" | "fail" | "warn" | "info";
}) {
  if (items.length === 0) return null;
  const colorVar =
    variant === "pass"
      ? "var(--c-pass, #0ea83b)"
      : variant === "fail"
        ? "#d73838"
        : variant === "warn"
          ? "#c89124"
          : "#2a5fb8";
  const labels: Record<string, { v: "pass" | "fail" | "warn" | "info"; t: string }> = {
    regressed: { v: "fail", t: "pass → fail" },
    fixed: { v: "pass", t: "fail → pass" },
    added: { v: "info", t: "added" },
    removed: { v: "warn", t: "removed" },
  };
  return (
    <>
      <div className="section-title" style={{ color: colorVar }}>
        {title} ({items.length})
      </div>
      <div className="card-tight mb-6">
        <table className="ck-table">
          <tbody>
            {items.map((d, i) => {
              const meta = labels[d.change] || { v: "d" as const, t: d.change };
              return (
                <tr key={i}>
                  <td className="w-32">
                    <Badge variant={meta.v}>{meta.t}</Badge>
                  </td>
                  <td>{d.text}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
