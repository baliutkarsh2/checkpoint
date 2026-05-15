// Display formatters reused across pages.

export function shortId(id: string | null | undefined, len = 12): string {
  if (!id) return "—";
  return id.slice(0, len);
}

export function scoreColor(score: number): string {
  if (score >= 90) return "#0ea83b";
  if (score >= 70) return "#c89124";
  return "#d73838";
}

export function fmtTimestamp(ts: string | null | undefined): string {
  if (!ts) return "—";
  return ts.slice(0, 19).replace("T", " ");
}

export function truncateMid(s: string, max: number): string {
  if (!s || s.length <= max) return s;
  const head = Math.floor((max - 1) / 2);
  const tail = max - head - 1;
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

export function classNames(
  ...xs: (string | false | null | undefined)[]
): string {
  return xs.filter(Boolean).join(" ");
}

// Build a curl reproduction for a trace event.
export function eventToCurl(e: {
  method: string;
  path: string;
  request_body?: unknown;
  _clone?: string;
}): string {
  const url = `${e._clone || "twin"}${e.path}`;
  const parts = [`curl -X ${e.method}`, `'${url}'`];
  if (e.request_body !== undefined && e.request_body !== null) {
    parts.push(
      `-H 'Content-Type: application/json'`,
      `-d '${JSON.stringify(e.request_body).replace(/'/g, "'\\''")}'`,
    );
  }
  return parts.join(" ");
}

// Tiny JSON pretty-print with stable key ordering for diffs/snapshots.
export function stableStringify(value: unknown, indent = 2): string {
  const seen = new WeakSet();
  const replacer = (_k: string, v: unknown) => {
    if (typeof v === "object" && v !== null) {
      if (seen.has(v as object)) return "[Circular]";
      seen.add(v as object);
      if (!Array.isArray(v)) {
        return Object.keys(v as object)
          .sort()
          .reduce((acc, k) => {
            (acc as Record<string, unknown>)[k] = (v as Record<string, unknown>)[k];
            return acc;
          }, {} as Record<string, unknown>);
      }
    }
    return v;
  };
  try {
    return JSON.stringify(value, replacer, indent);
  } catch {
    return String(value);
  }
}
