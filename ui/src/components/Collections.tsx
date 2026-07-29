import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Folder, Pencil, Plus, Trash2 } from "lucide-react";
import clsx from "clsx";

import { api } from "../api";
import { SectionLabel } from "../ui";

// Collections are folders you file papers into (like Zotero). They live in the
// `collection/` tag namespace, so filing / drag / the extension all reuse tags -- but
// they get their own first-class section here where you create, rename and delete them.
const PREFIX = "collection/";
const slug = (s: string) =>
  s.trim().toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");

export function Collections() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const active = params.get("tag") ?? "";
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");

  const { data } = useQuery({ queryKey: ["tags"], queryFn: api.tags });
  const items = (data?.tags ?? []).filter(
    (n) => n.name.startsWith(PREFIX) && n.name.split("/").length === 2,
  );

  const invalidate = () => qc.invalidateQueries({ queryKey: ["tags"] });
  const create = useMutation({ mutationFn: (name: string) => api.createTag(name), onSuccess: invalidate });
  const rename = useMutation({
    mutationFn: ({ from, to }: { from: string; to: string }) => api.renameTag(from, to),
    onSuccess: () => qc.invalidateQueries(),
  });
  const del = useMutation({
    mutationFn: (name: string) => api.deleteTag(name, true),
    onSuccess: () => qc.invalidateQueries(),
  });

  function addCollection() {
    const s = slug(draft);
    if (s) create.mutate(PREFIX + s);
    setDraft("");
    setAdding(false);
  }
  function commitEdit(name: string) {
    const s = slug(editDraft);
    setEditing(null);
    if (s && PREFIX + s !== name) rename.mutate({ from: name, to: PREFIX + s });
  }

  return (
    <div className="mt-6 px-3">
      <div className="flex items-center justify-between">
        <SectionLabel>Collections</SectionLabel>
        <button
          onClick={() => {
            setAdding(true);
            setDraft("");
          }}
          title="New collection"
          className="text-muted transition-colors hover:text-accent"
        >
          <Plus size={13} />
        </button>
      </div>

      <ul className="mt-1 space-y-0.5">
        {items.map((c) => {
          const name = c.name;
          const label = name.slice(PREFIX.length);
          if (editing === name) {
            return (
              <li key={name} className="px-1">
                <input
                  autoFocus
                  value={editDraft}
                  onChange={(e) => setEditDraft(e.target.value)}
                  onBlur={() => commitEdit(name)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitEdit(name);
                    if (e.key === "Escape") setEditing(null);
                  }}
                  className="w-full rounded border border-accent bg-surface px-1.5 py-0.5 text-[13px] outline-none"
                />
              </li>
            );
          }
          return (
            <li key={name} className="group flex items-center gap-1">
              <button
                onClick={() => navigate(`/?tag=${encodeURIComponent(name)}`)}
                title={name}
                className={clsx(
                  "flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1 text-left text-[13px] transition-colors hover:bg-surface",
                  active === name ? "text-accent" : "text-muted hover:text-ink",
                )}
              >
                <Folder size={12} className="shrink-0 opacity-60" />
                <span className="truncate">{label}</span>
                {!!c.count && <span className="ml-auto font-mono text-[10px] opacity-50">{c.count}</span>}
              </button>
              <button
                onClick={() => {
                  setEditing(name);
                  setEditDraft(label);
                }}
                title="Rename"
                className="shrink-0 px-0.5 text-muted opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
              >
                <Pencil size={11} />
              </button>
              <button
                onClick={() => {
                  if (window.confirm(`Delete collection “${label}”? Your papers stay in the library.`))
                    del.mutate(name);
                }}
                title="Delete collection"
                className="shrink-0 px-0.5 text-muted opacity-0 transition-opacity hover:text-red-600 group-hover:opacity-100"
              >
                <Trash2 size={11} />
              </button>
            </li>
          );
        })}

        {adding && (
          <li className="px-1">
            <input
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={addCollection}
              onKeyDown={(e) => {
                if (e.key === "Enter") addCollection();
                if (e.key === "Escape") setAdding(false);
              }}
              placeholder="new collection name…"
              className="w-full rounded border border-accent bg-surface px-1.5 py-0.5 text-[13px] outline-none"
            />
          </li>
        )}

        {!items.length && !adding && (
          <li className="px-2 py-1 text-[12px] text-muted">
            No collections yet —{" "}
            <button onClick={() => setAdding(true)} className="text-accent hover:underline">
              create one
            </button>
            .
          </li>
        )}
      </ul>
    </div>
  );
}
