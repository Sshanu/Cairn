import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import clsx from "clsx";
import { Check, GitMerge, Pencil, Trash2, X } from "lucide-react";

import { api } from "../api";
import { Button, Empty, ListSkeleton } from "../ui";

export function Tags() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["tags"], queryFn: api.tags });
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["tags"] });
    qc.invalidateQueries({ queryKey: ["stats"] });
    qc.invalidateQueries({ queryKey: ["items"] });
  };

  const rename = useMutation({
    mutationFn: ({ from, to }: { from: string; to: string }) => api.renameTag(from, to),
    onSuccess: () => {
      setEditing(null);
      refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.deleteTag(name, true),
    onSuccess: refresh,
  });
  const merge = useMutation({
    mutationFn: ({ sources, target }: { sources: string[]; target: string }) =>
      api.mergeTags(sources, target),
    onSuccess: () => {
      setSelected(new Set());
      refresh();
    },
  });

  const nodes = data?.tags ?? [];

  return (
    <div className="px-8 py-6">
      <h1 className="font-display text-[26px] leading-none">Tags</h1>
      <p className="mt-2 mb-5 max-w-2xl text-sm text-muted">
        Four facets, each a real hierarchy: <code>topic</code> for the research
        area, <code>method</code> for the technique, <code>task</code> for the
        problem, and <code>type</code> for the kind of artifact. An item carries
        tags from several facets at once, so nothing has to be forced into one
        folder. Rename or merge here and every item follows.
      </p>

      {selected.size > 1 && (
        <div className="rise mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-accent/40 bg-accent-soft px-3 py-2">
          <GitMerge size={14} className="text-accent" />
          <span className="text-sm text-accent">{selected.size} selected</span>
          <span className="text-sm text-muted">merge into</span>
          <select
            className="rounded-md border border-line bg-surface px-2 py-1 font-mono text-xs"
            defaultValue=""
            onChange={(e) =>
              e.target.value &&
              merge.mutate({
                sources: [...selected].filter((s) => s !== e.target.value),
                target: e.target.value,
              })
            }
          >
            <option value="">choose the tag to keep…</option>
            {[...selected].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <Button variant="quiet" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
        </div>
      )}

      {isLoading ? (
        <div className="max-w-3xl">
          <ListSkeleton rows={7} />
        </div>
      ) : nodes.length === 0 ? (
        <Empty
          title="No tags yet."
          hint="Run organize from ⌘K to derive a taxonomy and file everything."
        />
      ) : (
        <div className="max-w-3xl">
          {nodes.map((node) => (
            <div
              key={node.name}
              className={clsx(
                "group flex items-center gap-2 border-b border-line py-1.5",
                selected.has(node.name) && "bg-accent-soft/40",
              )}
              style={{ paddingLeft: `${node.depth * 22}px` }}
            >
              <input
                type="checkbox"
                checked={selected.has(node.name)}
                onChange={(e) => {
                  const next = new Set(selected);
                  e.target.checked ? next.add(node.name) : next.delete(node.name);
                  setSelected(next);
                }}
                className="h-3.5 w-3.5 shrink-0 accent-[var(--color-accent)] opacity-0 transition-opacity group-hover:opacity-100 checked:opacity-100"
              />

              {editing === node.name ? (
                <>
                  <input
                    autoFocus
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") rename.mutate({ from: node.name, to: draft });
                      if (e.key === "Escape") setEditing(null);
                    }}
                    className="flex-1 rounded-md border border-accent bg-surface px-2 py-0.5 font-mono text-[12.5px] outline-none"
                  />
                  <button
                    onClick={() => rename.mutate({ from: node.name, to: draft })}
                    className="text-accent"
                  >
                    <Check size={14} />
                  </button>
                  <button onClick={() => setEditing(null)} className="text-muted">
                    <X size={14} />
                  </button>
                </>
              ) : (
                <>
                  <Link
                    to={`/?tag=${encodeURIComponent(node.name)}`}
                    className="flex-1 truncate font-mono text-[12.5px] hover:text-accent"
                  >
                    {node.label}
                    {node.own === 0 && node.count > 0 && (
                      <span className="ml-2 text-[10.5px] text-muted opacity-60">
                        (group)
                      </span>
                    )}
                  </Link>
                  <span className="w-10 shrink-0 text-right font-mono text-[11.5px] text-muted">
                    {node.count}
                  </span>
                  <button
                    onClick={() => {
                      setEditing(node.name);
                      setDraft(node.name);
                    }}
                    className="text-muted opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
                    title="Rename (moves the whole subtree)"
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Delete ${node.name} and everything under it?`))
                        remove.mutate(node.name);
                    }}
                    className="text-muted opacity-0 transition-opacity hover:text-red-600 group-hover:opacity-100"
                    title="Delete tag and subtree"
                  >
                    <Trash2 size={13} />
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
