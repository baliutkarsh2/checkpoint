import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, Square, RotateCcw, Database, Wrench } from "lucide-react";
import { api, type CloneInfo, type SupportedClone } from "@/lib/api";
import { fmtTimestamp } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
  StatTile,
} from "@/components/bits";

/**
 * Live clones page — start / stop / seed / reset / inspect twins from the
 * dashboard. The CLI's `checkpoint clone *` commands all go through the same
 * registry that backs this page, so changes here are visible everywhere.
 */
export default function Clones() {
  const liveQ = useQuery({ queryKey: ["clones"], queryFn: api.clones, refetchInterval: 5000 });
  const supportedQ = useQuery({ queryKey: ["clones", "supported"], queryFn: api.clonesSupported });

  if (liveQ.isLoading || supportedQ.isLoading) return <Loading />;
  if (liveQ.error) return <ErrorBox error={liveQ.error} />;
  if (supportedQ.error) return <ErrorBox error={supportedQ.error} />;

  const live = liveQ.data || [];
  const supported = supportedQ.data || [];
  const liveIds = new Set(live.map((c) => c.id));

  return (
    <>
      <PageHead
        title="Clones"
        sub="Long-lived twin sessions you can manually exercise. Each clone is a stateful synthetic copy of one SaaS service."
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Supported" value={supported.length} />
        <StatTile
          label="Currently running"
          value={live.length}
          color={live.length > 0 ? "#0ea83b" : undefined}
        />
        <StatTile label="Stopped" value={supported.length - live.length} />
        <StatTile label="Idle" value={live.length === 0 ? "yes" : "no"} />
      </div>

      <div className="section-title">All clones</div>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {supported.map((s) => (
          <CloneCard
            key={s.id}
            supported={s}
            live={live.find((l) => l.id === s.id)}
            isRunning={liveIds.has(s.id)}
          />
        ))}
      </div>
    </>
  );
}

function CloneCard({
  supported: s,
  live,
  isRunning,
}: {
  supported: SupportedClone;
  live?: CloneInfo;
  isRunning: boolean;
}) {
  const qc = useQueryClient();
  const [seedName, setSeedName] = useState("small-project");
  const [showTools, setShowTools] = useState(false);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["clones"] });

  const startMut = useMutation({
    mutationFn: () => api.clone.start(s.id),
    onSuccess: invalidate,
  });
  const stopMut = useMutation({
    mutationFn: () => api.clone.stop(s.id),
    onSuccess: invalidate,
  });
  const seedMut = useMutation({
    mutationFn: () => api.clone.seed(s.id, seedName),
  });
  const resetMut = useMutation({
    mutationFn: () => api.clone.reset(s.id),
  });
  const toolsQ = useQuery({
    queryKey: ["clones", s.id, "tools"],
    queryFn: () => api.clone.tools(s.id),
    enabled: isRunning && showTools,
    staleTime: 30_000,
  });

  return (
    <div className="card flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-bold text-base flex items-center gap-2">
            {isRunning && (
              <span
                className="inline-block w-2 h-2 bg-accent border border-ink animate-blip"
                title="alive"
              />
            )}
            {s.id}
          </div>
          <div className="text-xs font-mono text-ink-4 dark:text-paper-3 truncate">
            {s.module}
          </div>
        </div>
        <Badge variant={isRunning ? "pass" : "d"}>
          {isRunning ? "running" : "stopped"}
        </Badge>
      </div>

      {isRunning && live && (
        <div className="text-xs space-y-1 font-mono text-ink-3 dark:text-paper-3">
          <div>
            <span className="opacity-60">URL:</span> {live.url}
          </div>
          <div>
            <span className="opacity-60">MCP:</span> {live.mcp_url}
          </div>
          <div>
            <span className="opacity-60">PID:</span> {live.pid} ·{" "}
            <span className="opacity-60">since</span> {fmtTimestamp(live.started_at)}
          </div>
        </div>
      )}

      {isRunning && (
        <div className="flex flex-wrap gap-2 items-center">
          <input
            type="text"
            value={seedName}
            onChange={(e) => setSeedName(e.target.value)}
            placeholder="seed name"
            className="input !h-8 !text-xs flex-1 min-w-[120px]"
          />
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            disabled={seedMut.isPending}
            onClick={() => seedMut.mutate()}
            title="POST /_seed/<name>"
          >
            <Database size={12} />
            {seedMut.isPending ? "…" : "Seed"}
          </button>
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            disabled={resetMut.isPending}
            onClick={() => resetMut.mutate()}
            title="POST /_reset"
          >
            <RotateCcw size={12} />
            {resetMut.isPending ? "…" : "Reset"}
          </button>
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            onClick={() => setShowTools((t) => !t)}
            title="MCP tools/list"
          >
            <Wrench size={12} />
            Tools
          </button>
        </div>
      )}

      {seedMut.data && !seedMut.data.ok && (
        <div className="text-xs text-fail">
          Seed failed: {seedMut.data.error || `HTTP ${seedMut.data.status}`}
        </div>
      )}
      {seedMut.data && seedMut.data.ok && (
        <div className="text-xs text-pass">Seed applied.</div>
      )}
      {resetMut.data && resetMut.data.ok && (
        <div className="text-xs text-pass">Reset applied.</div>
      )}

      {showTools && isRunning && (
        <div className="text-xs space-y-1 max-h-40 overflow-y-auto border-t border-paper-3 dark:border-ink-3 pt-2">
          {toolsQ.isLoading && <span className="text-ink-3">loading tools…</span>}
          {toolsQ.data && toolsQ.data.tools.length === 0 && (
            <span className="text-ink-3 italic">no MCP tools (or twin doesn't expose /mcp)</span>
          )}
          {toolsQ.data?.tools.map((t) => (
            <div key={t.name} className="font-mono">
              <span className="text-ink">{t.name}</span>
              {t.description && (
                <span className="text-ink-3 dark:text-paper-3"> — {t.description.slice(0, 60)}</span>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="flex justify-end gap-2 pt-2 border-t border-paper-3 dark:border-ink-3">
        {isRunning ? (
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            disabled={stopMut.isPending}
            onClick={() => stopMut.mutate()}
          >
            <Square size={12} />
            {stopMut.isPending ? "Stopping…" : "Stop"}
          </button>
        ) : (
          <button
            type="button"
            className="btn-accent !h-8 !text-xs"
            disabled={startMut.isPending}
            onClick={() => startMut.mutate()}
          >
            <Play size={12} />
            {startMut.isPending ? "Starting…" : "Start"}
          </button>
        )}
      </div>

      {(startMut.error || stopMut.error) && (
        <ErrorBox error={startMut.error || stopMut.error} />
      )}
    </div>
  );
}
