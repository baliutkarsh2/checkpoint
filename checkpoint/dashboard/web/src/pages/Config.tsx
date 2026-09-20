import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { ErrorBox, Loading, PageHead } from "@/components/bits";

/** The project's checkpoint.toml, as the CLI reads it.
 *
 *  Read-only: the config is a file in the repository that belongs in version
 *  control next to the scenarios it configures, so it is edited there and not
 *  from a web page that leaves no diff.
 *
 *  Pass `headless` when embedding inside the Setup hub.
 */
export default function Config({ headless = false }: { headless?: boolean }) {
  const q = useQuery({ queryKey: ["config"], queryFn: api.config });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const cfg = q.data;
  const sections = Object.entries(cfg.sections || {});
  const configured = sections.filter(([, values]) => Object.keys(values || {}).length > 0);

  const where = (
    <>
      <code className="font-mono">{cfg.path}</code>
      {!cfg.exists && " · not created yet"}
    </>
  );

  return (
    <>
      {!headless ? (
        <PageHead title="Config" sub={where} />
      ) : (
        <div className="text-sm text-ink-3 dark:text-paper-3 mb-4">{where}</div>
      )}

      {!cfg.exists && <NoConfig />}

      {cfg.problem && (
        <div className="card border-fail bg-fail-soft text-fail mb-5">
          <div className="card-title !text-fail">This file cannot be used as written</div>
          <div className="font-mono text-xs">{cfg.problem}</div>
        </div>
      )}

      {cfg.exists && !cfg.problem && configured.length === 0 && (
        <div className="card text-sm text-ink-3 dark:text-paper-3">
          The file exists but sets nothing, so every command runs on its defaults.
        </div>
      )}

      {configured.map(([name, values]) => (
        <Section key={name} name={name} values={values} />
      ))}

      {cfg.exists && !cfg.problem && configured.length > 0 && (
        <p className="text-xs text-ink-3 dark:text-paper-3 mt-5">
          Sections not listed here are unset, and their commands use the
          documented defaults. Edit the file to change any of this — a flag or
          an environment variable still wins over it for a single command.
        </p>
      )}
    </>
  );
}

function NoConfig() {
  const cmd = 'checkpoint init --command "python my_agent.py"';
  return (
    <div className="card mb-5">
      <div className="card-title">No config yet</div>
      <p className="text-sm mb-3">
        Point Checkpoint at the command that already runs your agent. This
        writes the file and a starter scenario; nothing that exists is touched.
      </p>
      <pre className="bg-ink text-paper p-3 font-mono text-xs overflow-x-auto whitespace-pre">
        {cmd}
      </pre>
    </div>
  );
}

function Section({ name, values }: { name: string; values: Record<string, unknown> }) {
  return (
    <div className="card-tight mb-4">
      <div className="px-4 py-2 border-b border-ink dark:border-paper-3 bg-paper-2 dark:bg-ink text-label font-mono uppercase text-ink-3 dark:text-paper-3">
        [{name}]
      </div>
      <table className="ck-table">
        <tbody>
          {Object.entries(values).map(([key, value]) => (
            <tr key={key}>
              <td className="font-mono text-xs !w-56">{key}</td>
              <td className="font-mono text-xs break-all">{render(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function render(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((v) => render(v)).join(", ");
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${k} = ${render(v)}`)
      .join("\n");
  }
  return String(value);
}
