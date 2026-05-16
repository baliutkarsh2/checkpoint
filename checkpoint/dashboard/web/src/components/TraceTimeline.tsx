import { useMemo, useState } from "react";
import { Copy, Check } from "lucide-react";
import type { TraceEvent } from "@/lib/api";
import { eventToCurl, stableStringify } from "@/lib/format";

const STATUS_COLOR = (status: number): string => {
  if (status >= 200 && status < 300) return "#0ea83b";
  if (status >= 300 && status < 400) return "#c89124";
  if (status >= 400 && status < 500) return "#d73838";
  if (status >= 500) return "#d73838";
  return "#5a5648";
};

interface TraceTimelineProps {
  events: TraceEvent[];
}

export default function TraceTimeline({ events }: TraceTimelineProps) {
  const [filter, setFilter] = useState({
    method: "" as string,
    status: "" as string,
    clone: "" as string,
    text: "" as string,
  });
  const [openIdx, setOpenIdx] = useState<number | null>(null);

  const clones = useMemo(
    () =>
      Array.from(
        new Set(events.map((e) => e._clone || (e as { clone?: string }).clone).filter(Boolean) as string[]),
      ),
    [events],
  );
  const methods = useMemo(
    () => Array.from(new Set(events.map((e) => e.method))),
    [events],
  );

  const filtered = useMemo(() => {
    return events
      .map((e, i) => ({ e, i }))
      .filter(({ e }) => {
        if (filter.method && e.method !== filter.method) return false;
        const clone = e._clone || (e as { clone?: string }).clone || "";
        if (filter.clone && clone !== filter.clone) return false;
        if (filter.status) {
          const klass = String(e.status)[0];
          if (klass !== filter.status) return false;
        }
        if (filter.text) {
          const q = filter.text.toLowerCase();
          if (!(e.path || "").toLowerCase().includes(q)) return false;
        }
        return true;
      });
  }, [events, filter]);

  if (events.length === 0) {
    return (
      <div className="card text-center text-ink-3 dark:text-paper-3 text-sm">
        No API calls captured for this run.
      </div>
    );
  }

  return (
    <div>
      <div className="flex flex-wrap gap-2 items-center mb-4">
        <input
          className="input flex-1 min-w-[200px]"
          placeholder="filter path…"
          value={filter.text}
          onChange={(e) => setFilter((f) => ({ ...f, text: e.target.value }))}
        />
        <select
          className="input"
          value={filter.method}
          onChange={(e) => setFilter((f) => ({ ...f, method: e.target.value }))}
          aria-label="Filter by method"
        >
          <option value="">all methods</option>
          {methods.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <select
          className="input"
          value={filter.status}
          onChange={(e) => setFilter((f) => ({ ...f, status: e.target.value }))}
          aria-label="Filter by status class"
        >
          <option value="">all statuses</option>
          <option value="2">2xx</option>
          <option value="3">3xx</option>
          <option value="4">4xx</option>
          <option value="5">5xx</option>
        </select>
        {clones.length > 1 && (
          <select
            className="input"
            value={filter.clone}
            onChange={(e) => setFilter((f) => ({ ...f, clone: e.target.value }))}
            aria-label="Filter by clone"
          >
            <option value="">all clones</option>
            {clones.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        )}
        <span className="text-xs text-ink-3 dark:text-paper-3 ml-auto font-mono">
          {filtered.length} / {events.length}
        </span>
      </div>

      <div className="card-tight">
        {filtered.map(({ e, i }) => {
          const open = openIdx === i;
          return (
            <div
              key={i}
              className="border-b border-paper-3 dark:border-ink-3 last:border-0"
            >
              <button
                type="button"
                onClick={() => setOpenIdx(open ? null : i)}
                className="w-full grid grid-cols-[60px_1fr_60px_90px] gap-3 p-3 font-mono text-xs items-center text-left hover:bg-paper-2 dark:hover:bg-ink"
              >
                <span className="font-semibold">{e.method || "—"}</span>
                <span className="truncate">{e.path || "—"}</span>
                <span style={{ color: STATUS_COLOR(e.status) }}>{e.status}</span>
                <span className="text-ink-3 dark:text-paper-3 truncate">
                  {e._clone || (e as { clone?: string }).clone || "—"}
                </span>
              </button>
              {open && <TraceDetail event={e} />}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TraceDetail({ event }: { event: TraceEvent }) {
  return (
    <div className="bg-paper dark:bg-ink-2 px-3 py-3 border-t border-paper-3 dark:border-ink-3">
      <div className="grid md:grid-cols-2 gap-4">
        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="card-title !mb-0">Request</span>
            <CopyBtn text={eventToCurl(event)} label="Copy as curl" />
          </div>
          {event.request_body !== undefined && event.request_body !== null ? (
            <pre className="json">{stableStringify(event.request_body)}</pre>
          ) : (
            <div className="text-xs text-ink-4 dark:text-paper-3 italic">
              No request body
            </div>
          )}
        </div>
        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="card-title !mb-0">Response</span>
            {typeof event.duration_ms === "number" && (
              <span className="text-[10px] font-mono text-ink-3 dark:text-paper-3">
                {event.duration_ms.toFixed(1)} ms
              </span>
            )}
          </div>
          {event.response_body !== undefined && event.response_body !== null ? (
            <pre className="json">{stableStringify(event.response_body)}</pre>
          ) : (
            <div className="text-xs text-ink-4 dark:text-paper-3 italic">
              No response body captured
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CopyBtn({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn-ghost !h-7 !text-[11px]"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* ignore */
        }
      }}
    >
      {copied ? <Check size={12} /> : <Copy size={12} />}
      <span>{copied ? "Copied" : label}</span>
    </button>
  );
}
