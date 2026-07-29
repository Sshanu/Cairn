import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import clsx from "clsx";
import { ChevronRight, CornerDownRight, ExternalLink } from "lucide-react";

import { api, type TagNode } from "../api";
import { Empty, Tag, spine } from "../ui";

/** Miller columns: one scrollable column per level of the hierarchy.
 *
 *  A sidebar tree has to indent, so depth costs horizontal space and deep paths
 *  truncate. Columns give every level the same full width no matter how deep it
 *  sits, which is why Finder browses filesystems this way. The last column
 *  shows the items filed at (or beneath) the selected node.
 */
export function Browse() {
  const [params, setParams] = useSearchParams();
  const path = params.get("tag") ?? "";
  const { data } = useQuery({ queryKey: ["tags"], queryFn: api.tags });
  const nodes = data?.tags ?? [];

  const childrenOf = useMemo(() => {
    const map = new Map<string, TagNode[]>();
    for (const node of nodes) {
      const parent = node.name.split("/").slice(0, -1).join("/");
      const list = map.get(parent) ?? [];
      list.push(node);
      map.set(parent, list);
    }
    return map;
  }, [nodes]);

  // One column per ancestor of the selected path, plus the root.
  const columns = useMemo(() => {
    const out: { parent: string; items: TagNode[] }[] = [
      { parent: "", items: childrenOf.get("") ?? [] },
    ];
    if (!path) return out;
    const parts = path.split("/");
    for (let i = 0; i < parts.length; i++) {
      const prefix = parts.slice(0, i + 1).join("/");
      const kids = childrenOf.get(prefix);
      if (kids?.length) out.push({ parent: prefix, items: kids });
    }
    return out;
  }, [childrenOf, path]);

  const items = useQuery({
    queryKey: ["browse-items", path],
    queryFn: () => api.items({ tag: path, limit: 200, bucket: "" }),
    enabled: !!path,
  });

  function select(name: string) {
    const next = new URLSearchParams(params);
    next.set("tag", name);
    setParams(next, { replace: true });
  }

  const crumbs = path ? path.split("/") : [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-line px-8 py-4">
        <h1 className="font-display text-[26px] leading-none">Browse</h1>
        <div className="mt-2 flex flex-wrap items-center gap-1 font-mono text-[12px]">
          <button
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("tag");
              setParams(next, { replace: true });
            }}
            className={clsx("hover:text-accent", path ? "text-muted" : "text-accent")}
          >
            all
          </button>
          {crumbs.map((crumb, i) => (
            <span key={i} className="flex items-center gap-1">
              <ChevronRight size={11} className="text-muted opacity-50" />
              <button
                onClick={() => select(crumbs.slice(0, i + 1).join("/"))}
                className={clsx(
                  "hover:text-accent",
                  i === crumbs.length - 1 ? "text-accent" : "text-muted",
                )}
              >
                {crumb}
              </button>
            </span>
          ))}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 overflow-x-auto">
        {columns.map((column, index) => (
          <div
            key={column.parent || "root"}
            className="flex w-60 shrink-0 flex-col overflow-y-auto border-r border-line"
          >
            <div className="sticky top-0 bg-paper px-3 py-2 text-[10px] font-semibold uppercase tracking-[0.09em] text-muted">
              {column.parent ? column.parent.split("/").pop() : "facet"}
            </div>
            {column.items.map((node) => {
              const onPath =
                path === node.name || path.startsWith(node.name + "/");
              const hasChildren = (childrenOf.get(node.name)?.length ?? 0) > 0;
              return (
                <button
                  key={node.name}
                  onClick={() => select(node.name)}
                  className={clsx(
                    "flex items-center gap-2 px-3 py-[5px] text-left font-mono text-[12px] transition-colors",
                    onPath
                      ? "bg-accent-soft text-accent"
                      : "text-ink hover:bg-surface",
                  )}
                >
                  <span className="min-w-0 flex-1 truncate">{node.label}</span>
                  <span className="shrink-0 text-[10px] opacity-55">{node.count}</span>
                  {hasChildren && (
                    <ChevronRight size={11} className="shrink-0 opacity-40" />
                  )}
                </button>
              );
            })}
            {column.items.length === 0 && (
              <p className="px-3 py-2 text-[11.5px] text-muted">no children</p>
            )}
            {index === columns.length - 1 && column.items.length > 0 && (
              <div className="px-3 py-2 text-[10.5px] text-muted opacity-60">
                {column.items.length} node{column.items.length === 1 ? "" : "s"}
              </div>
            )}
          </div>
        ))}

        <div className="min-w-[420px] flex-1 overflow-y-auto">
          {!path ? (
            <Empty
              title="Pick a facet to start."
              hint="Columns open to the right as you go deeper — there is no depth limit."
            />
          ) : items.isLoading ? (
            <p className="p-8 text-sm text-muted">Loading…</p>
          ) : !items.data?.items.length ? (
            <Empty
              title="Nothing filed here yet."
              hint={`No item carries ${path} or anything beneath it.`}
            />
          ) : (
            <div>
              <div className="flex items-baseline gap-2 px-6 py-3 text-[12px] text-muted">
                <CornerDownRight size={13} />
                <span className="font-mono">{path}</span>
                <span>and everything beneath it</span>
                <span className="ml-auto font-mono">{items.data.total} items</span>
              </div>
              {items.data.items.map((item) => {
                const s = spine(item.age_days);
                return (
                  <div
                    key={item.id}
                    className="group border-b border-line py-2.5 pl-4 pr-5 transition-colors hover:bg-surface"
                    style={{ borderLeft: `${s.width}px solid ${s.color}` }}
                  >
                    <div className="flex items-start gap-3">
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[14.5px]">
                          {item.title || item.canonical_url}
                        </p>
                        <p className="mt-0.5 font-mono text-[11px] text-muted">
                          {item.source || "web"}
                          {item.year ? ` · ${item.year}` : ""}
                          {item.bucket === "docs" ? " · document" : ""}
                        </p>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {item.tags.slice(0, 6).map((t) => (
                            <Tag key={t} name={t} onClick={() => select(t)} />
                          ))}
                        </div>
                      </div>
                      <div className="flex shrink-0 gap-2 opacity-0 transition-opacity group-hover:opacity-100">
                        <Link
                          to={`/?q=${encodeURIComponent(item.title ?? "")}`}
                          className="text-muted hover:text-accent"
                          title="open in library"
                        >
                          <CornerDownRight size={14} />
                        </Link>
                        <a
                          href={item.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-muted hover:text-accent"
                        >
                          <ExternalLink size={14} />
                        </a>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
