import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import clsx from "clsx";

import { BranchComposer } from "./BranchComposer";
import { ChevronRight, ChevronsDownUp, GripVertical, Hash, Pin, Plus, Trash2 } from "lucide-react";

import { api, type TagNode } from "../api";
import { useMutation, useQueryClient } from "@tanstack/react-query";

type Node = TagNode & { children: Node[] };

// Versioned: an earlier build persisted "every facet open" on first render, and
// a stored value always beat the default, so the tree could never start clean
// again. Bumping the key retires that state once -- v3 discards the old expanded
// sets so every tree starts fully collapsed, and only what you open is kept.
const STORAGE_KEY = "tt-tree-open-v3";
const PIN_KEY = "tt-tree-pins";
const ORDER_KEY = "tt-facet-order";
const ALIAS_KEY = "tt-tag-labels";

// `type/`, `venue/` and `site/` are recomputed from the URL on every ingest, so
// a real rename there would be undone and would leave a duplicate branch behind.
// Those get a display alias instead: you see your name, the pipeline keeps its
// own. Everything else renames for real, moving the whole subtree.
const COMPUTED_FACETS = new Set(["type", "venue", "site"]);

function loadAliases(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(ALIAS_KEY) ?? "{}") || {};
  } catch {
    return {};
  }
}

function loadSet(key: string): Set<string> {
  try {
    return new Set<string>(JSON.parse(localStorage.getItem(key) ?? "[]"));
  } catch {
    return new Set();
  }
}

/** Build the nested shape once; the API returns a flat, sorted list. */
function nest(flat: TagNode[]): Node[] {
  const byName = new Map<string, Node>();
  const roots: Node[] = [];
  for (const node of flat) {
    byName.set(node.name, { ...node, children: [] });
  }
  for (const node of byName.values()) {
    const parentName = node.name.split("/").slice(0, -1).join("/");
    const parent = parentName ? byName.get(parentName) : undefined;
    parent ? parent.children.push(node) : roots.push(node);
  }
  return roots;
}

const FACET_LABEL: Record<string, string> = {
  topic: "Topic",
  method: "Method",
  task: "Task",
  type: "Type",
  venue: "Venue",
  site: "Site",
  contribution: "Contribution",
};

// A starting order, not a fixed one: which facet you navigate by is personal
// and changes with what you are working on, so it is draggable and persisted.
const DEFAULT_ORDER = ["topic", "venue", "type", "method", "task", "site", "contribution", "zotero"];

