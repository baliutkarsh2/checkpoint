import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Search, ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { shortId } from "@/lib/format";
import { useHotkey } from "@/lib/keyboard";

interface Item {
  kind: "page" | "run" | "scenario" | "action";
  label: string;
  hint?: string;
  perform: () => void;
}

export default function CommandPalette({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);

  // Fetch a small slice of recent runs + scenarios for fuzzy matching.
  const { data: runs } = useQuery({
    queryKey: ["runs", { palette: true }],
    queryFn: () => api.runs({ per_page: 50 }),
    enabled: open,
  });
  const { data: scenarios } = useQuery({
    queryKey: ["scenarios"],
    queryFn: () => api.scenarios(),
    enabled: open,
  });

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
    }
  }, [open]);

  useHotkey("escape", onClose, { allowInInputs: true });

  const items: Item[] = useMemo(() => {
    const out: Item[] = [
      {
        kind: "page",
        label: "Runs",
        hint: "Recent run history",
        perform: () => navigate("/"),
      },
      {
        kind: "page",
        label: "Scenarios",
        hint: "All bundled + local scenarios",
        perform: () => navigate("/scenarios"),
      },
      {
        kind: "page",
        label: "Report",
        hint: "Trend + flaky criteria",
        perform: () => navigate("/report"),
      },
    ];
    runs?.rows.forEach((r) =>
      out.push({
        kind: "run",
        label: `${r.scenario || "(inline)"} · ${shortId(r.run_id)}`,
        hint: `${Math.round(r.satisfaction)}/100`,
        perform: () => navigate(`/runs/${r.run_id}`),
      }),
    );
    scenarios?.scenarios.forEach((s) =>
      out.push({
        kind: "scenario",
        label: s.title,
        hint: s.path,
        perform: () => navigate(`/scenarios?focus=${encodeURIComponent(s.path)}`),
      }),
    );
    return out;
  }, [navigate, runs, scenarios]);

  const filtered = useMemo(() => {
    if (!query) return items.slice(0, 30);
    const q = query.toLowerCase();
    return items.filter((i) => i.label.toLowerCase().includes(q)).slice(0, 30);
  }, [items, query]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[200] bg-ink/40 backdrop-blur-sm flex items-start justify-center pt-24"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl bg-paper dark:bg-ink-2 border border-ink dark:border-paper-3 shadow-offset"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 px-4 border-b border-ink dark:border-paper-3 h-12">
          <Search size={16} className="text-ink-3 dark:text-paper-3" />
          <input
            autoFocus
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setCursor(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setCursor((c) => Math.min(c + 1, filtered.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setCursor((c) => Math.max(c - 1, 0));
              } else if (e.key === "Enter") {
                filtered[cursor]?.perform();
                onClose();
              }
            }}
            placeholder="Search runs, scenarios, pages…"
            className="bg-transparent flex-1 outline-hidden text-sm font-sans py-3"
          />
          <span className="kbd">esc</span>
        </div>
        <ul className="max-h-[60vh] overflow-y-auto">
          {filtered.length === 0 && (
            <li className="px-4 py-6 text-center text-ink-3 dark:text-paper-3 text-sm">
              No results
            </li>
          )}
          {filtered.map((it, i) => (
            <li
              key={`${it.kind}-${it.label}-${i}`}
              className={`px-4 py-2.5 flex items-center justify-between cursor-pointer ${
                i === cursor
                  ? "bg-paper-2 dark:bg-ink"
                  : "hover:bg-paper-2 dark:hover:bg-ink"
              }`}
              onClick={() => {
                it.perform();
                onClose();
              }}
              onMouseEnter={() => setCursor(i)}
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className="font-mono text-[10px] uppercase text-ink-4 dark:text-paper-3 w-14">
                  {it.kind}
                </span>
                <span className="text-sm truncate">{it.label}</span>
              </div>
              <div className="flex items-center gap-3">
                {it.hint && (
                  <span className="font-mono text-xs text-ink-3 dark:text-paper-3">
                    {it.hint}
                  </span>
                )}
                <ArrowRight size={12} className="text-ink-4" />
              </div>
            </li>
          ))}
        </ul>
        <div className="border-t border-paper-3 dark:border-ink-3 px-4 py-2 text-[10px] font-mono uppercase text-ink-3 dark:text-paper-3 flex justify-between">
          <span>
            <span className="kbd">↑↓</span> navigate · <span className="kbd">↵</span> select
          </span>
          <span>{filtered.length} results</span>
        </div>
      </div>
    </div>
  );
}
