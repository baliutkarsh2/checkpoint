import { ReactNode, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Sun, Moon, Search } from "lucide-react";
import { api } from "@/lib/api";
import { useTheme } from "@/lib/theme";
import { useHotkey, useSequence } from "@/lib/keyboard";
import CommandPalette from "./CommandPalette";
import CompareBar from "./CompareBar";
import { useEventSource } from "@/lib/sse";
import { useQueryClient } from "@tanstack/react-query";

export default function Layout({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [, , toggleTheme] = useTheme();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const qc = useQueryClient();

  const { data: meta } = useQuery({
    queryKey: ["meta"],
    queryFn: api.meta,
    staleTime: 60_000,
  });

  // Live updates: when a run completes or a clone changes, invalidate the
  // affected queries so any open page refetches automatically. This is the
  // "feels alive" wiring that the old Jinja dashboard couldn't do.
  useEventSource("/api/events", {
    "run.created": () => qc.invalidateQueries({ queryKey: ["runs"] }),
    "run.updated": () => {
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["report"] });
      qc.invalidateQueries({ queryKey: ["summary"] });
    },
    "clones.changed": () => qc.invalidateQueries({ queryKey: ["clones"] }),
    "job.updated": () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });

  // Global hotkeys.
  useHotkey("mod+k", () => setPaletteOpen(true));
  useHotkey("/", () => {
    const el = document.querySelector<HTMLInputElement>("input.input");
    el?.focus();
    el?.select();
  });
  useSequence(["g", "r"], () => navigate("/"));
  useSequence(["g", "s"], () => navigate("/scenarios"));
  useSequence(["g", "p"], () => navigate("/report"));
  useHotkey("?", () => setPaletteOpen(true));
  useHotkey("d", toggleTheme);

  return (
    <div className="min-h-screen flex flex-col">
      {/* Util bar */}
      <div className="bg-ink text-paper font-mono text-[11px] tracking-wide h-7 relative z-[60]">
        <div className="max-w-[1280px] mx-auto px-8 flex items-center justify-between h-full">
          <div className="flex gap-4 items-center">
            <span className="w-1.5 h-1.5 rounded-full bg-accent shadow-[0_0_8px_#2dff5c]" />
            <span>checkpoint dashboard</span>
          </div>
          <div className="flex gap-4 text-paper/60 max-md:hidden">
            <span>
              JUDGE <b className="text-paper font-medium">{meta?.judge_model_default || "—"}</b>
            </span>
            <span>
              HOST <b className="text-paper font-medium">{meta?.host || "127.0.0.1"}</b>
            </span>
            <span>
              BUILD <b className="text-paper font-medium">v{meta?.version || "—"}</b>
            </span>
          </div>
        </div>
      </div>

      {/* Sticky nav */}
      <nav className="sticky top-0 z-[100] h-16 border-b border-ink bg-paper/85 backdrop-blur-md dark:bg-ink-2/85 dark:border-paper-3">
        <div className="max-w-[1280px] mx-auto px-8 flex items-center h-full">
          <Link to="/" className="font-bold text-[15px] flex items-center gap-2.5">
            <span className="w-2.5 h-2.5 bg-accent border border-ink animate-blip" />
            <span>checkpoint</span>
          </Link>
          <div className="flex gap-6 ml-12 flex-1">
            <NavItem to="/">Runs</NavItem>
            <NavItem to="/scenarios">Scenarios</NavItem>
            <NavItem to="/report">Report</NavItem>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn-ghost"
              onClick={() => setPaletteOpen(true)}
              title="Open command palette (⌘K)"
            >
              <Search size={14} />
              <span className="text-xs text-ink-3 dark:text-paper-3">
                Search
              </span>
              <span className="kbd ml-1">⌘K</span>
            </button>
            <button
              type="button"
              className="btn-ghost"
              onClick={toggleTheme}
              title="Toggle theme (d)"
              aria-label="Toggle theme"
            >
              <Sun size={14} className="dark:hidden" />
              <Moon size={14} className="hidden dark:block" />
            </button>
          </div>
        </div>
      </nav>

      {/* Page */}
      <main className="flex-1 max-w-[1280px] mx-auto w-full px-8 py-10 relative z-[1]">
        {children}
      </main>

      <footer className="mt-20 py-6 border-t border-paper-3 dark:border-ink-3 text-ink-3 dark:text-paper-3 text-[11px] uppercase tracking-wider font-mono">
        <div className="max-w-[1280px] mx-auto px-8 flex justify-between">
          <span>checkpoint dashboard · v{meta?.version || "dev"}</span>
          <span>
            <span className="kbd">?</span> for help · <span className="kbd">⌘K</span> to search
          </span>
        </div>
      </footer>

      <CompareBar />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}

function NavItem({ to, children }: { to: string; children: ReactNode }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        `text-[13.5px] font-medium py-1 border-b-2 ${isActive ? "border-ink dark:border-paper" : "border-transparent hover:text-ink-3 dark:hover:text-paper-3"}`
      }
    >
      {children}
    </NavLink>
  );
}
