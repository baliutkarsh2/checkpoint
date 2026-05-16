import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, AlertTriangle, XCircle, Play } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Badge, ErrorBox, PageHead } from "@/components/bits";

const STARTER = `# My new scenario

## Prompt
What the agent should do.

## Success Criteria
- [D] Exactly 1 issue exists
- [P] The agent's final answer references the issue number

## Config
clones: github
runs: 1
timeout: 60
`;

/** Dashboard equivalent of `checkpoint validate <scenario.md>`. */
export default function Validate() {
  const [raw, setRaw] = useState(STARTER);
  const [picked, setPicked] = useState<string>("");

  // Drop-down of existing scenarios for one-click validation.
  const scenariosQ = useQuery({ queryKey: ["scenarios"], queryFn: () => api.scenarios() });

  const validateMut = useMutation({
    mutationFn: (body: { raw?: string; path?: string }) => api.validateScenario(body),
  });

  const r = validateMut.data;

  return (
    <>
      <PageHead
        title="Validate scenario"
        sub="Lint + parse a scenario without running it. Same as `checkpoint validate`."
      />

      <div className="grid lg:grid-cols-2 gap-5">
        <div className="card flex flex-col gap-3">
          <div className="card-title">Source</div>

          <div className="flex items-center gap-2">
            <select
              className="input !text-xs flex-1"
              value={picked}
              onChange={(e) => setPicked(e.target.value)}
            >
              <option value="">— or pick a bundled scenario —</option>
              {scenariosQ.data?.scenarios.map((s) => (
                <option key={s.path} value={s.path}>
                  {s.title} ({s.path})
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn-outline !text-xs"
              disabled={!picked || validateMut.isPending}
              onClick={() => validateMut.mutate({ path: picked })}
            >
              <Play size={12} /> Validate file
            </button>
          </div>

          <textarea
            className="input !h-[420px] font-mono !text-xs w-full whitespace-pre"
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            spellCheck={false}
          />

          <button
            type="button"
            className="btn-accent self-start"
            disabled={!raw.trim() || validateMut.isPending}
            onClick={() => validateMut.mutate({ raw })}
          >
            <Play size={14} />
            {validateMut.isPending ? "Validating…" : "Validate this text"}
          </button>

          {validateMut.error && <ErrorBox error={validateMut.error} />}
        </div>

        <div className="space-y-4">
          {!r && (
            <div className="card text-ink-3 dark:text-paper-3 text-sm">
              Paste markdown or pick a file and click Validate. Errors stop the run;
              warnings let you proceed but indicate something might be off.
            </div>
          )}

          {r && (
            <div
              className={
                "card " +
                (r.ok && r.warnings.length === 0
                  ? "border-pass"
                  : r.errors.length > 0
                    ? "border-fail"
                    : "border-warn")
              }
            >
              <div className="flex items-center gap-2 mb-2">
                {r.ok && r.warnings.length === 0 ? (
                  <>
                    <CheckCircle2 size={16} className="text-pass" />
                    <span className="font-medium">All good — ready to run</span>
                  </>
                ) : r.errors.length > 0 ? (
                  <>
                    <XCircle size={16} className="text-fail" />
                    <span className="font-medium">{r.errors.length} error(s)</span>
                  </>
                ) : (
                  <>
                    <AlertTriangle size={16} className="text-warn" />
                    <span className="font-medium">{r.warnings.length} warning(s)</span>
                  </>
                )}
              </div>
              <ul className="space-y-1 text-sm">
                {r.errors.map((e, i) => (
                  <li key={i} className="text-fail">✗ {e}</li>
                ))}
                {r.warnings.map((w, i) => (
                  <li key={i} className="text-warn">⚠ {w}</li>
                ))}
                {r.ok && r.warnings.length === 0 && (
                  <li className="text-pass">✓ no issues</li>
                )}
              </ul>
            </div>
          )}

          {r?.scenario && (
            <div className="card space-y-3">
              <div>
                <div className="card-title">Title</div>
                <div className="font-medium">{r.scenario.title || "(none)"}</div>
              </div>
              <div>
                <div className="card-title">Clones</div>
                <div className="flex gap-1 flex-wrap">
                  {r.scenario.clones.length === 0 ? (
                    <span className="text-xs text-ink-3 dark:text-paper-3 italic">none</span>
                  ) : (
                    r.scenario.clones.map((c) => (
                      <Badge key={c} variant="info">{c}</Badge>
                    ))
                  )}
                </div>
              </div>
              <div>
                <div className="card-title">Criteria ({r.scenario.criteria.length})</div>
                <ul className="space-y-1">
                  {r.scenario.criteria.map((c, i) => (
                    <li key={i} className="text-sm flex items-start gap-2">
                      <Badge variant={c.kind === "P" ? "judge" : "d"}>{c.kind}</Badge>
                      <span>{c.text}</span>
                    </li>
                  ))}
                </ul>
              </div>
              {r.ok && (
                <Link to="/scenarios" className="btn-outline text-xs">
                  Save + run from Scenarios page →
                </Link>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
