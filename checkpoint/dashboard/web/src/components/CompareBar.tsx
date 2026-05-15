import { useNavigate } from "react-router-dom";
import { ArrowRight, X } from "lucide-react";
import { comparePicks, useComparePicks } from "@/lib/store";
import { shortId } from "@/lib/format";

export default function CompareBar() {
  const picks = useComparePicks();
  const navigate = useNavigate();
  if (picks.length === 0) return null;

  const ready = picks.length === 2;

  return (
    <div className="fixed left-1/2 bottom-6 -translate-x-1/2 z-[90] flex items-center gap-3.5 border border-ink bg-ink text-paper shadow-offset px-4 py-2.5 font-mono text-xs">
      <div className="flex gap-2.5 items-center text-paper/70">
        {picks[0] && <code className="text-accent">{shortId(picks[0])}</code>}
        {picks[1] ? (
          <>
            <ArrowRight size={12} className="text-ink-4" />
            <code className="text-accent">{shortId(picks[1])}</code>
          </>
        ) : (
          <span className="text-ink-4">pick 1 more to compare…</span>
        )}
      </div>
      <button
        type="button"
        disabled={!ready}
        className={`btn-accent !h-7 !px-3 !text-xs ${ready ? "" : "opacity-40 cursor-not-allowed"}`}
        onClick={() => ready && navigate(`/compare?a=${picks[0]}&b=${picks[1]}`)}
      >
        Compare
      </button>
      <button
        type="button"
        className="text-paper/50 hover:text-accent px-2 cursor-pointer"
        onClick={() => comparePicks.clear()}
        aria-label="Clear compare picks"
      >
        <X size={14} />
      </button>
    </div>
  );
}
