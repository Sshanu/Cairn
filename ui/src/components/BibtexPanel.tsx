import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, Copy, Loader2, Pencil, RotateCw, TriangleAlert } from "lucide-react";
import clsx from "clsx";

import { api, type Bibtex, type Item } from "../api";
import { SectionLabel } from "../ui";

// Only papers get a BibTeX; a blog or a doc has nothing to cite.
const PAPER_SOURCES = new Set([
  "arxiv", "openreview", "acl", "doi", "cvf", "neurips", "pmlr", "aaai", "ecva", "ieee",
]);

/** Copy a paper's BibTeX, extracted from the web -- never generated.
 *
 *  The entry is only ever a real one pulled from CVF/ACL/CrossRef/DBLP/arXiv. If
 *  none exists online the panel says so and offers a box to paste your own,
 *  which is stored as "manual" and never overwritten by a later auto-resolve.
 *  That is the deal: a true citation or an empty slot, never a plausible fake.
 */
export function BibtexPanel({ item }: { item: Item }) {
  const qc = useQueryClient();
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [draft, setDraft] = useState("");
  const [notFound, setNotFound] = useState(false);
  const [entry, setEntry] = useState<Bibtex | null>(
    item.bibtex
      ? {
          bibtex: item.bibtex,
          source: item.bibtex_source ?? "",
          venue: item.bibtex_venue ?? null,
          published: !!item.bibtex_published,
        }
      : null,
  );

  const resolve = useMutation({
    mutationFn: (refresh: boolean) => (refresh ? api.refreshBibtex(item.id) : api.bibtex(item.id)),
    onSuccess: (got) => {
      setNotFound(got.found === false || !got.bibtex);
      setEntry(got.bibtex ? got : null);
      qc.invalidateQueries({ queryKey: ["item", item.id] });
    },
  });

  const save = useMutation({
    mutationFn: (text: string) => api.patchItem(item.id, { bibtex: text }),
    onSuccess: () => {
      setEntry({ bibtex: draft.trim(), source: "manual", venue: null, published: false });
      setPasting(false);
      setNotFound(false);
      qc.invalidateQueries({ queryKey: ["item", item.id] });
    },
  });

  // After every hook, so the Rules of Hooks hold: a blog or doc has no citation.
  if (!PAPER_SOURCES.has(item.source ?? "")) return null;

  async function copy() {
    let text = entry?.bibtex ?? null;
    if (!text) {
      // POST, not GET: GET is stored-only now, so "Find BibTeX" must trigger the
      // real web search explicitly (the user clicked and expects a brief wait).
      const got = await resolve.mutateAsync(true);
      text = got.bibtex;
    }
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  }

  const busy = resolve.isPending;
  const manual = entry?.source === "manual";

  return (
    <div className="mt-5">
      <div className="flex items-center justify-between">
        <SectionLabel>BibTeX</SectionLabel>
        {entry && (
          <span
            title={`from ${entry.source}`}
            className={clsx(
              "rounded-full px-1.5 py-0.5 font-mono text-[10px]",
              manual
                ? "bg-accent-soft text-accent"
                : entry.published
                  ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                  : "bg-amber-500/15 text-amber-700 dark:text-amber-400",
            )}
          >
            {manual ? "yours" : entry.published ? entry.venue ?? "published" : "arXiv preprint"}
          </span>
        )}
      </div>

      {pasting ? (
        <div className="mt-1.5">
          <textarea
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={6}
            placeholder="Paste a BibTeX entry — @inproceedings{ ... }"
            className="w-full rounded-md border border-line bg-paper p-2 font-mono text-[11px] outline-none focus:border-accent"
          />
          <div className="mt-1.5 flex gap-1.5">
            <button
              onClick={() => draft.trim() && save.mutate(draft.trim())}
              disabled={!draft.trim() || save.isPending}
              className="rounded-md bg-accent px-2.5 py-1 text-[12.5px] text-white disabled:opacity-40"
            >
              Save
            </button>
            <button
              onClick={() => setPasting(false)}
              className="rounded-md border border-line px-2.5 py-1 text-[12.5px] text-muted hover:text-ink"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <button
            onClick={copy}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-2.5 py-1.5 text-[12.5px] transition-colors hover:border-accent hover:text-accent disabled:opacity-50"
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : copied ? <Check size={13} /> : <Copy size={13} />}
            {copied ? "Copied" : entry ? "Copy BibTeX" : "Find BibTeX"}
          </button>

          {entry && (
            <button
              onClick={() => setOpen((v) => !v)}
              className="inline-flex items-center gap-0.5 rounded-md px-1.5 py-1.5 text-[12px] text-muted hover:text-ink"
            >
              <ChevronRight size={12} className={clsx("transition-transform", open && "rotate-90")} />
              view
            </button>
          )}

          <button
            onClick={() => {
              setDraft(entry?.bibtex ?? "");
              setPasting(true);
            }}
            title="Paste or edit the entry yourself"
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-1.5 text-[11.5px] text-muted hover:text-accent"
          >
            <Pencil size={11} /> {entry ? "edit" : "paste"}
          </button>

          {entry && !manual && (
            <button
              onClick={() => resolve.mutate(true)}
              disabled={busy}
              title="Re-fetch from the web"
              className="ml-auto inline-flex items-center gap-1 rounded-md px-1.5 py-1.5 text-[11.5px] text-muted hover:text-accent disabled:opacity-50"
            >
              <RotateCw size={11} /> re-fetch
            </button>
          )}
        </div>
      )}

      {notFound && !pasting && (
        <p className="mt-1.5 flex items-center gap-1 text-[11.5px] text-amber-600">
          <TriangleAlert size={11} /> No BibTeX found online — paste your own above.
        </p>
      )}

      {open && entry && (
        <pre className="mt-2 max-h-72 overflow-auto rounded-md border border-line bg-paper p-2.5 font-mono text-[11px] leading-relaxed">
          {entry.bibtex}
        </pre>
      )}
    </div>
  );
}
