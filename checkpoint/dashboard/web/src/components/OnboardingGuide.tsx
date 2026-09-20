import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Check, Copy, FileText, ShieldCheck, Terminal } from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";

/**
 * Onboarding for a repository that has an agent but no Checkpoint yet. Lives
 * both as a Setup tab and as the empty state on the Runs page.
 *
 * Every step is one command that can be pasted unchanged.
 */
export default function OnboardingGuide() {
  const meta = useQuery({ queryKey: ["meta"], queryFn: api.meta });
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const scenarios = useQuery({ queryKey: ["scenarios"], queryFn: () => api.scenarios() });

  const sampleScenario = scenarios.data?.scenarios[0];
  const configured = Boolean(config.data?.exists);

  return (
    <div className="space-y-6">
      <Intro version={meta.data?.version} />

      <Step
        n={1}
        title="Point Checkpoint at your agent"
        done={configured}
        body={
          <>
            <CodeBlock language="bash">{`checkpoint init --command "python my_agent.py"`}</CodeBlock>
            <p className="text-sm text-ink-3 dark:text-paper-3 mt-2">
              This writes <code className="font-mono">checkpoint.toml</code> and
              a starter scenario. Your agent's code is not touched: Checkpoint
              runs the command you already run, puts the task in{" "}
              <code className="font-mono">$CHECKPOINT_TASK</code>, and reads the
              final answer from stdout.
            </p>
            {configured && config.data && (
              <Tip>
                Already set up — the config is at{" "}
                <code className="font-mono">{config.data.path}</code>.{" "}
                <Link to="/setup?tab=config" className="underline font-medium">
                  See what it says
                </Link>
                .
              </Tip>
            )}
          </>
        }
      />

      <Step
        n={2}
        title="Run a scenario and read the trajectory"
        body={
          <>
            <CodeBlock language="bash">
              {sampleScenario
                ? `checkpoint run scenarios/${sampleScenario.path}`
                : "checkpoint run"}
            </CodeBlock>
            <p className="text-sm text-ink-3 dark:text-paper-3 mt-2">
              With no argument it runs every scenario the project declares. Each
              run lands here with the calls the agent made to the twins, the
              state it left behind, and the criteria that failed.
            </p>
            <ul className="text-sm space-y-1 mt-1.5">
              <li>
                → Browse{" "}
                <Link to="/scenarios" className="font-medium underline">
                  Scenarios
                </Link>{" "}
                and hit <strong>Run</strong> on any card
              </li>
              <li>
                → Or <strong>New run</strong> on the{" "}
                <Link to="/" className="font-medium underline">
                  Runs
                </Link>{" "}
                page
              </li>
            </ul>
          </>
        }
      />

      <Step
        n={3}
        title="Gate the build on the pass rate, not on one run"
        body={
          <>
            <CodeBlock language="bash">{`checkpoint gate`}</CodeBlock>
            <p className="text-sm text-ink-3 dark:text-paper-3 mt-2">
              Runs every scenario enough times for the result to mean something
              and exits 0 only on SHIP. Put it in CI; read the verdicts on the{" "}
              <Link to="/gates" className="font-medium underline">
                Gate
              </Link>{" "}
              page.
            </p>
            <Tip>
              A perfect run of fewer than 16 cannot clear the default ship
              threshold, so the gate reports INCONCLUSIVE and says how many runs
              it needs — rather than calling a small sample a pass.
            </Tip>
          </>
        }
      />

      <RefCard />
    </div>
  );
}

function Intro({ version }: { version: string | undefined }) {
  return (
    <div className="card">
      <div className="flex items-start gap-3">
        <div>
          <h2 className="font-bold text-lg leading-tight">
            Checkpoint
            {version && (
              <span className="text-ink-3 dark:text-paper-3 text-sm font-mono ml-2">
                v{version}
              </span>
            )}
          </h2>
          <p className="text-sm text-ink-3 dark:text-paper-3 mt-1">
            Run your real agent, unmodified, against stateful copies of the
            services it calls — GitHub, Slack, Stripe, Linear, Supabase, Discord
            and Google Workspace. Check what it actually did, not just what it
            said.
          </p>
        </div>
      </div>
    </div>
  );
}

function Step({
  n,
  title,
  body,
  done,
}: {
  n: number;
  title: string;
  body: React.ReactNode;
  done?: boolean;
}) {
  return (
    <div className="card">
      <div className="flex items-center gap-3 mb-3">
        <div className="w-7 h-7 border border-ink bg-accent text-ink font-bold flex items-center justify-center text-sm">
          {done ? <Check size={14} /> : n}
        </div>
        <h3 className="font-bold">{title}</h3>
      </div>
      <div>{body}</div>
    </div>
  );
}

function CodeBlock({ children, language }: { children: string; language?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative group">
      <pre className="bg-ink text-paper p-3 font-mono text-xs overflow-x-auto whitespace-pre">
        {children}
      </pre>
      <button
        type="button"
        className="absolute top-2 right-2 px-2 py-1 text-[10px] font-mono uppercase tracking-wider bg-paper text-ink border border-ink opacity-0 group-hover:opacity-100 transition"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(children);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            /* ignore */
          }
        }}
        aria-label="Copy"
      >
        {copied ? (
          <>
            <Check size={10} className="inline mr-1" /> Copied
          </>
        ) : (
          <>
            <Copy size={10} className="inline mr-1" /> Copy
          </>
        )}
      </button>
      {language && (
        <div className="absolute top-2 left-3 text-[10px] font-mono uppercase tracking-wider text-paper/40">
          {language}
        </div>
      )}
    </div>
  );
}

function Tip({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-3 border-l-2 border-accent bg-paper-2 dark:bg-ink p-3 text-sm">
      {children}
    </div>
  );
}

function RefCard() {
  return (
    <div className="card-flat">
      <div className="card-title">More help</div>
      <ul className="space-y-2 text-sm">
        <li className="flex items-start gap-2">
          <Terminal size={14} className="mt-0.5 shrink-0" />
          <span>
            <code className="font-mono">checkpoint --help</code> — fifteen
            commands, grouped by what you are trying to do
          </span>
        </li>
        <li className="flex items-start gap-2">
          <ShieldCheck size={14} className="mt-0.5 shrink-0" />
          <span>
            <code className="font-mono">checkpoint check</code> — parse and lint
            your scenarios before you spend runs on them, or use the{" "}
            <Link to="/setup?tab=validate" className="font-medium underline">
              Validate
            </Link>{" "}
            tab
          </span>
        </li>
        <li className="flex items-start gap-2">
          <Terminal size={14} className="mt-0.5 shrink-0" />
          <span>
            <code className="font-mono">checkpoint twins</code> — start a twin
            and poke at it by hand, or use the{" "}
            <Link to="/twins" className="font-medium underline">
              Twins
            </Link>{" "}
            page
          </span>
        </li>
        <li className="flex items-start gap-2">
          <FileText size={14} className="mt-0.5 shrink-0" />
          <span>
            <a href="/api/docs" target="_blank" className="font-medium underline">
              OpenAPI / Swagger
            </a>
            {" "}— every JSON endpoint this dashboard uses, with try-it-out
          </span>
        </li>
      </ul>
    </div>
  );
}
