import { useEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  BookOpen, CalendarClock, Copy, Download, Layers, Moon, PanelLeftClose,
  PanelLeftOpen, Search, Settings as SettingsIcon, Sparkles, Sun,
  Tags as TagsIcon, Wand2,
} from "lucide-react";

import { api } from "./api";
import { load as loadSettings, save as saveSettings } from "./settings";
import { usePaneWidth } from "./usePaneWidth";
import { Workspace } from "./pages/Workspace";
import { History } from "./pages/History";
import { Ask } from "./pages/Ask";
import { Dupes } from "./pages/Dupes";
import { Tags } from "./pages/Tags";
import { SettingsPage } from "./pages/Settings";
import { JobBar, startJob } from "./pages/Jobs";
import { TagTree } from "./components/TagTree";
import { SectionLabel, CairnMark } from "./ui";
import { Toaster } from "./components/Toaster";
import { Collections } from "./components/Collections";

// Three places, not seven. Documents is a bucket, Browse and Tags are views of
// the same tree, and Duplicates is a maintenance task -- none of them was a
// destination, they were just navigation pretending to be structure.
const NAV = [
  { to: "/", label: "Workspace", icon: BookOpen, end: true },
  { to: "/history", label: "History", icon: CalendarClock, end: false },
  { to: "/ask", label: "Ask", icon: Sparkles, end: false },
];

/** Reload once when the server reports a bundle newer than the one running.
 *  Without this, every rebuild leaves the tab executing stale JavaScript. */
function useBuildWatcher() {
  const seen = useRef<string | null>(null);
  const { data } = useQuery({
    queryKey: ["build"],
    queryFn: api.build,
    refetchInterval: 10_000,
  });
  useEffect(() => {
    if (!data) return;
    if (seen.current && seen.current !== data.build) {
      window.location.reload();
    }
    seen.current = data.build;
  }, [data?.build]);
}

const LIBRARY_QUERY_KEYS = new Set([
  "stats",
  "tags",
  "items",
  "browse-items",
  "history",
  "digest",
  "map",
  "item",
  "related",
  "dupes",
  "triage",
]);

/** Notice extension/agent writes without polling every library endpoint.
 *
 * Invalidation deliberately uses refetchType "none": cached screens are marked
 * stale, then React Query refreshes them on focus or the next mount while showing
 * the cached result immediately. A long organizer job therefore cannot make the
 * active screen refetch every few seconds.
 */
function useLibraryRevision() {
  const queryClient = useQueryClient();
  const seen = useRef<string | null>(null);
  const { data } = useQuery({
    queryKey: ["revision"],
    queryFn: api.revision,
    refetchInterval: 3_000,
    refetchIntervalInBackground: false,
    staleTime: 0,
  });
  useEffect(() => {
    if (!data) return;
    if (seen.current && seen.current !== data.revision) {
      void queryClient.invalidateQueries({
        predicate: (query) => {
          const root = query.queryKey[0];
          return typeof root === "string" && LIBRARY_QUERY_KEYS.has(root);
        },
        refetchType: "none",
      });
    }
    seen.current = data.revision;
  }, [data?.revision, queryClient]);
}

// Theme lives in ONE place: the settings store (tt-settings-v1). This header
// toggle reads its initial state from there and writes back through save(), which
// re-applies accent + dark class -- so the Settings page and this toggle can no
// longer disagree (the old parallel tt-theme key is retired).
function useDarkMode() {
  const [dark, setDark] = useState(() => {
    const s = loadSettings();
    return (
      s.theme === "dark" ||
      (s.theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches)
    );
  });
  useEffect(() => {
    saveSettings({ theme: dark ? "dark" : "light" }); // save() also applies the theme
  }, [dark]);
  return [dark, setDark] as const;
}

