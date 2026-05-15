import { useEffect, useRef } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useJobStream } from "@/lib/sse";
import { fmtTimestamp } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
} from "@/components/bits";

export default function LiveRun() {
  const { jobId = "" } = useParams();
  const job = useQuery({
    queryKey: ["jobs", jobId],
    queryFn: () => api.jobs.get(jobId),
    enabled: Boolean(jobId),
    refetchInterval: 2_000,
  });
  const stream = useJobStream(jobId || null);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [stream.lines.length]);

  if (job.isLoading) return <Loading />;
  if (job.error) return <ErrorBox error={job.error} />;
  if (!job.data) return null;

  const j = job.data;
  const variant =
    j.status === "succeeded"
      ? "pass"
      : j.status === "failed"
        ? "fail"
        : j.status === "running"
          ? "info"
          : "warn";

  return (
    <>
      <PageHead
        title={`Live run · ${j.scenario}`}
        sub={
          <>
            Started {fmtTimestamp(j.started_at)} · job{" "}
            <code className="font-mono">{j.job_id.slice(0, 12)}</code> ·{" "}
            <Badge variant={variant}>{j.status}</Badge>
            {j.exit_code !== null && (
              <>
                {" "}· exit{" "}
                <code className="font-mono">{j.exit_code}</code>
              </>
            )}
          </>
        }
        right={
          j.run_id ? (
            <Link to={`/runs/${j.run_id}`} className="btn-accent">
              View results →
            </Link>
          ) : undefined
        }
      />

      <div className="card-tight">
        <div className="border-b border-paper-3 dark:border-ink-3 px-4 py-2 flex justify-between items-center text-xs font-mono">
          <span>
            Stream:{" "}
            <span
              className={
                stream.status === "open"
                  ? "text-pass"
                  : stream.status === "closed"
                    ? "text-fail"
                    : "text-warn"
              }
            >
              {stream.status}
            </span>
          </span>
          <span className="text-ink-3 dark:text-paper-3">
            {stream.lines.length} lines
          </span>
        </div>
        <div
          ref={logRef}
          className="font-mono text-[12px] leading-relaxed bg-ink text-paper p-4 max-h-[60vh] overflow-y-auto whitespace-pre-wrap"
        >
          {stream.lines.length === 0 && (
            <span className="text-paper/40">Waiting for output…</span>
          )}
          {stream.lines.map((l, i) => (
            <div key={i}>{l}</div>
          ))}
          {stream.ended && (
            <div className="text-accent mt-2">
              ── ended (exit {stream.ended.exit_code}, {stream.ended.status}) ──
            </div>
          )}
        </div>
      </div>

      <div className="text-xs text-ink-3 dark:text-paper-3 mt-4 font-mono">
        cmd: <code>{j.cmd.join(" ")}</code>
      </div>
    </>
  );
}
