import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, Save } from "lucide-react";
import { api } from "@/lib/api";
import {
  ErrorBox,
  Loading,
  PageHead,
} from "@/components/bits";

/** Dashboard equivalent of `checkpoint config show/set/unset`. */
export default function Config() {
  const qc = useQueryClient();
  const [revealEnv, setRevealEnv] = useState(false);
  const q = useQuery({
    queryKey: ["config", revealEnv],
    queryFn: () => api.config.get(revealEnv),
  });

  const setMut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) =>
      api.config.set(key, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
  });
  const unsetMut = useMutation({
    mutationFn: (key: string) => api.config.unset(key),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config"] }),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data) return null;

  const cfg = q.data;
  // Show every known key (so the user discovers what they CAN set), plus any
  // currently-set unknown keys (forward-compat).
  const allKeys = Array.from(
    new Set([...Object.keys(cfg.known_keys), ...Object.keys(cfg.values)]),
  ).sort();

  return (
    <>
      <PageHead
        title="Config"
        sub={
          <>
            User config at <code className="font-mono">{cfg.path}</code>
            {!cfg.exists && " · file doesn't exist yet (set any value to create it)"}
          </>
        }
        right={
          <label className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={revealEnv}
              onChange={(e) => setRevealEnv(e.target.checked)}
            />
            Reveal env: indirections
          </label>
        }
      />

      <div className="card-tight">
        <table className="ck-table">
          <thead>
            <tr>
              <th>Key</th>
              <th>Value</th>
              <th>Description</th>
              <th className="!w-24">Action</th>
            </tr>
          </thead>
          <tbody>
            {allKeys.map((key) => (
              <ConfigRow
                key={key}
                k={key}
                value={cfg.values[key]}
                description={cfg.known_keys[key] || ""}
                onSave={(v) => setMut.mutate({ key, value: v })}
                onUnset={() => unsetMut.mutate(key)}
                saving={setMut.isPending}
              />
            ))}
          </tbody>
        </table>
      </div>

      {(setMut.error || unsetMut.error) && (
        <div className="mt-4">
          <ErrorBox error={setMut.error || unsetMut.error} />
        </div>
      )}
    </>
  );
}

function ConfigRow({
  k,
  value,
  description,
  onSave,
  onUnset,
  saving,
}: {
  k: string;
  value: unknown;
  description: string;
  onSave: (v: unknown) => void;
  onUnset: () => void;
  saving: boolean;
}) {
  const set = value !== null && value !== undefined;
  const [draft, setDraft] = useState<string>(set ? String(value) : "");

  return (
    <tr>
      <td className="font-mono text-xs">{k}</td>
      <td>
        <input
          className="input !h-8 !text-xs w-full"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={set ? "" : "(unset)"}
        />
      </td>
      <td className="text-xs text-ink-3 dark:text-paper-3">{description}</td>
      <td>
        <div className="flex gap-1">
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            disabled={saving || draft === String(value ?? "")}
            onClick={() => onSave(draft)}
            title="Save (PUT /api/config/<key>)"
          >
            <Save size={12} />
          </button>
          {set && (
            <button
              type="button"
              className="btn-outline !h-8 !text-xs"
              onClick={onUnset}
              title="Unset"
            >
              <Trash2 size={12} />
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}