export default function App() {
  useBuildWatcher();
  useLibraryRevision();
  const [dark, setDark] = useDarkMode();
  const { width: sidebarWidth, onPointerDown: startResize, reset: resetSidebar } =
    usePaneWidth("tt-sidebar-w", 256, { min: 200, max: 640 });
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("tt-sidebar-collapsed") === "1",
  );
  useEffect(() => {
    localStorage.setItem("tt-sidebar-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);
  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
      if (e.key === "Escape") setPaletteOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-6 border-b border-line/70 px-6 py-3">
        <span
          className="flex items-center gap-2 font-display text-[22px] leading-none"
          title="marks the path through what you've read"
        >
          <CairnMark size={22} className="text-accent" />
          Cairn
        </span>
        <button
          onClick={() => setPaletteOpen(true)}
          className="group flex items-center gap-2 rounded-lg border border-line px-3 py-1.5 text-sm text-muted transition-colors hover:border-accent hover:text-ink"
        >
          <Search size={14} />
          Search or jump to…
          <kbd className="ml-6 rounded border border-line px-1 font-mono text-[10px]">⌘K</kbd>
        </button>
        <div className="ml-auto flex items-center gap-3">
          <JobBar />
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              clsx(
                "rounded-lg p-2 transition-colors hover:bg-surface hover:text-ink",
                isActive ? "text-accent" : "text-muted",
              )
            }
            aria-label="Settings"
          >
            <SettingsIcon size={16} />
          </NavLink>
          <button
            onClick={() => setDark(!dark)}
            className="rounded-lg p-2 text-muted transition-colors hover:bg-surface hover:text-ink"
            aria-label="Toggle theme"
          >
            {dark ? <Sun size={16} /> : <Moon size={16} />}
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside
          className="relative flex shrink-0 flex-col border-r border-line/70"
          style={{ width: collapsed ? 60 : sidebarWidth }}
        >
          <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-2 py-4">
            <nav className="space-y-0.5">
              {NAV.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  title={collapsed ? label : undefined}
                  className={({ isActive }) =>
                    clsx(
                      "flex items-center gap-2.5 rounded-lg py-2 text-sm transition-colors",
                      collapsed ? "justify-center px-0" : "px-3",
                      isActive
                        ? "bg-accent-soft font-medium text-accent"
                        : "text-muted hover:bg-surface hover:text-ink",
                    )
                  }
                >
                  <Icon size={16} className="shrink-0" />
                  {!collapsed && label}
                  {!collapsed && label === "Workspace" && stats && (
                    <span className="ml-auto font-mono text-[11px] opacity-70">
                      {stats.total}
                    </span>
                  )}
                </NavLink>
              ))}
            </nav>

            {!collapsed && (
              <>
                <Collections />

                <TagBrowser />

                {stats && (
                  <div className="mt-7 px-3">
                    <SectionLabel>Library</SectionLabel>
                    <p className="font-mono text-[11px] leading-relaxed text-muted">
                      {stats.total} items
                      <br />
                      {stats.untagged} untagged
                      <br />
                      {Object.keys(stats.sources).length} sources
                    </p>
                    {/* Newly-saved items are tagged automatically on the poll timer;
                        this drains the queue immediately if you don't want to wait. */}
                    {stats.queued > 0 && (
                      <button
                        onClick={() => startJob("tag-queue")}
                        title="Tag the newly-saved items now, instead of waiting for the next auto-tag cycle"
                        className="mt-2 flex items-center gap-1.5 rounded-md border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent hover:text-accent"
                      >
                        <Wand2 size={11} />
                        Tag {stats.queued} queued now
                      </button>
                    )}
                  </div>
                )}
              </>
            )}
          </div>

          {/* Collapse to an icon rail, or expand back. The choice persists. */}
          <button
            onClick={() => setCollapsed((v) => !v)}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={clsx(
              "flex items-center gap-2 border-t border-line/60 py-2.5 text-[12px] text-muted transition-colors hover:bg-surface hover:text-ink",
              collapsed ? "justify-center px-0" : "px-4",
            )}
          >
            {collapsed ? (
              <PanelLeftOpen size={16} />
            ) : (
              <>
                <PanelLeftClose size={16} /> Collapse
              </>
            )}
          </button>
        </aside>

        {!collapsed && (
          <div
            onPointerDown={startResize}
            onDoubleClick={resetSidebar}
            title="Drag to resize"
            className="group relative w-[3px] shrink-0 cursor-col-resize bg-transparent transition-colors hover:bg-accent/40"
          >
            <span className="absolute inset-y-0 -left-1 -right-1" />
          </div>
        )}

        <main className="min-w-0 flex-1 overflow-hidden">
          <AnimatedRoutes />
        </main>
      </div>

      {paletteOpen && <Palette onClose={() => setPaletteOpen(false)} />}
      <Toaster />
    </div>
  );
}

/** Routes wrapped so each page fades-and-lifts in when you navigate to it.
 *  Keyed on pathname only, so changing a filter (a query param) on the
 *  Workspace does not re-run the animation or unmount the list. */
