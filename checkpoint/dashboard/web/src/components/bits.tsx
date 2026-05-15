import type { ReactNode } from "react";
import { scoreColor } from "@/lib/format";

export function Badge({
  children,
  variant = "d",
}: {
  children: ReactNode;
  variant?: "pass" | "fail" | "warn" | "info" | "judge" | "d";
}) {
  return <span className={`badge badge-${variant}`}>{children}</span>;
}

export function ScoreBar({ score }: { score: number }) {
  const color = scoreColor(score);
  return (
    <span className="inline-flex items-center gap-2">
      <span className="font-mono font-semibold" style={{ color }}>
        {Math.round(score)}
      </span>
      <span className="score-bar">
        <span
          style={{
            width: `${Math.max(0, Math.min(100, score))}%`,
            background: color,
          }}
        />
      </span>
    </span>
  );
}

export function StatTile({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  color?: string;
}) {
  return (
    <div className="card">
      <div className="card-title">{label}</div>
      <div className="stat-num" style={color ? { color } : undefined}>
        {value}
      </div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: ReactNode;
}) {
  return (
    <div className="card text-center text-ink-3 dark:text-paper-3">
      <div className="font-mono text-sm uppercase tracking-wider">{title}</div>
      {hint && <div className="text-sm mt-2">{hint}</div>}
    </div>
  );
}

export function Sparkline({ data }: { data: number[] }) {
  if (data.length === 0) return null;
  const max = 100;
  return (
    <div className="flex items-end gap-px h-12">
      {data.map((v, i) => (
        <div
          key={i}
          className="flex-1 min-w-[3px] border border-ink"
          style={{
            height: `${Math.max(5, (v / max) * 100)}%`,
            background: scoreColor(v),
          }}
          title={`run ${i + 1}: ${Math.round(v)}`}
        />
      ))}
    </div>
  );
}

export function PageHead({
  title,
  sub,
  right,
}: {
  title: string;
  sub?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-end justify-between mb-7 gap-4 flex-wrap">
      <div>
        <h1 className="page-title">{title}</h1>
        {sub && <div className="page-sub !mb-0">{sub}</div>}
      </div>
      {right && <div>{right}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="text-ink-3 dark:text-paper-3 text-sm font-mono py-8 text-center">
      {label}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="card border-fail bg-fail-soft text-fail">
      <div className="card-title !text-fail">Error</div>
      <div className="font-mono text-xs">{msg}</div>
    </div>
  );
}
