import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { api } from "../api";
import { SectionLabel } from "../ui";

/** Papers in your library that share the most references with this one.
 *  Renders nothing unless the "Related in your library" feature is on. */
export function RelatedPanel({ id }: { id: number }) {
  const [params, setParams] = useSearchParams();
  const { data, isLoading } = useQuery({
    queryKey: ["related", id],
    queryFn: () => api.related(id),
  });

  if (!data?.enabled) return null; // feature off -> nothing shown

  const open = (rid: number) => {
    const next = new URLSearchParams(params);
    next.set("item", String(rid));
    setParams(next, { replace: true });
  };

  return (
    <div className="mt-5">
      <SectionLabel>Related in your library</SectionLabel>
      {isLoading ? (
        <p className="text-[12px] text-muted">Looking for shared references…</p>
      ) : data.related.length === 0 ? (
        <p className="text-[12px] text-muted">
          No strong overlap found yet. It grows as you open more papers with this on.
        </p>
      ) : (
        <ul className="space-y-0.5">
          {data.related.map((r) => (
            <li key={r.id} className="flex items-baseline gap-2">
              <button
                onClick={() => open(r.id)}
                className="min-w-0 flex-1 truncate text-left text-[12.5px] hover:text-accent"
                title={r.title ?? r.canonical_url}
              >
                {r.title ?? r.canonical_url}
              </button>
              <span
                className="shrink-0 font-mono text-[10px] text-muted"
                title={`${r.shared} shared references`}
              >
                {r.shared}×
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
