import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download } from "lucide-react";
import { api } from "@/lib/api";
import { fmtTimestamp, scoreColor, stableStringify } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
  StatTile,
} from "@/components/bits";
import TraceTimeline from "@/components/TraceTimeline";

export default function RunDetail() {
  const { runId = "" } = useParams();
  const q = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: Boolean(runId),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const r = q.data;
  const sat = Math.round(r.satisfaction);
  const passed = r.criteria.filter((c) => c.passed).length;

  return (
    <>
      <Link
        to="/"
        className="text-xs text-ink-3 dark:text-paper-3 inline-flex items-center gap-1 hover:underline"
      >
        <ArrowLeft size={12} /> back to runs
      </Link>

      <PageHead
        title={r.scenario || "(inline task)"}
        sub={
          <>
            Run <code className="font-mono">{r.run_id}</code> ·{" "}
            {fmtTimestamp((r.env as { timestamp?: string } | null)?.timestamp)}
          </>
        }
        right={
          <a href={`/api/runs/${r.run_id}`} className="btn-outline" download>
            <Download size={14} /> Download JSON
          </a>
        }
      />

      {/* Identity strip — what was tested, with what, in what mode */}
      <div className="card mb-5">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
          <Identity
            label="Scenario"
            value={r.scenario || "(inline)"}
            sub={r.scenario_path}
            link={
              r.scenario_path
                ? `/scenarios/file?path=${encodeURIComponent(r.scenario_path)}`
                : undefined
            }
          />
          <Identity
            label="Agent"
            value={r.harness?.name || "unknown"}
            sub={r.harness?.dir || r.harness?.cmd || ""}
          />
          <Identity
            label="Mode"
            value={r.harness?.mode || "—"}
          />
          <Identity
            label="Judge model"
            value={r.evaluator_model || "—"}
          />
        </div>
      </div>

      {/* Score header */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-8">
        <StatTile label="Satisfaction" value={`${sat} / 100`} color={scoreColor(sat)} />
        <StatTile
          label="Criteria"
          value={
            <>
              <span style={{ color: "#0ea83b" }}>{passed}</span>
              <span className="text-ink-4 text-lg"> / </span>
              <span>{r.criteria.length}</span>
            </>
          }
        />
        <StatTile
          label="Duration"
          value={r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}
        />
        {r.exit_code !== null && r.exit_code !== undefined && (
          <StatTile
            label="Exit code"
            value={r.exit_code}
            color={r.exit_code === 0 ? "#0ea83b" : "#d73838"}
          />
        )}
      </div>

      {/* Criteria */}
      <div className="section-title">Criteria</div>
      <div className="card-tight mb-6">
        <table className="ck-table">
          <thead>
            <tr>
              <th className="!w-10">Kind</th>
              <th>Criterion</th>
              <th className="!w-24">Result</th>
              <th className="!w-32">Evaluator</th>
              <th>Reasoning</th>
            </tr>
          </thead>
          <tbody>
            {r.criteria.map((c, i) => (
              <tr key={i}>
                <td>
                  <Badge variant={c.kind === "P" ? "judge" : "d"}>{c.kind}</Badge>
                </td>
                <td>{c.text}</td>
                <td>
                  <Badge variant={c.passed ? "pass" : "fail"}>
                    {c.passed ? "pass" : "fail"}
                  </Badge>
                </td>
                <td className="font-mono text-xs">{c.evaluator || "—"}</td>
                <td className="text-ink-3 dark:text-paper-3 text-xs">
                  {c.reasoning || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Failure analysis */}
      {r.failure_analysis && Object.keys(r.failure_analysis).length > 0 && (
        <>
          <div className="section-title">Failure analysis</div>
          {Object.entries(r.failure_analysis).map(([crit, why]) => (
            <div className="card mb-4" key={crit}>
              <div className="card-title">Why this failed</div>
              <strong className="block mb-2">{crit}</strong>
              <p className="text-ink-2 dark:text-paper-2 whitespace-pre-wrap text-sm">
                {why}
              </p>
            </div>
          ))}
        </>
      )}

      {/* Final answer */}
      {r.final_answer && (
        <details className="card mb-4" open>
          <summary className="cursor-pointer font-medium">Final answer</summary>
          <pre className="json mt-3">{r.final_answer}</pre>
        </details>
      )}

      {/* Trace */}
      <div className="section-title">API trace ({r.trace.length} events)</div>
      <TraceTimeline events={r.trace} />

      {/* State */}
      <details className="card mt-6">
        <summary className="cursor-pointer font-medium">Twin state</summary>
        <pre className="json mt-3">{stableStringify(r.state)}</pre>
      </details>
    </>
  );
}

function Identity({
  label,
  value,
  sub,
  link,
}: {
  label: string;
  value: string;
  sub?: string | null;
  link?: string;
}) {
  const body = (
    <>
      <div className="text-[10px] uppercase font-mono tracking-wider text-ink-3 dark:text-paper-3">
        {label}
      </div>
      <div className="font-medium text-base mt-0.5 truncate">{value}</div>
      {sub && (
        <div className="text-[11px] font-mono text-ink-4 dark:text-paper-3 truncate mt-0.5">
          {sub}
        </div>
      )}
    </>
  );
  return link ? (
    <Link to={link} className="block hover:underline">
      {body}
    </Link>
  ) : (
    <div>{body}</div>
  );
}