function AnimatedRoutes() {
  const location = useLocation();
  return (
    // h-full (not min-h-full) preserves the height chain so the Workspace's list
    // and detail panel each scroll on their own; other pages scroll in here.
    <div key={location.pathname} className="page-enter h-full overflow-y-auto">
      <Routes location={location}>
        <Route path="/" element={<Workspace />} />
        <Route path="/history" element={<History />} />
        <Route path="/tags" element={<Tags />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/dupes" element={<Dupes />} />
        <Route path="/ask" element={<Ask />} />
      </Routes>
    </div>
  );
}

function TagBrowser() {
  const { data } = useQuery({ queryKey: ["tags"], queryFn: api.tags });
  if (!data?.tags.length) return null;
  // collection/ lives in its own Collections section above, so keep it out of the tree.
  const nodes = data.tags.filter((n) => n.name.split("/")[0] !== "collection");
  if (!nodes.length) return null;
  return (
    <div className="mt-6 px-3">
      <TagTree nodes={nodes} />
    </div>
  );
}

/** ⌘K palette: searches the library for real, and runs commands.
 *
 *  It previously only filtered a static list of command labels, so typing a
 *  paper title matched nothing -- including the "search the library" action
 *  itself, whose label never contains what you typed. Now the query hits the
 *  index and the results are the items.
 */
function Palette({ onClose }: { onClose: () => void }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [cursor, setCursor] = useState(0);
  const navigate = useNavigate();

  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 140);
    return () => clearTimeout(t);
  }, [q]);

  const { data, isFetching } = useQuery({
    queryKey: ["palette", debounced],
    queryFn: () => api.items({ q: debounced, bucket: "", limit: 8 }),
    enabled: debounced.length > 1,
  });

  const commands = [
    ...NAV.map((n) => ({
      label: `Go to ${n.label}`,
      icon: n.icon,
      run: () => navigate(n.to),
    })),
    { label: "Manage tags: rename, merge, delete", icon: TagsIcon, run: () => navigate("/tags") },
    { label: "Settings: accent, theme, density", icon: TagsIcon, run: () => navigate("/settings") },
    { label: "Find duplicate items", icon: Copy, run: () => navigate("/dupes") },
    { label: "Ingest open Chrome tabs", icon: Layers, run: () => startJob("ingest") },
    { label: "Re-organize: propose → build hierarchy → tag everything", icon: Wand2, run: () => startJob("reorganize") },
    { label: "Backfill missing metadata", icon: Download, run: () => startJob("backfill") },
    { label: "Back up the database", icon: Download, run: () => startJob("backup") },
  ].filter((c) => !debounced || c.label.toLowerCase().includes(debounced.toLowerCase()));

  const results = data?.items ?? [];
  // Items first when searching: that is what the box is for.
  const rows = [
    ...results.map((item) => ({
      kind: "item" as const,
      key: `i${item.id}`,
      label: item.title || item.canonical_url,
      hint: [item.source, item.year].filter(Boolean).join(" · "),
      run: () => navigate(`/?q=${encodeURIComponent(item.title ?? "")}`),
    })),
    ...commands.map((c) => ({
      kind: "cmd" as const,
      key: c.label,
      label: c.label,
      hint: "",
      icon: c.icon,
      run: c.run,
    })),
    ...(debounced
      ? [{
          kind: "cmd" as const,
          key: "all",
          label: `See all results for “${debounced}”`,
          hint: data ? `${data.total} items` : "",
          icon: Search,
          run: () => navigate(`/?q=${encodeURIComponent(debounced)}`),
        }]
      : []),
  ];

  useEffect(() => setCursor(0), [debounced]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/25 pt-[12vh]"
      onClick={onClose}
    >
      <div
        className="rise w-[600px] overflow-hidden rounded-xl border border-line bg-surface shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-4">
          <Search size={15} className="shrink-0 text-muted" />
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setCursor((c) => Math.min(c + 1, rows.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setCursor((c) => Math.max(c - 1, 0));
              } else if (e.key === "Enter" && rows[cursor]) {
                rows[cursor].run();
                onClose();
              }
            }}
            placeholder="Search papers, or type a command…"
            className="w-full bg-transparent py-3.5 text-[15px] outline-none placeholder:text-muted"
          />
          {isFetching && <span className="thinking-dot shrink-0 text-accent">●</span>}
        </div>

        <div className="max-h-[52vh] overflow-y-auto py-1.5">
          {rows.map((row, i) => (
            <button
              key={row.key}
              onMouseEnter={() => setCursor(i)}
              onClick={() => {
                row.run();
                onClose();
              }}
              className={clsx(
                "flex w-full items-center gap-3 px-4 py-2 text-left text-sm transition-colors",
                i === cursor ? "bg-accent-soft text-accent" : "text-ink hover:bg-paper",
              )}
            >
              {row.kind === "item" ? (
                <BookOpen size={14} className="shrink-0 opacity-50" />
              ) : (
                <row.icon size={14} className="shrink-0 opacity-60" />
              )}
              <span className="min-w-0 flex-1 truncate">{row.label}</span>
              {row.hint && (
                <span className="shrink-0 font-mono text-[11px] opacity-55">{row.hint}</span>
              )}
            </button>
          ))}

          {!rows.length && (
            <p className="px-4 py-6 text-center text-sm text-muted">
              {debounced.length > 1 ? `Nothing matches “${debounced}”.` : "Type to search."}
            </p>
          )}
        </div>

        <div className="border-t border-line px-4 py-2 font-mono text-[10.5px] text-muted">
          ↑↓ move · ⏎ open · esc close
        </div>
      </div>
    </div>
  );
}
