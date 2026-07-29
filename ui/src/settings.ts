/** User customisation, in one place, with a safe fallback for every value.
 *
 *  Everything here is presentation. None of it can change what is stored, what
 *  the pipeline computes, or what codex is asked -- so a layout you designed
 *  yourself cannot break organising, search, or the next ingest. The worst a
 *  corrupt setting can do is fall back to the default.
 */
import { useSyncExternalStore } from "react";

export type Settings = {
  facetOrder: string[];
  facetLabels: Record<string, string>;
  hiddenFacets: string[];
  sidebarWidth: number;
  density: "comfortable" | "compact";
  view: "list" | "map" | "columns";
  showAgeSpine: boolean;
  showTagsInList: boolean;
  accent: string;
  theme: "light" | "dark" | "system";
};

export const DEFAULTS: Settings = {
  facetOrder: ["topic", "venue", "type", "method", "task", "site", "contribution"],
  facetLabels: {},
  hiddenFacets: [],
  sidebarWidth: 256,
  density: "comfortable",
  view: "list",
  showAgeSpine: true,
  showTagsInList: false,
  accent: "#d97757",
  theme: "system",
};

const KEY = "tt-settings-v1";

/** Validate every field independently: one bad value must not discard the rest. */
export function load(): Settings {
  let raw: Partial<Settings> = {};
  try {
    raw = JSON.parse(localStorage.getItem(KEY) ?? "{}") ?? {};
  } catch {
    raw = {};
  }

  const str = (v: unknown, fallback: string) =>
    typeof v === "string" && v.trim() ? v : fallback;

  return {
    facetOrder: Array.isArray(raw.facetOrder) && raw.facetOrder.every((x) => typeof x === "string")
      ? raw.facetOrder
      : DEFAULTS.facetOrder,
    facetLabels:
      raw.facetLabels && typeof raw.facetLabels === "object" && !Array.isArray(raw.facetLabels)
        ? Object.fromEntries(
            Object.entries(raw.facetLabels).filter(
              ([k, v]) => typeof k === "string" && typeof v === "string",
            ),
          )
        : {},
    hiddenFacets: Array.isArray(raw.hiddenFacets) ? raw.hiddenFacets.filter((x) => typeof x === "string") : [],
    // Clamped: a stored width of 0 or 99999 would leave no usable window.
    sidebarWidth: Math.min(680, Math.max(180, Number(raw.sidebarWidth) || DEFAULTS.sidebarWidth)),
    density: raw.density === "compact" ? "compact" : "comfortable",
    view: raw.view === "map" || raw.view === "columns" ? raw.view : "list",
    showAgeSpine: raw.showAgeSpine !== false, // default on
    showTagsInList: raw.showTagsInList === true, // default OFF (keeps the list clean)
    // Only a hex colour is accepted, so a bad value cannot make text invisible.
    accent: /^#[0-9a-f]{6}$/i.test(str(raw.accent, "")) ? (raw.accent as string) : DEFAULTS.accent,
    theme: raw.theme === "light" || raw.theme === "dark" ? raw.theme : "system",
  };
}

// Reactive layer. Density, the age spine and tags-in-list are read while the item
// list renders, so a change must re-render it immediately -- not only after a
// remount (which is why toggling compact "did nothing"). save()/reset() refresh a
// cached snapshot and notify subscribers; useSettings() re-renders on that. Accent
// and theme already applied live via applyTheme; now every appearance setting does.
let snapshot: Settings = load();
const listeners = new Set<() => void>();
function announce(next: Settings) {
  snapshot = next;
  for (const fn of listeners) fn();
}
export function subscribeSettings(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}
export function getSettings(): Settings {
  return snapshot;
}
/** Live-updating settings for components: re-renders the instant any setting changes. */
export function useSettings(): Settings {
  return useSyncExternalStore(subscribeSettings, getSettings, getSettings);
}

export function save(patch: Partial<Settings>): Settings {
  const next = { ...load(), ...patch };
  localStorage.setItem(KEY, JSON.stringify(next));
  applyTheme(next);
  announce(next);
  return next;
}

export function reset(): Settings {
  // "Reset all" must clear every appearance/layout key, not just the settings blob:
  // the sidebar renames, facet order, expanded set, pins, view and collapse all lived
  // in sibling tt-* keys and used to survive a reset. User DATA (saved collections,
  // Ask history) is deliberately left untouched.
  for (const k of [
    KEY,
    "tt-tag-labels",
    "tt-facet-order",
    "tt-tree-open-v3",
    "tt-tree-pins",
    "tt-view",
    "tt-sidebar-collapsed",
  ]) {
    localStorage.removeItem(k);
  }
  applyTheme(DEFAULTS);
  announce(DEFAULTS);
  return DEFAULTS;
}

/** Push the parts that are pure CSS onto the document. */
export function applyTheme(s: Settings) {
  const root = document.documentElement;
  root.style.setProperty("--color-accent", s.accent);
  const dark =
    s.theme === "dark" ||
    (s.theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  root.classList.toggle("dark", dark);
}
