import { useEffect } from "react";

export type KeyHandler = (e: KeyboardEvent) => void;

// Lightweight hotkey hook. Skips events when an input/textarea/contenteditable
// has focus so typing in a search box doesn't trigger nav.
export function useHotkey(
  combo: string,
  handler: KeyHandler,
  options: { allowInInputs?: boolean } = {},
) {
  useEffect(() => {
    const parsed = parseCombo(combo);
    const onKey = (e: KeyboardEvent) => {
      if (!options.allowInInputs && isTypingTarget(e.target)) return;
      if (!matches(parsed, e)) return;
      e.preventDefault();
      handler(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [combo, handler, options.allowInInputs]);
}

// Sequence hotkey: matches a 2-key sequence within `windowMs` ms (vim-style g+r).
export function useSequence(
  seq: [string, string],
  handler: KeyHandler,
  windowMs = 800,
) {
  useEffect(() => {
    let pending: { key: string; t: number } | null = null;
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      const now = Date.now();
      if (
        pending &&
        pending.key === seq[0] &&
        now - pending.t < windowMs &&
        k === seq[1]
      ) {
        e.preventDefault();
        pending = null;
        handler(e);
        return;
      }
      if (k === seq[0]) pending = { key: k, t: now };
      else pending = null;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [seq[0], seq[1], handler, windowMs]);
}

interface ParsedCombo {
  key: string;
  meta: boolean;
  ctrl: boolean;
  shift: boolean;
  alt: boolean;
}

function parseCombo(combo: string): ParsedCombo {
  const parts = combo.toLowerCase().split("+").map((s) => s.trim());
  const key = parts[parts.length - 1];
  return {
    key,
    meta: parts.includes("meta") || parts.includes("cmd"),
    ctrl: parts.includes("ctrl"),
    shift: parts.includes("shift"),
    alt: parts.includes("alt") || parts.includes("opt"),
  };
}

function matches(p: ParsedCombo, e: KeyboardEvent): boolean {
  if (e.key.toLowerCase() !== p.key) return false;
  // Treat "mod" as either meta (mac) or ctrl (everything else).
  const modOK =
    (p.meta || p.ctrl)
      ? e.metaKey || e.ctrlKey
      : !e.metaKey && !e.ctrlKey;
  return modOK && e.shiftKey === p.shift && e.altKey === p.alt;
}

function isTypingTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false;
  const tag = t.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    t.isContentEditable
  );
}
