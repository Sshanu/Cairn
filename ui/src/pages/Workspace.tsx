import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import clsx from "clsx";
import {
  Columns3, Combine, Download, ExternalLink, LayoutList, Pencil, Quote, Search, Sparkles, Trash2, X,
} from "lucide-react";

import { usePaneWidth } from "../usePaneWidth";
import { api, type Item } from "../api";
import { Button, Empty, ListSkeleton, SectionLabel, Tag, spine } from "../ui";
import { useSettings } from "../settings";
import { BibtexPanel } from "../components/BibtexPanel";
import { RelatedPanel } from "../components/RelatedPanel";
import { TagInput } from "../components/TagInput";
import { toast } from "../components/Toaster";
import { Browse } from "./Browse";

/** Export extracted BibTeX for a selection or the current filter as a .bib file,
 *  and report how many had no BibTeX (never fabricated). */
async function exportBib(body: Parameters<typeof api.exportBibtex>[0], label: string) {
  // The export resolves any missing BibTeX over the network, so it can take a few
  // seconds -- give immediate feedback rather than a frozen button.
  toast("Resolving BibTeX…");
  try {
    const r = await api.exportBibtex(body);
    if (!r.with_bibtex) {
      toast(`No BibTeX in these ${r.total} item${r.total === 1 ? "" : "s"}`);
      return;
    }
    const url = URL.createObjectURL(new Blob([r.bibtex], { type: "application/x-bibtex" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `cairn-${label}.bib`.replace(/[^\w.-]+/g, "-");
    a.click();
    URL.revokeObjectURL(url);
    toast(
      `Exported ${r.with_bibtex} of ${r.total}` +
        (r.missing.length ? ` · ${r.missing.length} missing BibTeX` : ""),
    );
  } catch {
    toast("Export failed");
  }
}

/** A plain-text citation from an item's fields, for pasting outside LaTeX. */
function citationText(item: Item): string {
  const authors = (item.authors ?? "").split(/[,;]/)[0]?.trim();
  const bits = [
    authors ? `${authors} et al.` : null,
    item.title ? `"${item.title}."` : null,
    item.venue || null,
    item.year ? String(item.year) : null,
  ].filter(Boolean);
  return bits.join(" ");
}

const STATUSES = ["unread", "reading", "read", "archived"];
const SORTS = [
  { id: "relevance", label: "Relevance" },
  { id: "age", label: "Oldest first seen" },
  { id: "newest", label: "Recently saved" },
];
type View = "list" | "columns";

/** A long "Proceedings of the … Conference on … (IWSLT 2025)" is noise in a list
 *  row. Prefer the trailing abbreviation in parentheses; otherwise truncate. */
function shortVenue(venue: string | null): string | null {
  if (!venue) return null;
  const abbrev = venue.match(/\(([^)]{2,32})\)\s*$/);
  if (abbrev) return abbrev[1];
  return venue.length > 42 ? venue.slice(0, 42).trimEnd() + "…" : venue;
}

/** One workspace: tree on the left, items in the middle, detail on the right.
 *
 *  Seven separate destinations made a small tool feel like a suite -- Library,
 *  Documents, Browse, Tags, Triage and Duplicates were all the same three
 *  questions (what do I have, what is it about, what should I read) wearing
 *  different navigation. They are views and filters here, not places.
 */
export function Workspace() {
  const [params, setParams] = useSearchParams();
  const [view, setView] = useState<View>(
    () => (localStorage.getItem("tt-view") as View) || "list",
  );
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Deep-linkable so another page (History) can open an item's detail here just
  // by navigating to /?item=<id>.
  const openItem = params.get("item") ? Number(params.get("item")) : null;
  const setOpenItem = (id: number | null) => setParam("item", id ? String(id) : "");
  const [draft, setDraft] = useState(params.get("q") ?? "");
  const searchRef = useRef<HTMLInputElement>(null);

  const q = params.get("q") ?? "";
  const tag = params.get("tag") ?? "";
  const status = params.get("status") ?? "";
  // Sidebar tag counts are library-wide (every bucket), so a tag click must
  // search every bucket too — otherwise a docs-only tag like site/google shows
  // "6" yet lands on an empty library view. With no tag, default to library.
  const bucket = params.get("bucket") ?? (tag ? "" : "library");
  const filed = params.get("filed") ?? "";  // "" | "none" | "no-topic"
  const sort = params.get("sort") ?? "relevance";

  useEffect(() => localStorage.setItem("tt-view", view), [view]);

  useEffect(() => {
    const t = setTimeout(() => {
      const next = new URLSearchParams(params);
      if (draft) {
        next.set("q", draft);
        next.delete("tag");
      } else next.delete("q");
      if (next.toString() !== params.toString()) setParams(next, { replace: true });
    }, 170);
    return () => clearTimeout(t);
  }, [draft]);

  // Keep the search box in sync when q changes from OUTSIDE the box -- the command
  // palette, a saved collection, or browser Back -- otherwise the input stayed
  // empty/stale while the list was filtered. Only overwrite when they differ.
  useEffect(() => {
    setDraft((d) => (d === q ? d : q));
  }, [q]);

  const [focusIndex, setFocusIndex] = useState(-1);
  // Read the latest rows/focus inside the key handler without rebinding it.
  const nav = useRef({ rows: [] as Item[], focusIndex: -1 });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).matches("input,textarea,[contenteditable]")) return;
      const { rows, focusIndex: fi } = nav.current;
      if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        setFocusIndex((i) => Math.min((i < 0 ? -1 : i) + 1, rows.length - 1));
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        setFocusIndex((i) => Math.max((i < 0 ? 0 : i) - 1, 0));
      } else if (e.key === "Enter" && fi >= 0 && rows[fi]) {
        setOpenItem(rows[fi].id);
      } else if (e.key === "o" && fi >= 0 && rows[fi]) {
        window.open(rows[fi].canonical_url, "_blank", "noopener");
      } else if (e.key === "Escape") {
        setOpenItem(null);
        setFocusIndex(-1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    setParams(next, { replace: true });
  };

  const { data: stats } = useQuery({ queryKey: ["stats"], queryFn: api.stats });
  // Paged so the list stays fast on a big library: 120 at a time, more fetched
  // as you scroll near the end (see ItemList onEndReached).
  const items = useInfiniteQuery({
    queryKey: ["items", { q, tag, status, sort, bucket, filed }],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.items({
        q, tag, status, sort, bucket, limit: 120, offset: pageParam,
        untagged: filed === "none",
        no_topic: filed === "no-topic",
      }),
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return loaded < last.total ? loaded : undefined;
    },
  });

  const rows = items.data?.pages.flatMap((p) => p.items) ?? [];
  const total = items.data?.pages[0]?.total ?? 0;
  nav.current = { rows, focusIndex };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* One toolbar carries every control that used to be a separate screen. */}
      <div className="flex flex-wrap items-center gap-2 border-b border-line/50 px-5 py-3">
        <div className="relative min-w-[240px] flex-1">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <input
            ref={searchRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Search everything…   /"
            className="w-full rounded-lg border border-line bg-surface py-1.5 pl-9 pr-3 text-sm outline-none transition-colors placeholder:text-muted focus:border-accent"
          />
        </div>

        <div className="flex overflow-hidden rounded-lg border border-line">
          {(["library", "docs"] as const).map((b) => (
            <button
              key={b}
              // Always set an EXPLICIT bucket. Setting "" used to collapse to "all
              // buckets" whenever a tag was active, so the Library button did nothing
              // and neither toggle highlighted. Explicit values let it narrow again;
              // a tag click still starts from all buckets (it drops the bucket param).
              onClick={() => setParam("bucket", b)}
              className={clsx(
                "px-2.5 py-1.5 text-[12.5px] transition-colors",
                bucket === b ? "bg-accent text-white" : "text-muted hover:text-ink",
              )}
            >
              {b === "library" ? "Library" : "Documents"}
            </button>
          ))}
        </div>

        <select
          value={filed}
          onChange={(e) => setParam("filed", e.target.value)}
          title="Items the pipeline could not file"
          className="rounded-lg border border-line bg-surface px-2 py-1.5 text-[12.5px] text-muted outline-none focus:border-accent"
        >
          <option value="">All items</option>
          <option value="none">Unassigned{stats ? ` (${stats.untagged})` : ""}</option>
          <option value="no-topic">No topic{stats ? ` (${stats.no_topic})` : ""}</option>
        </select>

        <select
          value={status}
          onChange={(e) => setParam("status", e.target.value)}
          className="rounded-lg border border-line bg-surface px-2 py-1.5 text-[12.5px] text-muted outline-none focus:border-accent"
        >
          <option value="">Any status</option>
          {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>

        <select
          value={sort}
          onChange={(e) => setParam("sort", e.target.value)}
          className="rounded-lg border border-line bg-surface px-2 py-1.5 text-[12.5px] text-muted outline-none focus:border-accent"
        >
          {SORTS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>

        <div className="flex overflow-hidden rounded-lg border border-line">
          {([
            ["list", LayoutList, "List"],
            ["columns", Columns3, "Columns"],
          ] as const).map(([id, Icon, label]) => (
            <button
              key={id}
              onClick={() => setView(id)}
              title={label}
              className={clsx(
                "px-2 py-1.5 transition-colors",
                view === id ? "bg-accent text-white" : "text-muted hover:text-ink",
              )}
            >
              <Icon size={14} />
            </button>
          ))}
        </div>
      </div>

      {(tag || q || status || filed) && (
        <div className="flex items-center gap-2 px-5 py-1.5">
          {tag && (
            <button
              onClick={() => setParam("tag", "")}
              className="inline-flex items-center gap-1 rounded-md bg-accent-soft px-2 py-0.5 font-mono text-[11.5px] text-accent"
            >
              {tag} <X size={10} />
            </button>
          )}
          {q && <span className="font-mono text-[11.5px] text-muted">“{q}”</span>}
          {filed && (
            <button
              onClick={() => setParam("filed", "")}
              className="inline-flex items-center gap-1 rounded-md bg-accent-soft px-2 py-0.5 font-mono text-[11.5px] text-accent"
            >
              {filed === "none" ? "unassigned" : "no topic"} <X size={10} />
            </button>
          )}
          <button
            onClick={() =>
              exportBib(
                {
                  q, tag, status,
                  bucket: bucket === "library" ? "" : bucket,
                  untagged: filed === "none",
                  no_topic: filed === "no-topic",
                },
                tag ? tag.split("/").pop()! : q || "library",
              )
            }
            title="Export BibTeX for everything under this filter"
            className="ml-auto inline-flex items-center gap-1 rounded-md border border-line px-2 py-0.5 text-[11.5px] text-muted transition-colors hover:border-accent hover:text-accent"
          >
            <Download size={11} /> Export .bib
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1">
          {view === "columns" ? (
            <Browse />
          ) : items.isLoading ? (
            <ListSkeleton rows={10} />
          ) : rows.length === 0 ? (
            <Empty
              title="Nothing here."
              hint={
                tag && bucket === "docs"
                  ? `No document carries ${tag}. Switch to Library to see the rest.`
                  : filed === "none"
                    ? "Nothing unassigned — every item carries a topic, method or task."
                    : filed === "no-topic"
                      ? "Every item has a topic."
                  : q || tag
                    ? "No item matches these filters."
                    : "Run ingest from ⌘K."
              }
            />
          ) : (
            <ItemList
              items={rows}
              total={total}
              onEndReached={() => {
                if (items.hasNextPage && !items.isFetchingNextPage) items.fetchNextPage();
              }}
              selected={selected}
              focusIndex={focusIndex}
              onSelect={(id, on) => {
                const next = new Set(selected);
                on ? next.add(id) : next.delete(id);
                setSelected(next);
              }}
              onSelectAll={(on) =>
                setSelected(on ? new Set(rows.map((r) => r.id)) : new Set())
              }
              onOpen={setOpenItem}
              bulkBar={
                selected.size > 0 && (
                  <BulkBar
                    ids={[...selected]}
                    onDone={() => {
                      setSelected(new Set());
                      items.refetch();
                    }}
                  />
                )
              }
            />
          )}
        </div>

        {openItem !== null && (
          // key forces a fresh mount per item -- without it, Detail's local notes/
          // edit state carried over and one paper's note could be saved onto another.
          <Detail
            key={openItem}
            id={openItem}
            onClose={() => setOpenItem(null)}
            onTag={(t) => setParam("tag", t)}
          />
        )}
      </div>
    </div>
  );
}

function ItemList({
  items, total, onEndReached, selected, focusIndex, onSelect, onSelectAll, onOpen, bulkBar,
}: {
  items: Item[];
  total: number;
  onEndReached: () => void;
  selected: Set<number>;
  focusIndex: number;
  onSelect: (id: number, on: boolean) => void;
  onSelectAll: (on: boolean) => void;
  onOpen: (id: number) => void;
  bulkBar: React.ReactNode;
}) {
  // Live appearance prefs: toggling density / tags-in-list / age-spine on the
  // Settings route re-renders this list immediately, instead of only after a remount
  // (which is why "compact vs comfortable did nothing").
  const { density, showAgeSpine, showTagsInList } = useSettings();
  const compact = density === "compact";
  const parentRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => (compact ? 44 : 66),
    overscan: 10,
  });

  // Fetch the next page when the last rendered row nears the end of the loaded set.
  const virtualItems = virtualizer.getVirtualItems();
  const lastIndex = virtualItems.length ? virtualItems[virtualItems.length - 1].index : 0;
  useEffect(() => {
    if (items.length && lastIndex >= items.length - 20) onEndReached();
  }, [lastIndex, items.length, onEndReached]);

  // Keep the keyboard-focused row on screen as j/k move it.
  useEffect(() => {
    if (focusIndex >= 0) virtualizer.scrollToIndex(focusIndex, { align: "auto" });
  }, [focusIndex, virtualizer]);

  const allOn = items.length > 0 && items.every((i) => selected.has(i.id));

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Select every item under the current filter in one click. */}
      <div className="flex items-center gap-2 px-5 py-1.5">
        <label className="flex cursor-pointer items-center gap-2 text-[12px] text-muted hover:text-ink">
          <input
            type="checkbox"
            checked={allOn}
            ref={(el) => {
              if (el) el.indeterminate = selected.size > 0 && !allOn;
            }}
            onChange={(e) => onSelectAll(e.target.checked)}
            className="h-3.5 w-3.5 accent-[var(--color-accent)]"
          />
          {selected.size > 0
            ? `${selected.size} selected`
            : `Select all ${items.length}${items.length < total ? ` of ${total}` : ""}`}
        </label>
      </div>
      {bulkBar && <div className="px-5 pt-2">{bulkBar}</div>}
      <div ref={parentRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
          {virtualizer.getVirtualItems().map((row) => {
            const item = items[row.index];
            const s = spine(item.age_days);
            const on = selected.has(item.id);
            return (
              <div
                key={item.id}
                ref={virtualizer.measureElement}
                data-index={row.index}
                className="absolute left-0 top-0 w-full"
                style={{ transform: `translateY(${row.start}px)` }}
              >
                <div
                  draggable
                  onDragStart={(e) => {
                    // Drag the whole selection when the row is part of it,
                    // otherwise just this row.
                    const ids = selected.has(item.id) ? [...selected] : [item.id];
                    e.dataTransfer.setData(
                      "application/x-cairn-items",
                      JSON.stringify(ids),
                    );
                    e.dataTransfer.effectAllowed = "copy";
                  }}
                  onClick={() => onOpen(item.id)}
                  className={clsx(
                    "group mx-2 cursor-pointer rounded-lg pl-3 pr-4 transition-colors",
                    compact ? "py-1.5" : "py-3",
                    on ? "bg-accent-soft/50" : "hover:bg-surface",
                    row.index === focusIndex && "kbd-focus",
                  )}
                  style={showAgeSpine ? { borderLeft: `${s.width}px solid ${s.color}` } : undefined}
                >
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      checked={on}
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e) => onSelect(item.id, e.target.checked)}
                      className="mt-1 h-3.5 w-3.5 shrink-0 accent-[var(--color-accent)] opacity-0 transition-opacity group-hover:opacity-100 checked:opacity-100"
                    />
                    <div className="min-w-0 flex-1">
                      <p className={clsx("truncate leading-snug", compact ? "text-[13px]" : "text-[14.5px]")}>
                        {item.title || item.canonical_url}
                      </p>
                      {/* One quiet line: source, a SHORT venue, year, status. */}
                      <p
                        className={clsx(
                          "truncate font-mono text-muted",
                          compact ? "mt-0.5 text-[10.5px]" : "mt-1 text-[11px]",
                        )}
                      >
                        {[
                          item.source || "web",
                          shortVenue(item.venue),
                          item.year || null,
                          item.status !== "unread" ? item.status : null,
                        ]
                          .filter(Boolean)
                          .join("  ·  ")}
                      </p>
                      {/* Tags under the row are opt-in (Settings -> In the item list):
                          off by default so the list stays clean and scannable. */}
                      {showTagsInList && item.tags.length > 0 && (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {item.tags.slice(0, 8).map((t) => (
                            <Tag key={t} name={t} />
                          ))}
                        </div>
                      )}
                    </div>
                    <a
                      href={item.canonical_url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      className="mt-1 shrink-0 text-muted opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
                    >
                      <ExternalLink size={14} />
                    </a>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function BulkBar({ ids, onDone }: { ids: number[]; onDone: () => void }) {
  const [tagDraft, setTagDraft] = useState("");
  const qc = useQueryClient();
  const bulk = useMutation({
    mutationFn: (patch: {
      status?: string; tags?: string[]; remove_tags?: string[]; bucket?: string;
    }) => api.bulk(ids, patch),
    onSuccess: () => {
      // Sidebar tag counts and the untagged/no-topic figures come from ["tags"]/
      // ["stats"] -- invalidate them too, or tagging leaves the counts stale.
      qc.invalidateQueries({ queryKey: ["tags"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
      onDone();
    },
  });
  const split = () => tagDraft.split(/[\s,]+/).filter(Boolean);
  const del = useMutation({
    mutationFn: () => api.bulkDelete(ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stats"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
      toast(`${ids.length} item${ids.length === 1 ? "" : "s"} deleted`);
      onDone();
    },
  });
  const merge = useMutation({
    mutationFn: () => api.mergeItems(ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stats"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
      toast(`Merged ${ids.length} into 1`);
      onDone();
    },
  });

  return (
    <div className="rise mb-2 flex flex-wrap items-center gap-2 rounded-lg border border-accent/40 bg-accent-soft px-3 py-2">
      <span className="text-sm font-medium text-accent">{ids.length} selected</span>
      {/* Tag: type a tag and apply it to every selected item. One clear action;
          removing a tag is done per-item in the detail panel. */}
      <input
        value={tagDraft}
        onChange={(e) => setTagDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && tagDraft.trim()) bulk.mutate({ tags: split() });
        }}
        placeholder="new tag…"
        className="w-32 rounded-md border border-line bg-surface px-2 py-1 font-mono text-xs outline-none focus:border-accent"
      />
      <Button variant="ghost" disabled={!tagDraft.trim()} onClick={() => bulk.mutate({ tags: split() })}>
        Tag {ids.length === 1 ? "it" : `these ${ids.length}`}
      </Button>
      <span className="mx-1 h-4 w-px bg-line" />
      <span className="text-[12px] text-muted">mark</span>
      {STATUSES.map((s) => (
        <Button key={s} variant="quiet" onClick={() => bulk.mutate({ status: s })}>{s}</Button>
      ))}
      <span className="mx-1 h-4 w-px bg-line" />
      <Button variant="quiet" onClick={() => bulk.mutate({ bucket: "docs" })}>→ Docs</Button>
      <Button variant="quiet" onClick={() => bulk.mutate({ bucket: "library" })}>→ Library</Button>
      {/* Merge: only for 2+ items -- fold near-duplicates the auto-finder missed
          into one, keeping the most complete copy and every URL as an alias. */}
      {ids.length >= 2 && (
        <>
          <span className="mx-1 h-4 w-px bg-line" />
          <Button
            variant="quiet"
            onClick={() => {
              if (
                confirm(
                  `Merge these ${ids.length} into one item?\n\n` +
                    "• The most complete one is kept (its title + URL).\n" +
                    "• Every other URL becomes an alias, so re-ingesting won't recreate the duplicate.\n" +
                    "• All tags are combined; any missing abstract / authors / venue / BibTeX is filled from a copy that has it.\n" +
                    "• The most-advanced reading status and earliest first-seen are kept.\n" +
                    "• The extra rows are then deleted.",
                )
              )
                merge.mutate();
            }}
          >
            <Combine size={13} /> Merge
          </Button>
        </>
      )}
      <span className="mx-1 h-4 w-px bg-line" />
      <Button variant="quiet" onClick={() => exportBib({ ids }, `${ids.length}-items`)}>
        <Download size={13} /> Export .bib
      </Button>
      {/* The real destructive action: remove the items from the library. */}
      <button
        onClick={() => {
          if (
            confirm(
              `Delete ${ids.length} item${ids.length === 1 ? "" : "s"} permanently? This cannot be undone.`,
            )
          )
            del.mutate();
        }}
        className="ml-auto inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-sm font-medium text-red-600 transition-colors hover:bg-red-600 hover:text-white"
      >
        <Trash2 size={13} /> Delete
      </button>
      <Button variant="quiet" onClick={onDone}>Clear</Button>
    </div>
  );
}

function Detail({
  id, onClose, onTag,
}: {
  id: number;
  onClose: () => void;
  onTag: (t: string) => void;
}) {
  const qc = useQueryClient();
  const { width, onPointerDown, reset } = usePaneWidth("tt-detail-w", 420, {
    min: 300,
    max: 820,
    edge: "left",
  });
  const { data: item } = useQuery({ queryKey: ["item", id], queryFn: () => api.item(id) });
  const { data: tagData } = useQuery({ queryKey: ["tags"], queryFn: api.tags });
  const allTags = tagData?.flat.map((t) => t.name) ?? [];
  const [notes, setNotes] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["item", id] });
    qc.invalidateQueries({ queryKey: ["items"] });
    qc.invalidateQueries({ queryKey: ["tags"] });
    qc.invalidateQueries({ queryKey: ["stats"] });
  };
  const patch = useMutation({
    mutationFn: (p: {
      status?: string; notes?: string; bucket?: string;
      title?: string; authors?: string; venue?: string; year?: number;
    }) => api.patchItem(id, p),
    onSuccess: () => { setEditing(false); refresh(); toast("Saved"); },
  });
  const addTags = useMutation({
    mutationFn: (t: string[]) => api.addTags(id, t),
    onSuccess: () => { refresh(); toast("Tag added"); },
  });
  const removeTag = useMutation({
    mutationFn: (n: string) => api.removeTag(id, n),
    onSuccess: () => { refresh(); toast("Tag removed"); },
  });
  const summarize = useMutation({
    mutationFn: () => api.summarize(id),
    onSuccess: () => { refresh(); toast("Summary generated"); },
  });
  const del = useMutation({
    mutationFn: () => api.deleteItem(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["items"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
      toast("Item deleted");
      onClose();
    },
  });

  return (
    <>
      <div
        onPointerDown={onPointerDown}
        onDoubleClick={reset}
        title="Drag to resize"
        className="relative w-[3px] shrink-0 cursor-col-resize transition-colors hover:bg-accent/40"
      >
        <span className="absolute inset-y-0 -left-1 -right-1" />
      </div>
      <aside
        style={{ width }}
        className="slide-in-right flex shrink-0 flex-col overflow-y-auto border-l border-line bg-surface"
      >
      <div className="flex items-center justify-between border-b border-line/50 px-5 py-3">
        <span className="font-mono text-[11px] text-muted">item {id}</span>
        <div className="flex items-center gap-2">
          {item && (
            <a
              href={item.canonical_url}
              target="_blank"
              rel="noreferrer"
              title="Open in browser"
              className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11.5px] text-muted transition-colors hover:border-accent hover:text-accent"
            >
              <ExternalLink size={12} /> Open
            </a>
          )}
          {item && (
            <button
              onClick={() => {
                navigator.clipboard.writeText(citationText(item));
                toast("Citation copied");
              }}
              title="Copy a plain-text citation"
              className="rounded-md p-1 text-muted transition-colors hover:text-accent"
            >
              <Quote size={13} />
            </button>
          )}
          {item && (
            <button
              onClick={() => setEditing((v) => !v)}
              title="Edit title and metadata"
              className={clsx("rounded-md p-1 transition-colors", editing ? "text-accent" : "text-muted hover:text-ink")}
            >
              <Pencil size={13} />
            </button>
          )}
          <button
            onClick={() => {
              if (confirm("Delete this item permanently? This cannot be undone."))
                del.mutate();
            }}
            title="Delete item permanently"
            className="rounded-md p-1 text-muted transition-colors hover:text-red-600"
          >
            <Trash2 size={13} />
          </button>
          <button onClick={onClose} className="text-muted hover:text-ink"><X size={15} /></button>
        </div>
      </div>
      {!item ? (
        <div className="space-y-3 px-5 py-4" aria-hidden="true">
          <div className="skeleton h-5 w-3/4" />
          <div className="skeleton h-3 w-1/2" />
          <div className="skeleton h-3 w-2/3" />
          <div className="skeleton mt-4 h-20 w-full" />
          <div className="skeleton h-8 w-40" />
        </div>
      ) : editing ? (
        <EditFields item={item} onSave={(p) => patch.mutate(p)} onCancel={() => setEditing(false)} />
      ) : (
        <div className="px-5 py-4">
          <h2 className="font-display text-[20px] leading-tight">
            {item.title || item.canonical_url}
          </h2>
          <p className="mt-1.5 font-mono text-[11px] text-muted">
            {[item.source, item.venue, item.year].filter(Boolean).join(" · ")}
          </p>
          <a
            href={item.canonical_url}
            target="_blank"
            rel="noreferrer"
            className="mt-1 block break-all font-mono text-[11px] text-accent hover:underline"
          >
            {item.canonical_url}
          </a>
          {/* Every URL merged into this item, so a merge never hides a link. */}
          {item.alt_urls && item.alt_urls.length > 0 && (
            <div className="mt-1.5">
              <span className="font-mono text-[10px] uppercase tracking-wide text-muted">also at</span>
              {item.alt_urls.map((u) => (
                <a
                  key={u}
                  href={u}
                  target="_blank"
                  rel="noreferrer"
                  className="block break-all font-mono text-[10.5px] text-muted hover:text-accent hover:underline"
                >
                  {u}
                </a>
              ))}
            </div>
          )}
          {item.authors && <p className="mt-3 text-[12.5px] text-muted">{item.authors}</p>}
          {item.summary && <p className="mt-3 text-[13.5px]">{item.summary}</p>}
          {item.relevance && (
            <p className="mt-2 rounded-md bg-accent-soft/50 px-2.5 py-1.5 text-[12.5px] text-ink">
              <span className="font-semibold text-accent">Why you saved this: </span>
              {item.relevance}
            </p>
          )}
          {item.abstract && (
            <p className="mt-2 text-[13px] leading-relaxed text-muted">{item.abstract}</p>
          )}
          {(item.abstract || item.title) && (
            <button
              onClick={() => summarize.mutate()}
              disabled={summarize.isPending}
              className="mt-2 inline-flex items-center gap-1 text-[11.5px] text-muted transition-colors hover:text-accent disabled:opacity-50"
            >
              <Sparkles size={11} />
              {summarize.isPending
                ? "Summarizing…"
                : item.relevance || item.summary
                  ? "Regenerate summary"
                  : "Summarize + why saved"}
            </button>
          )}

          <BibtexPanel item={item} />
          <RelatedPanel id={id} />

          <div className="mt-5">
            <SectionLabel>Tags</SectionLabel>
            <div className="flex flex-wrap gap-1">
              {item.tags.map((t) => (
                <Tag key={t} name={t} onClick={() => onTag(t)} onRemove={() => removeTag.mutate(t)} />
              ))}
              {!item.tags.length && (
                <span className="font-mono text-[11px] text-accent/70">untagged</span>
              )}
            </div>
            <TagInput all={allTags} onAdd={(tags) => addTags.mutate(tags)} />
          </div>

          <div className="mt-5">
            <SectionLabel>Status</SectionLabel>
            <div className="flex flex-wrap gap-1.5">
              {STATUSES.map((s) => (
                <Button
                  key={s}
                  variant={item.status === s ? "primary" : "ghost"}
                  className="!px-2.5 !py-1 !text-xs"
                  onClick={() => patch.mutate({ status: s })}
                >
                  {s}
                </Button>
              ))}
            </div>
          </div>

          <div className="mt-5">
            <SectionLabel>Notes</SectionLabel>
            <textarea
              value={notes ?? item.notes ?? ""}
              onChange={(e) => setNotes(e.target.value)}
              onBlur={() => notes !== null && patch.mutate({ notes })}
              rows={4}
              placeholder="Why does this matter?"
              className="w-full rounded-md border border-line bg-paper px-2.5 py-2 text-[13px] outline-none focus:border-accent"
            />
          </div>

          {item.first_seen && (
            <p className="mt-5 font-mono text-[10.5px] text-muted">
              first seen {item.first_seen.slice(0, 10)}
              {item.age_days != null && ` · carried ${item.age_days} days`}
            </p>
          )}
        </div>
      )}
      </aside>
    </>
  );
}

/** Correct captured metadata by hand -- titles and venues are sometimes wrong,
 *  and no re-fetch can fix a site behind bot-protection. */
function EditFields({
  item,
  onSave,
  onCancel,
}: {
  item: Item;
  onSave: (p: { title?: string; authors?: string; venue?: string; year?: number }) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState(item.title ?? "");
  const [authors, setAuthors] = useState(item.authors ?? "");
  const [venue, setVenue] = useState(item.venue ?? "");
  const [year, setYear] = useState(item.year?.toString() ?? "");
  const field =
    "mt-0.5 w-full rounded-md border border-line bg-paper px-2 py-1.5 text-[13px] outline-none focus:border-accent";
  const label = "text-[10px] uppercase tracking-wide text-muted";

  return (
    <div className="space-y-3 px-5 py-4">
      <p className="text-[11.5px] text-muted">Captured data is sometimes wrong. Fix it here.</p>
      <label className="block">
        <span className={label}>Title</span>
        <textarea value={title} onChange={(e) => setTitle(e.target.value)} rows={2} className={field} />
      </label>
      <label className="block">
        <span className={label}>Authors</span>
        <input value={authors} onChange={(e) => setAuthors(e.target.value)} className={field} />
      </label>
      <div className="flex gap-2">
        <label className="block flex-1">
          <span className={label}>Venue</span>
          <input value={venue} onChange={(e) => setVenue(e.target.value)} className={field} />
        </label>
        <label className="block w-24">
          <span className={label}>Year</span>
          <input value={year} onChange={(e) => setYear(e.target.value)} inputMode="numeric" className={field} />
        </label>
      </div>
      <div className="flex gap-2">
        <Button
          onClick={() =>
            onSave({
              title: title.trim() || undefined, // never blank the title by accident
              authors,
              venue,
              year: year.trim() ? Number(year) : undefined,
            })
          }
        >
          Save
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