export function TagTree({ nodes }: { nodes: TagNode[] }) {
  const qc = useQueryClient();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const active = params.get("tag") ?? "";
  const [order, setOrder] = useState<string[]>(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(ORDER_KEY) ?? "null");
      if (Array.isArray(stored) && stored.length) return stored;
    } catch {
      /* fall through to the default */
    }
    return DEFAULT_ORDER;
  });
  const [dragging, setDragging] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [aliases, setAliases] = useState<Record<string, string>>(loadAliases);
  const [editing, setEditing] = useState<string | null>(null);
  const [composing, setComposing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    localStorage.setItem(ALIAS_KEY, JSON.stringify(aliases));
  }, [aliases]);

  const labelOf = (node: { name: string; label: string }) =>
    aliases[node.name] ?? node.label;

  const isComputed = (name: string) => COMPUTED_FACETS.has(name.split("/")[0]);

  const create = useMutation({
    mutationFn: (name: string) => api.createTag(name),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["tags"] });
      // Reveal the whole path including the new node itself, so you can keep
      // nesting without hunting for it.
      setOpen((o) => new Set([...o, ...ancestorsOf(r.name), r.name]));
    },
  });

  // Dropping items onto a branch files them there. This is the Zotero gesture:
  // the tree is not just a filter, it is somewhere you can put things.
  const assign = useMutation({
    mutationFn: ({ ids, tag }: { ids: number[]; tag: string }) =>
      api.bulk(ids, { tags: [tag] }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tags"] });
      qc.invalidateQueries({ queryKey: ["items"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteTag(name, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tags"] });
      qc.invalidateQueries({ queryKey: ["items"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });

  function deleteBranch(node: { name: string; count: number }) {
    const beneath = node.count
      ? `\n\n${node.count} item${node.count === 1 ? "" : "s"} will lose this tag. The items themselves are not deleted.`
      : "";
    if (window.confirm(`Delete "${node.name}" and everything under it?${beneath}`)) {
      remove.mutate(node.name);
      setPins((ps) => {
        const next = new Set(ps);
        [...next].forEach((p) => {
          if (p === node.name || p.startsWith(node.name + "/")) next.delete(p);
        });
        return next;
      });
    }
  }

  // window.prompt gave no view of the existing vocabulary, which is how you end
  // up with two branches meaning the same thing. The composer shows them.
  function addChild(parent: string) {
    setComposing(parent);
  }

  const rename = useMutation({
    mutationFn: ({ from, to }: { from: string; to: string }) => api.renameTag(from, to),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tags"] });
      qc.invalidateQueries({ queryKey: ["items"] });
    },
  });

  function commitRename(node: { name: string; label: string }) {
    const value = draft.trim();
    setEditing(null);
    if (!value || value === labelOf(node)) return;
    if (isComputed(node.name)) {
      // Display-only: the underlying tag keeps the name the pipeline writes.
      setAliases((a) => ({ ...a, [node.name]: value }));
    } else {
      const parent = node.name.split("/").slice(0, -1).join("/");
      const slug = value.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9/-]/g, "");
      const from = node.name;
      const to = parent ? `${parent}/${slug}` : slug;
      rename.mutate({ from, to });
      // Renaming shifts this branch's path AND every descendant's, so repoint pins.
      repointPins(from, to);
    }
  }

  useEffect(() => {
    localStorage.setItem(ORDER_KEY, JSON.stringify(order));
  }, [order]);

  const rank = (label: string) => {
    const i = order.indexOf(label);
    return i === -1 ? 99 : i;
  };

  const tree = useMemo(
    () => nest(nodes).sort((a, b) => rank(a.label) - rank(b.label)),
    [nodes, order],
  );

  function reorder(from: string, to: string) {
    if (from === to) return;
    setOrder((current) => {
      const names = [...new Set([...current, ...tree.map((t) => t.label)])];
      const next = names.filter((n) => n !== from);
      next.splice(next.indexOf(to), 0, from);
      return next;
    });
  }

  // A branch's path just changed (a rename or a move); keep any pin on it or its
  // descendants pointing at the new path instead of silently vanishing.
  function repointPins(from: string, to: string) {
    setPins((ps) => {
      if (![...ps].some((p) => p === from || p.startsWith(from + "/"))) return ps;
      return new Set(
        [...ps].map((p) =>
          p === from ? to : p.startsWith(from + "/") ? to + p.slice(from.length) : p,
        ),
      );
    });
  }

  // Drag a branch onto another branch (or a facet root) to re-parent it. Moving a
  // subtree is just a path change, which renameTag already applies atomically to the
  // branch and everything under it. Reject the illegal moves: onto itself, into its
  // own descendant (a cycle), a no-op back to its current parent, or across into a
  // computed facet (venue/type/site are derived from the URL, not hand-arranged).
  function reparent(from: string, toParent: string) {
    if (COMPUTED_FACETS.has(toParent.split("/")[0])) return;
    if (toParent === from || toParent.startsWith(from + "/")) return;
    if (toParent === from.split("/").slice(0, -1).join("/")) return;
    const leaf = from.split("/").slice(-1)[0];
    const to = `${toParent}/${leaf}`;
    rename.mutate({ from, to });
    repointPins(from, to);
  }

  // Which branches are open is a working state, not a preference reset on every
  // navigation: a reload that re-collapses the tree loses where you were.
  const [open, setOpen] = useState<Set<string>>(() => {
    // Distinguish "never set" from "set to empty": an absent key means first
    // load, a present-but-empty array means the user deliberately collapsed
    // everything. Both start collapsed, but only the latter must survive a
    // refresh -- the old code re-opened three facets whenever the set was empty,
    // so collapsing everything never stuck.
    let stored: string[] | null = null;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw !== null) stored = JSON.parse(raw);
    } catch {
      /* corrupt or unavailable storage: treat as first load */
    }
    // Restore EXACTLY what was open -- nothing more. Earlier this also force-opened
    // the active tag's ancestors on every mount, so a refresh kept re-expanding the
    // facet you were filtering by (e.g. venue) even after you collapsed it. A reload
    // should refresh data, not change what's expanded.
    return new Set<string>(Array.isArray(stored) ? stored : []);
  });

  // Pinned nodes: any tag at any depth, lifted out of the tree into its own
  // section. The tree shows structure; pins show the handful of branches you
  // are actually working in this month.
  const [pins, setPins] = useState<Set<string>>(() => loadSet(PIN_KEY));
  useEffect(() => {
    try {
      localStorage.setItem(PIN_KEY, JSON.stringify([...pins]));
    } catch {
      /* ignore */
    }
  }, [pins]);

  const togglePin = (name: string) =>
    setPins((s) => {
      const next = new Set(s);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });

  // Index every node of the NESTED tree by name. Pins resolve through this, so a
  // pinned branch carries its full subtree -- the old flat map had no children,
  // which is exactly why a pinned branch showed none.
  const byName = useMemo(() => {
    const map = new Map<string, Node>();
    const walk = (n: Node) => {
      map.set(n.name, n);
      n.children.forEach(walk);
    };
    tree.forEach(walk);
    return map;
  }, [tree]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify([...open]));
    } catch {
      /* storage full or blocked: the tree still works, it just forgets */
    }
  }, [open]);

  function select(name: string) {
    // Always land on the Workspace: the sidebar is visible on every page, and a
    // tag click from History or Settings used to set a param the page ignored,
    // so nothing opened. Navigating carries the filter to where it is shown.
    if (name === active) {
      navigate("/");
      return;
    }
    // Counts beside each tag are library-wide, so drop any bucket/query filter.
    navigate(`/?tag=${encodeURIComponent(name)}`);
  }

  function ancestorsOf(name: string): string[] {
    const parts = name.split("/");
    return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
  }

  /** Items dragged from the list arrive as JSON on the drag payload. */
  function droppedIds(e: React.DragEvent): number[] {
    try {
      const raw = e.dataTransfer.getData("application/x-cairn-items");
      const ids = JSON.parse(raw);
      return Array.isArray(ids) ? ids.filter((n) => typeof n === "number") : [];
    } catch {
      return [];
    }
  }


  const pinned = [...pins].map((n) => byName.get(n)).filter(Boolean) as Node[];
  const anyOpen = tree.some((f) => open.has(f.name));

  return (
    <div className="space-y-3">
      {composing !== null && (
        <BranchComposer
          parent={composing}
          existing={nodes.map((n) => n.name)}
          onCancel={() => setComposing(null)}
          onCreate={(name) => {
            create.mutate(name);
            setComposing(null);
          }}
        />
      )}

      <div className="flex items-center gap-2">
        <button
          onClick={() => addChild("")}
          className="flex items-center gap-1 text-[10px] uppercase tracking-[0.08em] text-muted transition-colors hover:text-accent"
          title="Create your own branch, then drag papers onto it"
        >
          <Plus size={10} /> new branch
        </button>
      </div>

      <button
        onClick={() =>
          setOpen(anyOpen ? new Set() : new Set(tree.map((f) => f.name)))
        }
        className="flex w-full items-center gap-1 text-[10px] uppercase tracking-[0.08em] text-muted transition-colors hover:text-accent"
      >
        <ChevronsDownUp size={10} />
        {anyOpen ? "collapse all" : "expand all"}
      </button>

      {pinned.length > 0 && (
        <div className="mb-1 border-b border-line pb-3">
          <p className="mb-1 flex items-center gap-1 text-[10.5px] font-semibold uppercase tracking-[0.09em] text-accent">
            <Pin size={10} /> Pinned
          </p>
          {/* Render each pin as a full Branch, not a flat link: a pinned branch
              must expand to its sub-branches and be renamable, exactly like it is
              in the tree below. The leaf label is enough here (hover shows the full
              path); the subtree gives the context the old flat row lacked. */}
          {pinned
            .sort((a, b) => a.name.localeCompare(b.name))
            .map((node) => (
              <Branch
                key={node.name}
                node={node}
                depth={0}
                open={open}
                setOpen={setOpen}
                active={active}
                onSelect={select}
                pins={pins}
                togglePin={togglePin}
                editing={editing}
                setEditing={setEditing}
                draft={draft}
                setDraft={setDraft}
                commitRename={commitRename}
                labelOf={labelOf}
                addChild={addChild}
                deleteBranch={deleteBranch}
                reparent={reparent}
                assign={assign}
                droppedIds={droppedIds}
                dropTarget={dropTarget}
                setDropTarget={setDropTarget}
              />
            ))}
        </div>
      )}

      {tree.map((facet) => {
        const hasChildren = facet.children.length > 0;
        const isActive = active === facet.name;
        return (
        <div
          key={facet.name}
          draggable
          onDragStart={() => setDragging(facet.label)}
          onDragEnd={() => setDragging(null)}
          onDragOver={(e) => {
            e.preventDefault();
            if (e.dataTransfer.types.includes("application/x-cairn-branch")) {
              setDropTarget(facet.name);
            }
          }}
          onDragLeave={() => setDropTarget(null)}
          onDrop={(e) => {
            e.preventDefault();
            // A branch dropped on a facet root moves to that facet's top level;
            // anything else here is a facet-on-facet reorder.
            const branch = e.dataTransfer.getData("application/x-cairn-branch");
            if (branch) {
              reparent(branch, facet.name);
            } else if (dragging) {
              reorder(dragging, facet.label);
            }
            setDragging(null);
            setDropTarget(null);
          }}
          className={clsx(
            "group/facet rounded transition-colors",
            dragging === facet.label && "opacity-40",
            dragging && dragging !== facet.label && "hover:bg-accent-soft/50",
          )}
        >
          <div className={clsx(
            "flex items-center gap-1 rounded",
            isActive && "bg-accent-soft",
            dropTarget === facet.name && "ring-1 ring-accent bg-accent-soft",
          )}>
            <GripVertical
              size={9}
              className="shrink-0 cursor-grab text-muted opacity-30 active:cursor-grabbing"
            />
            {/* Chevron only when there is something to expand -- a facet with no
                children (or only items filed directly on it, like a bare project
                tag) shows no misleading expander. */}
            {hasChildren ? (
              <button
                onClick={() =>
                  setOpen((s) => {
                    const next = new Set(s);
                    next.has(facet.name) ? next.delete(facet.name) : next.add(facet.name);
                    return next;
                  })
                }
                aria-label={open.has(facet.name) ? "collapse" : "expand"}
                className="shrink-0 text-muted opacity-60 hover:opacity-100"
              >
                <ChevronRight
                  size={11}
                  className={clsx(
                    "transition-transform duration-150",
                    open.has(facet.name) && "rotate-90",
                  )}
                />
              </button>
            ) : (
              <span className="w-[11px] shrink-0" />
            )}
            {editing === facet.name ? (
              <input
                autoFocus
                value={draft}
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={() => commitRename(facet)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename(facet);
                  if (e.key === "Escape") setEditing(null);
                }}
                className="w-full rounded border border-accent bg-surface px-1 text-[10.5px] uppercase outline-none"
              />
            ) : (
              // Clicking the label FILTERS to everything under the facet, so the
              // count beside it is always reachable (that was the project=3 bug).
              <button
                onClick={() => select(facet.name)}
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  setEditing(facet.name);
                  setDraft(labelOf(facet));
                }}
                title={`${facet.name} — click to show all, double-click to rename`}
                className={clsx(
                  "min-w-0 flex-1 truncate text-left text-[10.5px] font-semibold uppercase tracking-[0.09em] transition-colors",
                  isActive ? "text-accent" : "text-muted hover:text-ink",
                )}
              >
                {aliases[facet.name] ?? FACET_LABEL[facet.label] ?? facet.label}
              </button>
            )}

            <span className="ml-auto flex shrink-0 items-center gap-[5px] pl-1.5">
              <span className="font-mono text-[10px] tabular-nums text-muted opacity-60">
                {facet.count}
              </span>
              {/* Always visible: a control you cannot see is a control you do not have. */}
              <button
                onClick={() => addChild(facet.name)}
                title={`Add a branch under ${facet.label}`}
                className="text-muted opacity-45 transition-opacity hover:text-accent hover:opacity-100"
              >
                <Plus size={11} />
              </button>
              <button
                onClick={() => deleteBranch(facet)}
                title={`Delete ${facet.label} and everything under it`}
                className="text-muted opacity-45 transition-opacity hover:text-red-600 hover:opacity-100"
              >
                <Trash2 size={11} />
              </button>
            </span>
          </div>

          {hasChildren && open.has(facet.name) && (
            <div className="mt-1">
              {facet.children.map((child) => (
                <Branch
                  key={child.name}
                  node={child}
                  depth={0}
                  open={open}
                  setOpen={setOpen}
                  active={active}
                  onSelect={select}
                  pins={pins}
                  togglePin={togglePin}
                  editing={editing}
                  setEditing={setEditing}
                  draft={draft}
                  setDraft={setDraft}
                  commitRename={commitRename}
                  labelOf={labelOf}
                  addChild={addChild}
                  deleteBranch={deleteBranch}
                  reparent={reparent}
                  assign={assign}
                  droppedIds={droppedIds}
                  dropTarget={dropTarget}
                  setDropTarget={setDropTarget}
                />
              ))}
            </div>
          )}
        </div>
        );
      })}
    </div>
  );
}

