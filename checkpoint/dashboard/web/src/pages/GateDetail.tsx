import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api, type GateScenarioStat, type GateResult, type Verdict } from "@/lib/api";
import { fmtTimestamp } from "@/lib/format";
import { ErrorBox, Loading, PageHead, StatTile } from "@/components/bits";
import { VerdictBadge } from "./Gates";

/** What each verdict means, in the words the CLI uses for it. */
const MEANING: Record<Verdict, string> = {
  SHIP: "every scenario confidently passes",
  BLOCK: "a confident failure, a regression, or a scenario that failed every run",
  CONDITIONAL: "enough runs to decide, results genuinely mixed",
  INCONCLUSIVE: "too few runs for SHIP to be reachable",
  ERROR: "the sandbox, judge or scenarios broke — no verdict is possible",
};

const CLASS_VARIANT: Record<string, string> = {
  stable_pass: "pass",
  stable_fail: "fail",
  regression: "fail",
  flaky: "warn",
  inconclusive: "warn",
  error: "judge",
};

export default function GateDetail() {
  const { gateId = "" } = useParams();
  const q = useQuery({
    queryKey: ["gates", gateId],
    queryFn: () => api.gate(gateId),
    enabled: Boolean(gateId),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const g = q.data;
  const scenarios = g.scenarios || [];
  const skipped = g.skipped || [];
  const errors = g.errors || [];

  return (
    <>
      <Link
        to="/gates"
        className="text-xs text-ink-3 dark:text-paper-3 inline-flex items-center gap-1 hover:underline"
      >
        <ArrowLeft size={12} /> back to gate results
      </Link>

      <PageHead
        title="Gate result"
        sub={
          <>
            <code className="font-mono">{g.target || "the project's scenarios"}</code> ·{" "}
            {fmtTimestamp(g.created_at)} · gate{" "}
            <code className="font-mono">{g.gate_id}</code>
          </>
        }
        right={<VerdictBadge verdict={g.verdict} />}
      />

      <Headline result={g} />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Scenarios" value={scenarios.length} sub={`${g.policy.runs} runs each`} />
        <StatTile
          label="Skipped"
          value={skipped.length}
          sub={skipped.length ? "not run at all" : "nothing left out"}
        />
        <StatTile
          label="Run errors"
          value={errors.length}
          sub={errors.length ? "excluded from every rate" : "none"}
          color={errors.length ? "#d73838" : undefined}
        />
        <StatTile
          label="Exit code"
          value={g.exit_code}
          sub={g.exit_code === 0 ? "CI passes" : "CI fails"}
        />
      </div>

      <Policy result={g} />

      <div className="section-title">Per scenario</div>
      <div className="card-tight mb-3">
        <table className="ck-table">
          <thead>
            <tr>
              <th>Scenario</th>
              <th className="!w-20">Pass</th>
              <th className="!w-20">Rate</th>
              <th className="!w-36">{pct(g.policy.confidence)} CI</th>
              <th className="!w-24">Reliability</th>
              <th className="!w-32">Reading</th>
            </tr>
          </thead>
          <tbody>
            {scenarios.length === 0 && (
              <tr>
                <td colSpan={6} className="text-sm text-ink-3 dark:text-paper-3">
                  This gate ran no scenarios, which is why it could not decide.
                </td>
              </tr>
            )}
            {scenarios.map((s) => (
              <ScenarioRow key={s.scenario} stat={s} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="space-y-2 mb-7">
        {scenarios.map((s) => (
          <div key={s.scenario} className="text-xs text-ink-3 dark:text-paper-3">
            <span className="font-mono">{s.scenario}</span>: {s.evidence}
          </div>
        ))}
      </div>

      {skipped.length > 0 && (
        <>
          <div className="section-title">Skipped ({skipped.length})</div>
          <div className="card-tight mb-7">
            <table className="ck-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Why it was not run</th>
                </tr>
              </thead>
              <tbody>
                {skipped.map((s) => (
                  <tr key={s.path}>
                    <td className="font-mono text-xs">{s.path}</td>
                    <td className="text-xs">{s.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {errors.length > 0 && (
        <>
          <div className="section-title">Run errors ({errors.length})</div>
          <div className="card border-fail bg-fail-soft mb-7">
            <p className="text-sm mb-3">
              These runs produced no pass/fail sample, so they are not counted
              in any rate above. A sandbox that would not start says nothing
              about the agent — which is why the verdict is ERROR rather than a
              pass rate computed from what happened to survive.
            </p>
            <ul className="space-y-1 font-mono text-xs">
              {errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </div>
        </>
      )}

      {(g.notes || []).length > 0 && (
        <>
          <div className="section-title">Notes</div>
          <div className="card mb-7">
            <ul className="space-y-1 text-sm">
              {g.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          </div>
        </>
      )}

      <div className="text-xs text-ink-3 dark:text-paper-3 space-y-1">
        {(g.baseline_updated || []).length > 0 && (
          <div>Baseline updated for {g.baseline_updated.length} scenario(s).</div>
        )}
        {g.certificate && (
          <div>
            Signed certificate written to{" "}
            <code className="font-mono">{g.certificate}</code>.
          </div>
        )}
      </div>
    </>
  );
}

function Headline({ result: g }: { result: GateResult }) {
  const tone =
    g.verdict === "SHIP"
      ? "border-pass bg-pass-soft"
      : g.verdict === "BLOCK" || g.verdict === "ERROR"
        ? "border-fail bg-fail-soft"
        : "border-warn bg-warn-soft";
  return (
    <div className={`card ${tone} mb-7`}>
      <div className="flex items-baseline gap-4 flex-wrap">
        <div className="text-3xl font-bold font-mono">{g.verdict}</div>
        <div className="font-mono text-sm text-ink-3 dark:text-paper-3">
          exit {g.exit_code}
        </div>
      </div>
      <p className="text-sm mt-2">{MEANING[g.verdict]}.</p>
    </div>
  );
}

function Policy({ result: g }: { result: GateResult }) {
  const p = g.policy;
  const rows: [string, string][] = [
    ["runs per scenario", String(p.runs)],
    ["a run passes at", `${p.pass_threshold} / 100`],
    ["confidence", pct(p.confidence)],
    ["SHIP needs a lower bound of", p.ship_min.toFixed(2)],
    ["BLOCK at an upper bound of", p.block_max.toFixed(2)],
    ["regression drop", p.regression_drop.toFixed(2)],
    ["runs a clean scenario needs to SHIP", String(p.runs_needed_to_ship)],
    [
      "CONDITIONAL exits 0",
      p.allow_conditional && !p.strict ? "yes" : "no",
    ],
  ];
  return (
    <details className="card-tight mb-7">
      <summary className="cursor-pointer px-4 py-2 text-label font-mono uppercase text-ink-3 dark:text-paper-3 border-b border-ink dark:border-paper-3 bg-paper-2 dark:bg-ink">
        Policy this verdict was decided under
      </summary>
      <table className="ck-table">
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <td className="text-xs !w-72">{label}</td>
              <td className="font-mono text-xs">{value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function ScenarioRow({ stat: s }: { stat: GateScenarioStat }) {
  const [k, reliability] = bestReliability(s.pass_hat_k);
  return (
    <tr>
      <td className="font-mono text-xs">{s.scenario}</td>
      <td className="font-mono text-xs">
        {s.passes}/{s.n}
        {s.error_runs > 0 && (
          <span className="text-fail"> +{s.error_runs} err</span>
        )}
      </td>
      <td className="font-mono text-xs">{pct(s.pass_rate)}</td>
      <td className="font-mono text-xs">
        [{pct(s.ci_low)}, {pct(s.ci_high)}]
      </td>
      <td className="font-mono text-xs">
        {k === null ? "—" : `pass^${k} ${pct(reliability)}`}
      </td>
      <td>
        <span className={`badge badge-${CLASS_VARIANT[s.classification] || "d"}`}>
          {s.classification.replace(/_/g, " ")}
        </span>
      </td>
    </tr>
  );
}

/** The longest streak this scenario has evidence for. */
function bestReliability(map: Record<string, number>): [number | null, number] {
  const ks = Object.keys(map || {}).map(Number).filter((n) => !Number.isNaN(n));
  if (ks.length === 0) return [null, 0];
  const k = Math.max(...ks);
  return [k, map[String(k)]];
}

function pct(value: number): string {
  return `${Math.round(value * 100)}%`;
}
