import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Sparkles } from "lucide-react";

import { api } from "../api";

/** A once-a-week glance: what you added, what's waiting, where reading clusters.
 *  Shown only when the Weekly digest feature is on. Entirely deterministic. */
export function DigestCard() {
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const { data } = useQuery({ queryKey: ["digest"], queryFn: api.digest });

  if (!settings?.weekly_digest || !data) return null;

  const stat = (n: number, label: string) => (
    <div>
      <div className="font-display text-[20px] leading-none">{n}</div>
      <div className="mt-0.5 text-[11px] text-muted">{label}</div>
    </div>
  );

  return (
    <div className="rise mb-6 rounded-xl border border-line bg-surface/50 p-4">
      <div className="mb-3 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.09em] text-muted">
        <Sparkles size={12} className="text-accent" /> This week
      </div>

      <div className="flex flex-wrap gap-6">
        {stat(data.added_week, "added")}
        {stat(data.reading, "reading now")}
        {stat(data.stale_unread, "unread > 30 days")}
      </div>

      {data.clusters.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 text-[11px] text-muted">Your active topics lately</div>
          <div className="flex flex-wrap gap-1.5">
            {data.clusters.map((c) => (
              <Link
                key={c.name}
                to={`/?tag=${encodeURIComponent(c.name)}`}
                className="inline-flex items-center gap-1 rounded-md bg-accent-soft px-2 py-0.5 font-mono text-[11.5px] text-accent hover:opacity-80"
              >
                {c.name.split("/").pop()}
                <span className="opacity-60">{c.count}</span>
              </Link>
            ))}
          </div>
        </div>
      )}

      {data.read_next.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 text-[11px] text-muted">
            Unread in your active topics — read next
          </div>
          <ul className="space-y-0.5">
            {data.read_next.slice(0, 3).map((o) => (
              <li key={o.id} className="flex items-baseline gap-2">
                <Link
                  to={`/?item=${o.id}`}
                  className="min-w-0 flex-1 truncate text-[12.5px] hover:text-accent"
                  title={o.title ?? o.canonical_url}
                >
                  {o.title ?? o.canonical_url}
                </Link>
                {o.age_days != null && (
                  <span className="shrink-0 font-mono text-[10.5px] text-muted">{o.age_days}d</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
