import { useEffect, useMemo, useRef, useState } from "react";
import { CornerDownLeft, Hash, TriangleAlert } from "lucide-react";
import clsx from "clsx";

/** Name a new branch, with the existing vocabulary in front of you.
 *
 *  This replaces window.prompt for a reason beyond looks: typing blind is how a
 *  library grows `topic/prompt-tuning` alongside `topic/prompt-optimization`.
 *  Showing what already exists as you type is the cheapest possible guard
 *  against creating the near-duplicates that later have to be merged.
 */
export function BranchComposer({
  parent,
  existing,
  onCancel,
  onCreate,
}: {
  parent: string;
  existing: string[];
  onCancel: () => void;
  onCreate: (fullName: string) => void;
}) {
  const [value, setValue] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);

  const slug = value
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9/-]/g, "");
  const fullName = parent ? `${parent}/${slug}` : slug;

  /** Rank by where the match lands: a leaf that starts with what you typed is a
   *  likelier duplicate of your intent than one that merely contains it. */
  const suggestions = useMemo(() => {
    const q = slug.replace(/-/g, "");
    if (!q) return []; // nothing on an empty box
    const scored: Array<{ name: string; rank: number }> = [];
    for (const name of existing) {
      const leaf = (name.split("/").pop() ?? "").replace(/-/g, "");
      const flatFull = name.replace(/[-/]/g, "");
      // Prefix only: the leaf starts with it, the full path starts with it, or
      // some segment starts with it. No loose substring matches.
      let rank = -1;
      if (leaf.startsWith(q)) rank = 0;
      else if (flatFull.startsWith(q)) rank = 1;
      else if (name.split("/").some((seg) => seg.replace(/-/g, "").startsWith(q))) rank = 2;
      if (rank >= 0) scored.push({ name, rank });
    }
    return scored
      .sort((a, b) => a.rank - b.rank || a.name.length - b.name.length)
      .slice(0, 10)
      .map((s) => s.name);
  }, [slug, existing]);

  useEffect(() => setCursor(0), [slug]);

  const exact = existing.includes(fullName);
  const elsewhere = suggestions.find((n) => n !== fullName && n.split("/").pop() === slug);

  function submit() {
    if (!slug || exact) return;
    onCreate(fullName);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/25 pt-[18vh] backdrop-blur-[2px]"
      onMouseDown={onCancel}
    >
      <div
        className="rise w-[min(560px,92vw)] overflow-hidden rounded-xl border border-line bg-surface shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-3.5 py-3">
          <Hash size={13} className="shrink-0 text-muted" />
          <div className="min-w-0 flex-1">
            {parent && (
              <div className="truncate font-mono text-[10.5px] text-muted">{parent}/</div>
            )}
            <input
              ref={inputRef}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") onCancel();
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setCursor((c) => Math.min(c + 1, suggestions.length - 1));
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setCursor((c) => Math.max(c - 1, 0));
                }
                if (e.key === "Tab" && suggestions[cursor]) {
                  // Tab borrows the wording; Enter still creates under *this* parent.
                  e.preventDefault();
                  setValue(suggestions[cursor].split("/").pop() ?? "");
                }
                if (e.key === "Enter") {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder={parent ? "name the sub-branch" : "name the branch, e.g. project"}
              className="w-full bg-transparent font-mono text-[13.5px] outline-none placeholder:text-muted/60"
            />
          </div>
          <button
            onClick={submit}
            disabled={!slug || exact}
            className="shrink-0 rounded-md border border-line px-2 py-1 text-muted transition-colors enabled:hover:border-accent enabled:hover:text-accent disabled:opacity-30"
            title="Create"
          >
            <CornerDownLeft size={12} />
          </button>
        </div>

        {(exact || elsewhere) && (
          <div className="flex items-start gap-2 border-b border-line bg-accent-soft/40 px-3.5 py-2 text-[12px]">
            <TriangleAlert size={12} className="mt-[3px] shrink-0 text-accent" />
            <span className="text-muted">
              {exact ? (
                <>
                  <span className="font-mono text-ink">{fullName}</span> already exists.
                </>
              ) : (
                <>
                  <span className="font-mono text-ink">{elsewhere}</span> already covers this
                  name — reuse it instead of making a second one?
                </>
              )}
            </span>
          </div>
        )}

        {suggestions.length > 0 && (
          <ul className="max-h-[240px] overflow-y-auto py-1">
            {suggestions.map((name, i) => (
              <li key={name}>
                <button
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => setValue(name.split("/").pop() ?? "")}
                  className={clsx(
                    "flex w-full items-baseline gap-2 px-3.5 py-1.5 text-left transition-colors",
                    i === cursor && "bg-accent-soft",
                  )}
                >
                  <span className="truncate font-mono text-[12px] text-muted">
                    {name.split("/").slice(0, -1).join("/")}
                    {name.includes("/") && "/"}
                    <span className="text-ink">{name.split("/").pop()}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="border-t border-line px-3.5 py-2 font-mono text-[10.5px] text-muted">
          {slug ? (
            <>
              creates <span className="text-accent">{fullName}</span>
            </>
          ) : (
            "↑↓ browse · tab to borrow a name · enter to create · esc to cancel"
          )}
        </div>
      </div>
    </div>
  );
}
