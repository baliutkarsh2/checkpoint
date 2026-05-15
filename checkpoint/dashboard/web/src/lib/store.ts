import { useEffect, useState } from "react";

// Tiny pub/sub primitive — avoids pulling in Zustand for two pieces of UI state.
function createStore<T>(initial: T) {
  let value = initial;
  const listeners = new Set<(v: T) => void>();
  return {
    get: () => value,
    set: (next: T | ((prev: T) => T)) => {
      const v =
        typeof next === "function" ? (next as (p: T) => T)(value) : next;
      if (Object.is(v, value)) return;
      value = v;
      listeners.forEach((l) => l(v));
    },
    subscribe: (fn: (v: T) => void) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

function useStore<T>(store: ReturnType<typeof createStore<T>>): T {
  const [v, set] = useState(store.get());
  useEffect(() => {
    const unsub = store.subscribe(set);
    return () => {
      unsub();
    };
  }, [store]);
  return v;
}

// "Compare picks" — the run IDs the user has selected to compare.
const compareStore = createStore<string[]>([]);

export function useComparePicks() {
  return useStore(compareStore);
}
export const comparePicks = {
  toggle(runId: string) {
    compareStore.set((prev) => {
      if (prev.includes(runId)) return prev.filter((p) => p !== runId);
      // Cap at 2 — drop the oldest pick when a third is added.
      const next = [...prev, runId];
      return next.length > 2 ? next.slice(-2) : next;
    });
  },
  clear() {
    compareStore.set([]);
  },
  remove(runId: string) {
    compareStore.set((prev) => prev.filter((p) => p !== runId));
  },
};
