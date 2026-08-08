import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useInfiniteQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { CalendarClock, ChevronRight, ExternalLink } from "lucide-react";

import { api, type HistoryDay, type HistoryItem } from "../api";
import { Empty, Spinner } from "../ui";
import { DigestCard } from "../components/DigestCard";

const BUCKETS = [
  { id: "", label: "Everything" },
  { id: "library", label: "Library" },
  { id: "docs", label: "Documents" },
] as const;

/** A day-by-day record of what entered the library, newest first.
 *
 *  Grouped by first_seen -- the day a tab or paper was first encountered, not
 *  the day the row happened to be written -- so the timeline reflects real
 *  reading history and stretches back across the whole Zotero import, not just
 *  this session. Days load a page at a time as you scroll.
 */
export function History() {
  const [bucket, setBucket] = useBucketParam();

  const query = useInfiniteQuery({
    queryKey: ["history", bucket],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      api.history({ bucket: bucket || undefined, before: pageParam || undefined, days: 30 }),
    getNextPageParam: (last) => last.next_before ?? undefined,
  });

  const days = useMemo(
    () => query.data?.pages.flatMap((p) => p.days) ?? [],
    [query.data],
  );
  // A shared scale so a busy day reads as busy at a glance. Sqrt keeps a
  // one-item day visible next to a three-hundred-item import day.
  const maxCount = Math.max(1, ...days.map((d) => d.count));

  // Days are collapsed by default so the page opens as a scannable timeline of
  // "how much on each day"; you expand one to read it. The most recent day --
  // what you just added -- opens on its own, once, so the page is never empty.
  const [openDays, setOpenDays] = useState<Set<string>>(new Set());
  const autoOpened = useRef(false);
  useEffect(() => {
    if (!autoOpened.current && days.length) {
      autoOpened.current = true;
      setOpenDays(new Set([days[0].date]));
    }
  }, [days]);
  const toggleDay = (date: string) =>
    setOpenDays((s) => {
      const next = new Set(s);
      next.has(date) ? next.delete(date) : next.add(date);
      return next;
    });

  const sentinel = useInfiniteScroll(
    () => query.fetchNextPage(),
    query.hasNextPage && !query.isFetchingNextPage,
  );

  const totalItems = days.reduce((n, d) => n + d.count, 0);

  return (
    <div className="mx-auto max-w-3xl px-8 py-6">
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <h1 className="flex items-center gap-2 font-display text-[26px] leading-none">
          <CalendarClock size={22} className="text-accent" /> History
        </h1>
        <div className="ml-auto flex overflow-hidden rounded-lg border border-line">
          {BUCKETS.map((b) => (
            <button
              key={b.id}
              onClick={() => setBucket(b.id)}
              className={clsx(
                "px-2.5 py-1.5 text-[12.5px] transition-colors",
                bucket === b.id ? "bg-accent text-white" : "text-muted hover:text-ink",
              )}
            >
              {b.label}
            </button>
          ))}
        </div>
      </div>

      <DigestCard />

      {query.isLoading ? (
        <Spinner label="Reading your history…" />
      ) : days.length === 0 ? (
        <Empty title="Nothing here yet" hint="Items appear here the day they are first seen." />
      ) : (
        <>
          <p className="mb-4 font-mono text-[11.5px] text-muted">
            {totalItems} items across {days.length} day{days.length === 1 ? "" : "s"}
            {query.hasNextPage ? " · scroll for more" : ""}
          </p>

          {/* The spine: one continuous rule the day dots sit on. */}
          <div className="relative border-l border-line pl-6">
            {days.map((day) => (
              <DaySection
                key={day.date}
                day={day}
                maxCount={maxCount}
                open={openDays.has(day.date)}
                onToggle={() => toggleDay(day.date)}
              />
            ))}
          </div>

          <div ref={sentinel} className="h-10" />
          {query.isFetchingNextPage && <Spinner label="Loading earlier days…" />}
        </>
      )}
    </div>
  );
}

