import { useMemo, useState } from "react";
import clsx from "clsx";

/** Add a tag with prefix autocomplete against the existing vocabulary.
 *
 *  Deliberately strict: suggestions are prefix matches only (on the leaf, the
 *  full path, or any segment), capped at ten, and there are none at all when the
 *  box is empty or nothing shares the prefix. A dropdown that lists everything --
 *  or anything, when you have typed nothing -- is noise, not help.
 */
export function TagInput({
  all,
  onAdd,
  placeholder = "add a tag, press Enter",
}: {
  all: string[];
  onAdd: (tags: string[]) => void;
  placeholder?: string;
}) {
  const [value, setValue] = useState("");
  const [cursor, setCursor] = useState(0);

  const suggestions = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (!q) return []; // never suggest on an empty box
    const scored: { name: string; rank: number }[] = [];
    for (const name of all) {
      const lower = name.toLowerCase();
      const leaf = lower.split("/").pop() ?? "";
      let rank = -1;
      if (leaf.startsWith(q)) rank = 0; // leaf starts with what you typed
      else if (lower.startsWith(q)) rank = 1; // full path starts with it
      else if (lower.split("/").some((seg) => seg.startsWith(q))) rank = 2; // any segment
      if (rank >= 0) scored.push({ name, rank });
    }
    return scored
      .sort((a, b) => a.rank - b.rank || a.name.length - b.name.length)
      .slice(0, 10) // at most ten
      .map((s) => s.name);
  }, [value, all]);

  function commit(tag?: string) {
    const text = (tag ?? value).trim();
    if (!text) return;
    onAdd(text.split(/[\s,]+/).filter(Boolean));
    setValue("");
    setCursor(0);
  }

  return (
    <div className="relative mt-2">
      <input
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          setCursor(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setCursor((c) => Math.min(c + 1, suggestions.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setCursor((c) => Math.max(c - 1, 0));
          } else if (e.key === "Enter") {
            e.preventDefault();
            commit(suggestions[cursor]); // typed text if no suggestion
          } else if (e.key === "Escape") {
            setValue("");
          }
        }}
        placeholder={placeholder}
        className="w-full rounded-md border border-line bg-paper px-2 py-1.5 font-mono text-xs outline-none focus:border-accent"
      />
      {suggestions.length > 0 && (
        <ul className="absolute z-20 mt-1 max-h-52 w-full overflow-auto rounded-md border border-line bg-surface py-1 shadow-lg">
          {suggestions.map((s, i) => (
            <li key={s}>
              <button
                onMouseEnter={() => setCursor(i)}
                onClick={() => commit(s)}
                className={clsx(
                  "block w-full truncate px-2 py-1 text-left font-mono text-[11.5px]",
                  i === cursor ? "bg-accent-soft text-accent" : "text-muted hover:text-ink",
                )}
              >
                {s}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
