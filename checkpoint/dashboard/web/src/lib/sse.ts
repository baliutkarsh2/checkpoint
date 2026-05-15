import { useEffect, useRef, useState } from "react";

// Generic Server-Sent Events hook. Reconnects on error with exponential
// backoff *unless* the server has signalled a clean end of stream — that
// matters for one-shot streams like job logs which the server closes after
// the terminal "ended" event.
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

  const [status, setStatus] = useState<"connecting" | "open" | "closed" | "ended">(
    enabled ? "connecting" : "closed",
  );

  useEffect(() => {
    if (!enabled) {
      setStatus("closed");
      return;
    }

    let es: EventSource | null = null;
    let backoff = 500;
    let cancelled = false;
    let stopReconnect = false;
    let timer: number | null = null;

    const connect = () => {
      if (cancelled || stopReconnect) return;
      es = new EventSource(url);
      setStatus("connecting");

      es.onopen = () => {
        backoff = 500;
        setStatus("open");
      };
      es.onerror = () => {
        es?.close();
        if (cancelled || stopReconnect) {
          setStatus(stopReconnect ? "ended" : "closed");
          return;
        }
        setStatus("closed");
        timer = window.setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 8_000);
      };

      // Subscribe to every named event the caller cares about. The reserved
      // "ended" event tells us the server has closed the stream intentionally;
      // we set stopReconnect so the next onerror (when the connection drops
      // cleanly) doesn't trigger a reconnect.
      Object.keys(listenersRef.current).forEach((evt) => {
        es!.addEventListener(evt, (e: MessageEvent) => {
          let payload: unknown = e.data;
          try {
            payload = JSON.parse(e.data);
          } catch {
            // leave raw
          }
          listenersRef.current[evt]?.(payload);
          if (evt === "ended") {
            stopReconnect = true;
            // Close immediately; don't wait for the server-side timeout.
            es?.close();
            setStatus("ended");
          }
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

// Streams a job's stdout/stderr live. Yields lines as they arrive plus a
// terminal "ended" event with the exit code. Once `ended` fires, the
// underlying EventSource is closed and no reconnects are attempted.
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