function DaySection({
  day,
  maxCount,
  open,
  onToggle,
}: {
  day: HistoryDay;
  maxCount: number;
  open: boolean;
  onToggle: () => void;
}) {
  const width = Math.max(0.05, Math.sqrt(day.count / maxCount)) * 100;
  return (
    <section className="rise mb-0.5">
      {/* One compact row is the toggle: date, count, and an inline bar whose
          length shows how busy the day was -- scannable without expanding. */}
      <button
        onClick={onToggle}
        aria-expanded={open}
        className="sticky top-0 z-10 -ml-6 flex w-full items-center gap-2 bg-paper/85 py-[3px] pl-6 text-left backdrop-blur-sm"
      >
        <span
          className={clsx(
            "absolute -left-[6px] h-2.5 w-2.5 rounded-full border-2 border-paper transition-colors",
            open ? "bg-accent" : "bg-line",
          )}
        />
        <ChevronRight
          size={12}
          className={clsx("shrink-0 text-muted transition-transform duration-150", open && "rotate-90")}
        />
        <h2 className="shrink-0 text-[13.5px] font-medium">{formatDay(day.date)}</h2>
        <span className="shrink-0 font-mono text-[10.5px] text-muted">{day.count}</span>
        <span className="ml-1 h-[3px] flex-1 overflow-hidden rounded-full bg-line/40">
          <span className="grow-x block h-full rounded-full bg-accent/60" style={{ width: `${width}%` }} />
        </span>
      </button>

      {open && (
        <ul className="mb-2 mt-0.5">
          {day.items.map((item, i) => (
            <HistoryRow key={item.id} item={item} index={i} />
          ))}
        </ul>
      )}
    </section>
  );
}

function HistoryRow({ item, index }: { item: HistoryItem; index: number }) {
  const navigate = useNavigate();
  const meta = [item.source, item.venue, item.year].filter(Boolean).join(" · ");
  return (
    <li
      className="rise group relative flex items-baseline gap-2 rounded-md px-2 py-[3px] transition-colors hover:bg-surface"
      style={{ animationDelay: `${Math.min(index, 12) * 18}ms` }}
    >
      <span className="pop absolute -left-[25px] top-[9px] h-1.5 w-1.5 rounded-full bg-line group-hover:bg-accent" />
      {/* Click opens the detail panel in the Workspace; the icon opens the tab. */}
      <button
        onClick={() => navigate(`/?item=${item.id}`)}
        className="min-w-0 flex-1 truncate text-left text-[13px] leading-snug hover:text-accent"
        title={item.title || item.canonical_url}
      >
        {item.title || item.canonical_url}
      </button>
      {meta && (
        <span className="hidden shrink-0 font-mono text-[10px] text-muted sm:inline">{meta}</span>
      )}
      <a
        href={item.canonical_url}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => e.stopPropagation()}
        title="Open in browser"
        className="shrink-0 text-muted opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
      >
        <ExternalLink size={12} />
      </a>
    </li>
  );
}

// --- helpers ----------------------------------------------------------------

const DAY_MS = 86_400_000;

function formatDay(iso: string): string {
  const date = new Date(iso + "T00:00:00");
  const today = new Date();
  const midnight = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const diff = Math.round((midnight.getTime() - date.getTime()) / DAY_MS);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  const opts: Intl.DateTimeFormatOptions =
    date.getFullYear() === today.getFullYear()
      ? { weekday: "long", month: "long", day: "numeric" }
      : { weekday: "short", year: "numeric", month: "long", day: "numeric" };
  return new Intl.DateTimeFormat(undefined, opts).format(date);
}

function useBucketParam(): [string, (b: string) => void] {
  const [params, setParams] = useSearchParams();
  const set = (b: string) => {
    const next = new URLSearchParams(params);
    b ? next.set("bucket", b) : next.delete("bucket");
    setParams(next, { replace: true });
  };
  return [params.get("bucket") ?? "", set];
}

/** Fire `onHit` when the sentinel scrolls into view and there is more to load. */
function useInfiniteScroll(onHit: () => void, enabled: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const cb = useRef(onHit);
  cb.current = onHit;
  useEffect(() => {
    const el = ref.current;
    if (!el || !enabled) return;
    const io = new IntersectionObserver(
      (entries) => entries[0]?.isIntersecting && cb.current(),
      { rootMargin: "300px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [enabled]);
  return ref;
}
