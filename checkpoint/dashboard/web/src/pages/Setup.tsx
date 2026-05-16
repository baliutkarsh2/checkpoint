import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Activity, Settings, FileCheck, Rocket } from "lucide-react";
import { PageHead } from "@/components/bits";
import Doctor from "./Doctor";
import Config from "./Config";
import Validate from "./Validate";
import OnboardingGuide from "@/components/OnboardingGuide";

/**
 * Setup hub — the dashboard's equivalent of "control panel". Four tabs that
 * keep environment, config, scenario linting, and getting-started guidance
 * within one URL so the top nav stays focused on the daily-driver pages
 * (Runs / Scenarios / Agents / Clones / Reports).
 *
 * Tabs are URL-addressable (?tab=doctor) so docs / links can deep-link.
 */

type Tab = "onboarding" | "doctor" | "config" | "validate";

const TABS: { id: Tab; label: string; icon: typeof Rocket; sub: string }[] = [
  {
    id: "onboarding",
    label: "Onboarding",
    icon: Rocket,
    sub: "How to set up Checkpoint in your repo",
  },
  {
    id: "doctor",
    label: "Doctor",
    icon: Activity,
    sub: "Environment readiness (Docker, ports, key)",
  },
  {
    id: "config",
    label: "Config",
    icon: Settings,
    sub: "Edit your ~/.checkpoint/config.json",
  },
  {
    id: "validate",
    label: "Validate",
    icon: FileCheck,
    sub: "Lint a scenario before running",
  },
];

export default function Setup() {
  const [params, setParams] = useSearchParams();
  const initial = (params.get("tab") as Tab | null) || "onboarding";
  const [tab, setTabState] = useState<Tab>(initial);

  const setTab = (next: Tab) => {
    setTabState(next);
    const p = new URLSearchParams(params);
    p.set("tab", next);
    setParams(p, { replace: true });
  };

  return (
    <>
      <PageHead
        title="Setup"
        sub="Everything you need to get Checkpoint working in your repo, in one place."
      />

      <div className="flex flex-wrap gap-2 mb-6 border-b border-paper-3 dark:border-ink-3">
        {TABS.map((t) => {
          const Icon = t.icon;
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={
                "flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 -mb-px transition " +
                (active
                  ? "border-ink text-ink font-medium dark:border-paper dark:text-paper"
                  : "border-transparent text-ink-3 dark:text-paper-3 hover:text-ink dark:hover:text-paper")
              }
            >
              <Icon size={14} />
              {t.label}
            </button>
          );
        })}
      </div>

      {tab === "onboarding" && <OnboardingGuide />}
      {tab === "doctor" && <Doctor headless />}
      {tab === "config" && <Config headless />}
      {tab === "validate" && <Validate headless />}
    </>
  );
}