function Branch({
  node, depth, open, setOpen, active, onSelect, pins, togglePin,
  editing, setEditing, draft, setDraft, commitRename, labelOf,
  addChild, deleteBranch, reparent, assign, droppedIds, dropTarget, setDropTarget,
}: {
  node: Node;
  depth: number;
  open: Set<string>;
  setOpen: React.Dispatch<React.SetStateAction<Set<string>>>;
  active: string;
  onSelect: (name: string) => void;
  pins: Set<string>;
  togglePin: (name: string) => void;
  editing: string | null;
  setEditing: (n: string | null) => void;
  draft: string;
  setDraft: (v: string) => void;
  commitRename: (node: { name: string; label: string }) => void;
  labelOf: (node: { name: string; label: string }) => string;
  addChild: (parent: string) => void;
  deleteBranch: (node: { name: string; count: number }) => void;
  reparent: (from: string, toParent: string) => void;
  assign: { mutate: (v: { ids: number[]; tag: string }) => void };
  droppedIds: (e: React.DragEvent) => number[];
  dropTarget: string | null;
  setDropTarget: (n: string | null) => void;
}) {
  const isOpen = open.has(node.name);
  const hasChildren = node.children.length > 0;
  const isActive = active === node.name;

  return (
    <div>
      <div
        draggable
        onDragStart={(e) => {
          // Moving a branch: carry its own path. stopPropagation so this doesn't also
          // fire the enclosing facet's reorder-dragstart.
          e.stopPropagation();
          e.dataTransfer.setData("application/x-cairn-branch", node.name);
          e.dataTransfer.effectAllowed = "move";
        }}
        onDragOver={(e) => {
          const t = e.dataTransfer.types;
          if (
            t.includes("application/x-cairn-items") ||
            t.includes("application/x-cairn-branch")
          ) {
            e.preventDefault();
            e.stopPropagation();
            setDropTarget(node.name);
          }
        }}
        onDragLeave={() => setDropTarget(null)}
        onDrop={(e) => {
          e.preventDefault();
          e.stopPropagation();
          // A branch dropped here becomes a child of this node; papers dropped here
          // are filed under it. The two are told apart by the payload's MIME type.
          const branch = e.dataTransfer.getData("application/x-cairn-branch");
          if (branch) {
            reparent(branch, node.name);
          } else {
            const ids = droppedIds(e);
            if (ids.length) assign.mutate({ ids, tag: node.name });
          }
          setDropTarget(null);
        }}
        className={clsx(
          "group flex items-center gap-1 rounded px-1 py-[3px] transition-colors",
          dropTarget === node.name && "ring-1 ring-accent bg-accent-soft",
          isActive ? "bg-accent-soft text-accent" : "text-muted hover:text-ink",
        )}
        style={{ paddingLeft: `${depth * 11 + 4}px` }}
      >
        {hasChildren ? (
          <button
            onClick={() =>
              setOpen((s) => {
                const next = new Set(s);
                next.has(node.name) ? next.delete(node.name) : next.add(node.name);
                return next;
              })
            }
            className="shrink-0 opacity-60 hover:opacity-100"
            aria-label={isOpen ? "collapse" : "expand"}
          >
            <ChevronRight
              size={10}
              className={clsx("transition-transform duration-150", isOpen && "rotate-90")}
            />
          </button>
        ) : (
          <Hash size={9} className="shrink-0 opacity-30" />
        )}

        {editing === node.name ? (
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => commitRename(node)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitRename(node);
              if (e.key === "Escape") setEditing(null);
            }}
            className="min-w-0 flex-1 rounded border border-accent bg-surface px-1 font-mono text-[11.5px] outline-none"
          />
        ) : (
          <button
            onClick={() => onSelect(node.name)}
            onDoubleClick={(e) => {
              e.stopPropagation();
              setEditing(node.name);
              setDraft(labelOf(node));
            }}
            title={`${node.name} — double-click to rename`}
            className="min-w-0 flex-1 truncate text-left font-mono text-[11.5px] hover:text-accent"
          >
            {labelOf(node)}
          </button>
        )}
        <span className="ml-auto flex shrink-0 items-center gap-[5px] pl-1.5">
          <span className="font-mono text-[10px] tabular-nums opacity-50">{node.count}</span>
          {/* No rename pencil (double-click the label renames) and no describe
              button -- a decluttered row: add-branch, delete, pin. */}
          <button
            onClick={(e) => {
              e.stopPropagation();
              addChild(node.name);
            }}
            title="Add a branch under this"
            className="text-muted opacity-40 transition-opacity hover:text-accent hover:opacity-100"
          >
            <Plus size={11} />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              deleteBranch(node);
            }}
            title="Delete this branch and everything under it"
            className="text-muted opacity-40 transition-opacity hover:text-red-600 hover:opacity-100"
          >
            <Trash2 size={11} />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              togglePin(node.name);
            }}
            title={pins.has(node.name) ? "Unpin" : "Pin to top"}
            className={clsx(
              "transition-opacity hover:text-accent",
              pins.has(node.name)
                ? "text-accent opacity-100"
                : "text-muted opacity-40 hover:opacity-100",
            )}
          >
            <Pin size={11} />
          </button>
        </span>
      </div>

      {isOpen &&
        node.children.map((child) => (
          <Branch
            key={child.name}
            node={child}
            depth={depth + 1}
            open={open}
            setOpen={setOpen}
            active={active}
            onSelect={onSelect}
            pins={pins}
            togglePin={togglePin}
            editing={editing}
            setEditing={setEditing}
            draft={draft}
            setDraft={setDraft}
            commitRename={commitRename}
            labelOf={labelOf}
            addChild={addChild}
            deleteBranch={deleteBranch}
            reparent={reparent}
            assign={assign}
            droppedIds={droppedIds}
            dropTarget={dropTarget}
            setDropTarget={setDropTarget}
          />
        ))}
    </div>
  );
}
