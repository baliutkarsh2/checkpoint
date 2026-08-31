import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Check,
  Copy,
  ExternalLink,
  FileText,
  GitBranch,
  Play,
  Terminal,
} from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";

/**
 * Onboarding guide for new users. Lives both as a Setup tab and as the
 * empty-state on the Runs page so first-time users hit it immediately.
 *
 * Three-step path tailored to the most common case: drop Checkpoint into an
 * existing agent repo. Each step is copy-pasteable and self-contained.
 */
export default function OnboardingGuide() {
  const meta = useQuery({ queryKey: ["meta"], queryFn: api.meta });
  const agents = useQuery({ queryKey: ["agents"], queryFn: api.agents });
  const scenarios = useQuery({ queryKey: ["scenarios"], queryFn: () => api.scenarios() });

  const sampleAgent = agents.data?.find((a) => a.source === "bundled") || agents.data?.[0];
  const sampleScenario = scenarios.data?.scenarios[0];

  return (
    <div className="space-y-6">
      <Intro version={meta.data?.version} />

      <Step
        n={1}
        title="Install"
        body={
          <>
            <CodeBlock language="bash">{`pip install checkpoint
export OPENAI_API_KEY=sk-...   # used by the LLM judge`}</CodeBlock>
            <p className="text-sm text-ink-3 dark:text-paper-3 mt-2">
              You'll also need Docker running — that's the default run mode so
              your agent's real SDKs (PyGithub, supabase-py, ...) get
              TLS-intercepted to local twins.
            </p>
          </>
        }
      />

      <Step
        n={2}
        title="Wrap your agent in a Checkpoint-compatible harness"
        body={
          <>
            <p className="text-sm mb-3">
              In your existing agent repo, run:
            </p>
            <CodeBlock language="bash">{`cd /path/to/your-agent-repo
checkpoint init --template openai-agents
# templates: raw, openai-agents, anthropic, langchain`}</CodeBlock>
            <p className="text-sm text-ink-3 dark:text-paper-3 mt-2">
              This creates a <code>harness/</code> directory with a Dockerfile,
              an <code>entrypoint.sh</code>, and a starter <code>harness.py</code>{" "}
              shim. Edit <code>harness.py</code> to call into your agent's
              entry point (it reads <code>CHECKPOINT_TASK</code> from env and
              prints <code>{"{\"text\": \"...\"}"}</code> to stdout).
            </p>
            <Tip>
              Already have a working agent? The fastest path is to make your
              entry point read <code>CHECKPOINT_TASK</code> and print
              <code> {"{\"text\": \"final answer\"}"} </code> at the end. The
              shim does the rest.
            </Tip>
          </>
        }
      />

      <Step
        n={3}
        title="Pick a scenario and run it"
        body={
          <>
            <p className="text-sm mb-3">From the CLI:</p>
            <CodeBlock language="bash">
              {sampleScenario
                ? `checkpoint run scenarios/${sampleScenario.path} --harness-dir harness/`
                : `checkpoint run scenarios/<your-scenario>.md --harness-dir harness/`}
            </CodeBlock>
            <p className="text-sm text-ink-3 dark:text-paper-3 mt-2">
              Or from this dashboard:
            </p>
            <ul className="text-sm space-y-1 mt-1.5">
              <li>
                → Go to{" "}
                <Link to="/scenarios" className="font-medium underline">
                  Scenarios
                </Link>{" "}
                and click <strong>Run</strong> on any card
              </li>
              <li>
                → Or hit <strong>New run</strong> on the{" "}
                <Link to="/" className="font-medium underline">
                  Runs
                </Link>{" "}
                page and pick agent + scenario
              </li>
            </ul>
            {sampleAgent && sampleScenario && (
              <Tip>
                For instant gratification: the auto-discovered{" "}
                <strong>{sampleAgent.name}</strong> agent + the bundled{" "}
                <strong>{sampleScenario.title}</strong> scenario are both ready
                to go right now. Open Scenarios → click Run.
              </Tip>
            )}
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
            Welcome to Checkpoint
            {version && (
              <span className="text-ink-3 dark:text-paper-3 text-sm font-mono ml-2">
                v{version}
              </span>
            )}
          </h2>
          <p className="text-sm text-ink-3 dark:text-paper-3 mt-1">
            Test your AI agent against stateful synthetic copies of GitHub,
            Slack, Stripe, Linear, Supabase, Discord, and Google Workspace.
            Your agent uses its real SDKs, unmodified. No real-API spend.
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
}: {
  n: number;
  title: string;
  body: React.ReactNode;
}) {
  return (
    <div className="card">
      <div className="flex items-center gap-3 mb-3">
        <div className="w-7 h-7 border border-ink bg-accent text-ink font-bold flex items-center justify-center text-sm">
          {n}
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
            <code className="font-mono">checkpoint --help</code> — every CLI
            command (run, init, scenario, clone, traces, compare, doctor,
            config, debug, serve)
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
        <li className="flex items-start gap-2">
          <ExternalLink size={14} className="mt-0.5 shrink-0" />
          <span>
            See <code className="font-mono">SETUP.md</code> in the repo for a
            walk-through of integrating Checkpoint into an existing repo
          </span>
        </li>
        <li className="flex items-start gap-2">
          <GitBranch size={14} className="mt-0.5 shrink-0" />
          <span>
            Four reference agents under{" "}
            <code className="font-mono">examples/agents/</code> (OpenAI tools,
            Anthropic tools, LangChain ReAct, MCP client) — these are full
            Dockerized harnesses ready to run against any scenario
          </span>
        </li>
        <li className="flex items-start gap-2">
          <Play size={14} className="mt-0.5 shrink-0" />
          <span>
            <Link to="/setup?tab=validate" className="font-medium underline">
              Validate
            </Link>{" "}
            tab in Setup — lint a scenario before running it
          </span>
        </li>
      </ul>
    </div>
  );
}
