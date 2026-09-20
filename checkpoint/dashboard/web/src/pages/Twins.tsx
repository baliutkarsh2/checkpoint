import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, Square, RotateCcw, Database, Wrench } from "lucide-react";
import { api, type TwinSession, type SupportedTwin } from "@/lib/api";
import { fmtTimestamp } from "@/lib/format";
import {
  Badge,
  ErrorBox,
  Loading,
  PageHead,
  StatTile,
} from "@/components/bits";

/**
 * Long-lived twin sessions — start / stop / seed / reset / inspect them from
 * the dashboard. The CLI's `checkpoint twins` subcommands go through the same
 * session file, so a twin started here is the one the CLI sees.
 */
export default function Twins() {
  const liveQ = useQuery({ queryKey: ["twins"], queryFn: api.twins, refetchInterval: 5000 });
  const supportedQ = useQuery({ queryKey: ["twins", "supported"], queryFn: api.twinsSupported });

  if (liveQ.isLoading || supportedQ.isLoading) return <Loading />;
  if (liveQ.error) return <ErrorBox error={liveQ.error} />;
  if (supportedQ.error) return <ErrorBox error={supportedQ.error} />;

  const live = liveQ.data || [];
  const supported = supportedQ.data || [];
  const liveIds = new Set(live.map((t) => t.id));

  return (
    <>
      <PageHead
        title="Twins"
        sub="Stateful copies of the services your agent calls. Start one to exercise it by hand; scenarios start their own."
      />

      <div className="grid grid-cols-2 md:grid-cols-4 gap-5 mb-7">
        <StatTile label="Available" value={supported.length} />
        <StatTile
          label="Running"
          value={live.length}
          color={live.length > 0 ? "#0ea83b" : undefined}
        />
        <StatTile label="Stopped" value={supported.length - live.length} />
        <StatTile label="Idle" value={live.length === 0 ? "yes" : "no"} />
      </div>

      <div className="section-title">All twins</div>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {supported.map((s) => (
          <TwinCard
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

function TwinCard({
  supported: s,
  live,
  isRunning,
}: {
  supported: SupportedTwin;
  live?: TwinSession;
  isRunning: boolean;
}) {
  const qc = useQueryClient();
  const [seedName, setSeedName] = useState(s.seeds[0] || "");
  const [showTools, setShowTools] = useState(false);

  const invalidate = () => qc.invalidateQueries({ queryKey: ["twins"] });

  const startMut = useMutation({
    mutationFn: () => api.twin.start(s.id),
    onSuccess: invalidate,
  });
  const stopMut = useMutation({
    mutationFn: () => api.twin.stop(s.id),
    onSuccess: invalidate,
  });
  const seedMut = useMutation({
    mutationFn: () => api.twin.seed(s.id, seedName),
  });
  const resetMut = useMutation({
    mutationFn: () => api.twin.reset(s.id),
  });
  const toolsQ = useQuery({
    queryKey: ["twins", s.id, "tools"],
    queryFn: () => api.twin.tools(s.id),
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
            {s.title || s.id}
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
          <select
            value={seedName}
            onChange={(e) => setSeedName(e.target.value)}
            className="input !h-8 !text-xs flex-1 min-w-[120px]"
            aria-label="Seed dataset"
          >
            {s.seeds.length === 0 && <option value="">no seeds</option>}
            {s.seeds.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            disabled={seedMut.isPending || !seedName}
            onClick={() => seedMut.mutate()}
            title="Load this dataset into the running twin"
          >
            <Database size={12} />
            {seedMut.isPending ? "…" : "Seed"}
          </button>
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            disabled={resetMut.isPending}
            onClick={() => resetMut.mutate()}
            title="Empty the twin's state"
          >
            <RotateCcw size={12} />
            {resetMut.isPending ? "…" : "Reset"}
          </button>
          <button
            type="button"
            className="btn-outline !h-8 !text-xs"
            onClick={() => setShowTools((t) => !t)}
            title="What this twin exposes over MCP"
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
            <span className="text-ink-3 italic">this twin exposes no MCP tools</span>
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
