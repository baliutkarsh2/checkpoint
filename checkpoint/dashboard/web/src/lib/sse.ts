import { useEffect, useRef, useState } from "react";

// Generic Server-Sent Events hook. Reconnects on error with exponential backoff.
// Each named event invokes the matching listener with the parsed JSON payload.
export type SSEListeners = Record<string, (payload: unknown) => void>;

export function useEventSource(
  url: string,
  listeners: SSEListeners,
  enabled = true,
) {
  // Keep the latest listeners in a ref so the effect doesn't re-subscribe on every
  // render. Callers typically pass inline objects.
  const listenersRef = useRef(listeners);
  listenersRef.current = listeners;

  const [status, setStatus] = useState<"connecting" | "open" | "closed">(
    "connecting",
  );

  useEffect(() => {
    if (!enabled) return;

    let es: EventSource | null = null;
    let backoff = 500;
    let cancelled = false;
    let timer: number | null = null;

    const connect = () => {
      if (cancelled) return;
      es = new EventSource(url);
      setStatus("connecting");

      es.onopen = () => {
        backoff = 500;
        setStatus("open");
      };
      es.onerror = () => {
        setStatus("closed");
        es?.close();
        if (cancelled) return;
        timer = window.setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 8_000);
      };

      // Subscribe to every named event the caller cares about.
      Object.keys(listenersRef.current).forEach((evt) => {
        es!.addEventListener(evt, (e: MessageEvent) => {
          let payload: unknown = e.data;
          try {
            payload = JSON.parse(e.data);
          } catch {
            // leave raw
          }
          listenersRef.current[evt]?.(payload);
        });
      });
    };

    connect();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
      es?.close();
    };
  }, [url, enabled]);

  return status;
}

// Streams a job's stdout/stderr live. Yields lines as they arrive plus a final
// "ended" event with the exit code.
export function useJobStream(jobId: string | null) {
  const [lines, setLines] = useState<string[]>([]);
  const [ended, setEnded] = useState<{ exit_code: number; status: string } | null>(
    null,
  );

  const status = useEventSource(
    jobId ? `/api/jobs/${jobId}/stream` : "",
    {
      log: (payload) => {
        const line = (payload as { line?: string }).line ?? String(payload);
        setLines((prev) => [...prev, line]);
      },
      ended: (payload) => {
        setEnded(payload as { exit_code: number; status: string });
      },
    },
    Boolean(jobId),
  );

  return { lines, ended, status };
}
