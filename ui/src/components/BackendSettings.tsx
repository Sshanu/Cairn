import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, Plus, Wand2, X } from "lucide-react";
import clsx from "clsx";

import { api, type ServerSettings } from "../api";
import { Button, SectionLabel } from "../ui";
import { startJob } from "../pages/Jobs";
import { toast } from "./Toaster";

/** An on/off switch. Flex-aligned so the knob always sits inside the track;
 *  position (left/right) and colour (grey/accent) and a word both signal state. */
function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      onClick={() => onChange(!on)}
      className="inline-flex shrink-0 items-center gap-2"
    >
      <span
        className={clsx(
          "flex h-5 w-9 items-center rounded-full px-0.5 transition-colors",
          on ? "justify-end bg-accent" : "justify-start bg-line",
        )}
      >
        <span className="h-4 w-4 rounded-full bg-white shadow" />
      </span>
      <span className={clsx("w-6 text-[11px]", on ? "text-accent" : "text-muted")}>
        {on ? "on" : "off"}
      </span>
    </button>
  );
}

/** One editable agent prompt: label, one-line description, a Reset-to-default,
 *  and a tall monospace editor. Full transparency into what the agent runs. */
function PromptField({
  label,
  description,
  value,
  def,
  rows,
  onChange,
  input,
}: {
  label: string;
  description: ReactNode;
  value: string;
  def: string;
  rows: number;
  onChange: (v: string) => void;
  input: string;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[13px] font-medium">{label}</span>
        <button
          onClick={() => onChange(def)}
          disabled={value === def}
          className="text-[11px] text-muted transition-colors hover:text-accent disabled:opacity-40"
        >
          Reset to default
        </button>
      </div>
      <p className="mb-1.5 text-[12px] leading-snug text-muted">{description}</p>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        spellCheck={false}
        className={clsx(input, "resize-y font-mono text-[11.5px] leading-relaxed")}
      />
    </div>
  );
}

function FeatureRow({
  title,
  hint,
  children,
}: {
  title: string;
  hint: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2.5">
      <div className="min-w-0">
        <p className="text-[13.5px]">{title}</p>
        <p className="mt-0.5 text-[12px] leading-snug text-muted">{hint}</p>
      </div>
      {children}
    </div>
  );
}

/** The settings that change what the backend actually does -- how often it files
 *  tabs, where it backs up, and which domains it never touches. Unlike the
 *  appearance settings these live on the server and take effect immediately:
 *  saving a new cadence reloads the launchd agent behind it.
 */
export function BackendSettings() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const [draft, setDraft] = useState<ServerSettings | null>(null);
  const [saved, setSaved] = useState(false);
  const [newDomain, setNewDomain] = useState("");

  useEffect(() => {
    if (data && !draft) setDraft(data);
  }, [data]);

  const save = useMutation({
    mutationFn: (patch: Partial<ServerSettings>) => api.putSettings(patch),
    onSuccess: (fresh) => {
      setDraft(fresh);
      qc.setQueryData(["settings"], fresh);
      setSaved(true);
      setTimeout(() => setSaved(false), 1600);
      // Blocking a domain removes what's already saved from it -- tell the user and
      // refresh the library so the removed items disappear immediately.
      if (fresh.purged_blocked && fresh.purged_blocked > 0) {
        toast(`Removed ${fresh.purged_blocked} item${fresh.purged_blocked === 1 ? "" : "s"} on blocked domains`);
        qc.invalidateQueries({ queryKey: ["items"] });
        qc.invalidateQueries({ queryKey: ["stats"] });
        qc.invalidateQueries({ queryKey: ["tags"] });
      }
    },
  });

  // Live "does my agent actually work?" check -- one tiny real call to the
  // configured backend. MUST stay above the early return below, or it becomes a
  // conditional hook (crashes the page once settings finish loading).
  const test = useMutation({ mutationFn: api.agentTest });

  if (isLoading || !draft) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted">
        <Loader2 size={14} className="animate-spin" /> Loading backend settings…
      </p>
    );
  }

  const set = (patch: Partial<ServerSettings>) => setDraft({ ...draft, ...patch });
  const dirty = JSON.stringify(draft) !== JSON.stringify(data);

  const addDomain = () => {
    const d = newDomain.trim();
    if (d && !draft.blocklist.includes(d)) set({ blocklist: [...draft.blocklist, d] });
    setNewDomain("");
  };

  const select =
    "rounded-lg border border-line bg-surface px-2 py-1.5 text-[13px] text-ink outline-none focus:border-accent";
  const input =
    "w-full rounded-lg border border-line bg-paper px-2.5 py-1.5 text-[13px] outline-none focus:border-accent";

  const toggle = (key: keyof ServerSettings) => (
    <Toggle
      on={!!draft[key]}
      onChange={(v) => set({ [key]: v } as Partial<ServerSettings>)}
    />
  );

  const PROVIDERS: { id: string; label: string; needsKey: boolean; note: string }[] = [
    { id: "codex", label: "Codex CLI", needsKey: false, note: "uses your codex login" },
    { id: "claude", label: "Claude (Anthropic)", needsKey: true, note: "" },
    { id: "openai", label: "OpenAI", needsKey: true, note: "" },
    { id: "ollama", label: "Ollama (local)", needsKey: false, note: "http://localhost:11434" },
    { id: "none", label: "No agent", needsKey: false, note: "organising / Ask / summaries off" },
  ];
  const provider = PROVIDERS.find((p) => p.id === draft.agent_provider) ?? PROVIDERS[0];

  return (
    <div className="space-y-7">
      <div>
        <SectionLabel>Agent</SectionLabel>
        <p className="mb-2 text-[12.5px] text-muted">
          Which model does the organizing, Ask and summaries. Keys are stored
          locally and never shown back.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={draft.agent_provider}
            onChange={(e) => set({ agent_provider: e.target.value })}
            className={select}
          >
            {PROVIDERS.map((p) => (
              <option key={p.id} value={p.id}>{p.label}</option>
            ))}
          </select>
          {provider.id !== "none" && (
            <input
              value={draft.agent_model}
              onChange={(e) => set({ agent_model: e.target.value })}
              placeholder={
                provider.id === "ollama" ? "llama3.1" : provider.id === "openai" ? "gpt-4o" : "model id"
              }
              className={clsx(input, "max-w-[180px]")}
            />
          )}
        </div>
        {provider.needsKey && (
          <input
            type="password"
            value={draft.agent_api_key ?? ""}
            onChange={(e) => set({ agent_api_key: e.target.value })}
            placeholder={draft.agent_api_key_set ? "•••••••• (saved — type to replace)" : "API key"}
            className={clsx(input, "mt-2 max-w-md font-mono text-[12px]")}
          />
        )}
        {provider.id === "ollama" && (
          <input
            value={draft.agent_base_url}
            onChange={(e) => set({ agent_base_url: e.target.value })}
            placeholder="http://localhost:11434/v1"
            className={clsx(input, "mt-2 max-w-md font-mono text-[12px]")}
          />
        )}
        {provider.id !== "none" && (
          <div className="mt-2 flex items-center gap-2 text-[12px] text-muted">
            reasoning effort
            <select
              value={draft.reasoning_effort || "medium"}
              onChange={(e) => set({ reasoning_effort: e.target.value })}
              className={select}
            >
              {["low", "medium", "high"].map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
        )}
        {provider.note && <p className="mt-1.5 text-[11.5px] text-muted">{provider.note}</p>}
        {provider.id !== "none" && (
          <div className="mt-3">
            <button
              onClick={() => test.mutate()}
              disabled={test.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1 text-[12px] text-muted hover:text-accent disabled:opacity-50"
            >
              {test.isPending ? (
                <><Loader2 size={13} className="animate-spin" /> Testing…</>
              ) : (
                "Test agent"
              )}
            </button>
            {test.data && (
              <p
                className={clsx(
                  "mt-1.5 text-[12px]",
                  test.data.ok ? "text-emerald-500" : "text-red-500",
                )}
              >
                {test.data.ok
                  ? `✓ ${test.data.name}/${test.data.model} replied in ${test.data.latency_ms} ms`
                  : `✗ ${test.data.error ?? "no response"}`}
              </p>
            )}
            {test.isError && (
              <p className="mt-1.5 text-[12px] text-red-500">
                ✗ {(test.error as Error)?.message ?? "request failed"}
              </p>
            )}
          </div>
        )}
      </div>

      <div>
        <SectionLabel>Features</SectionLabel>
        <p className="mb-2 text-[12.5px] text-muted">
          Every agent-backed nicety is opt-in — turn on only what you want.
        </p>
        <div className="divide-y divide-line/60">
          <FeatureRow
            title="Auto-organize on ingest"
            hint="New items get topic / venue tags the moment they're filed, so the library never becomes a pile."
          >
            {toggle("auto_organize")}
          </FeatureRow>
          <FeatureRow
            title="Auto-file into my custom branches"
            hint="Let the agent also file new papers into your own hierarchies (project/…) using the description you give each branch. Off = your custom tags stay fully manual."
          >
            {toggle("auto_file_projects")}
          </FeatureRow>
          <FeatureRow
            title="Auto-summary + “why you saved this”"
            hint="A two-line TL;DR and a relevance line per paper, generated once at ingest."
          >
            {toggle("auto_summary")}
          </FeatureRow>
          <FeatureRow
            title="Related in your library"
            hint="Fetch each paper's references so you can see which of your papers it overlaps with."
          >
            {toggle("related_citations")}
          </FeatureRow>
          <FeatureRow
            title="Weekly digest"
            hint="A once-a-week rollup: what you added, what's unread, which clusters are forming."
          >
            {toggle("weekly_digest")}
          </FeatureRow>
          <FeatureRow
            title="Pause tracking"
            hint="Stop the poller and the automatic ingest entirely — nothing new is recorded until you turn this off. Your library stays as-is."
          >
            {toggle("tracking_paused")}
          </FeatureRow>
        </div>
      </div>

      <div>
        <SectionLabel>How often the backend adds items</SectionLabel>
        <p className="mb-2 text-[12.5px] text-muted">
          Open tabs are filed into the library automatically on this cadence — no
          need to remember to run anything.
        </p>
        <div className="flex items-center gap-2">
          <select
            value={draft.ingest_interval_min}
            onChange={(e) => set({ ingest_interval_min: Number(e.target.value) })}
            className={select}
          >
            {[15, 30, 60, 120, 240, 480, 720, 1440].map((m) => (
              <option key={m} value={m}>
                {m < 60 ? `every ${m} min` : `every ${m / 60} h`}
              </option>
            ))}
          </select>
          <span className="text-[12px] text-muted">
            ledger tick every
            <select
              value={draft.poll_interval_min}
              onChange={(e) => set({ poll_interval_min: Number(e.target.value) })}
              className={clsx(select, "ml-1")}
            >
              {[1, 5, 10, 15, 30].map((m) => (
                <option key={m} value={m}>{m} min</option>
              ))}
            </select>
          </span>
        </div>
      </div>

      <div>
        <SectionLabel>Backup</SectionLabel>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={draft.backup_destination}
            onChange={(e) => set({ backup_destination: e.target.value })}
            className={select}
          >
            <option value="icloud">iCloud Drive</option>
            <option value="folder">A folder</option>
            <option value="github">A git repo (GitHub)</option>
          </select>
          <select
            value={draft.backup_interval_hours}
            onChange={(e) => set({ backup_interval_hours: Number(e.target.value) })}
            className={select}
          >
            <option value={0}>manual only</option>
            {[6, 12, 24, 48, 168].map((h) => (
              <option key={h} value={h}>
                {h < 24 ? `every ${h} h` : h === 24 ? "daily" : h === 168 ? "weekly" : `every ${h / 24} d`}
              </option>
            ))}
          </select>
        </div>
        {draft.backup_destination === "folder" && (
          <input
            value={draft.backup_folder}
            onChange={(e) => set({ backup_folder: e.target.value })}
            placeholder="/Users/you/Dropbox/Cairn"
            className={clsx(input, "mt-2")}
          />
        )}
        {draft.backup_destination === "github" && (
          <input
            value={draft.github_repo}
            onChange={(e) => set({ github_repo: e.target.value })}
            placeholder="/Users/you/code/my-library-backups  (a local clone; it commits & pushes)"
            className={clsx(input, "mt-2")}
          />
        )}
        <p className="mt-1.5 font-mono text-[11px] text-muted">saving to {draft.backup_here}</p>
      </div>

      <div className="space-y-5">
        <div>
          <SectionLabel>Organization — the three agents</SectionLabel>
          <p className="text-[12.5px] leading-snug text-muted">
            Cairn organizes in three steps, each a prompt you can edit — nothing hidden.
            (<code>type</code>, <code>venue</code> and <code>site</code> are computed from
            the URL, not from any prompt.)
          </p>
        </div>

        <PromptField
          label="1 · Tag-proposal prompt"
          description="Reads titles in clustered batches of 50 and proposes a flat pool of candidate concepts across your whole library."
          value={draft.tag_proposal_prompt}
          def={draft.tag_proposal_prompt_default}
          rows={7}
          onChange={(v) => set({ tag_proposal_prompt: v })}
          input={input}
        />
        <PromptField
          label="2 · Hierarchy prompt"
          description="Designs the whole tree in one reasoning pass — the concept pool is a prior, not a checklist. The agent plans the areas and deep structure itself and merges near-duplicates."
          value={draft.hierarchy_prompt}
          def={draft.hierarchy_prompt_default}
          rows={7}
          onChange={(v) => set({ hierarchy_prompt: v })}
          input={input}
        />
        <PromptField
          label="3 · Tagging prompt"
          description="Tags every item into the hierarchy — per item, in batches of 10, multi-label. May grow the vocab for a genuine gap."
          value={draft.classify_prompt}
          def={draft.classify_prompt_default}
          rows={10}
          onChange={(v) => set({ classify_prompt: v })}
          input={input}
        />

        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-[13px] font-medium">Facets — the hierarchies to build</span>
            <button
              onClick={() => set({ facets: draft.facets_default })}
              disabled={draft.facets.join(",") === draft.facets_default.join(",")}
              className="text-[11px] text-muted transition-colors hover:text-accent disabled:opacity-40"
            >
              Reset to default
            </button>
          </div>
          <p className="mb-1.5 text-[12px] leading-snug text-muted">
            Comma-separated. Each gets its own independent hierarchy from the pool. e.g.
            <code> topic, method, task</code> — or add your own.
          </p>
          <input
            value={draft.facets.join(", ")}
            onChange={(e) =>
              set({
                facets: e.target.value
                  .split(",")
                  .map((f) => f.trim().replace(/\/+$/, ""))
                  .filter(Boolean),
              })
            }
            className={clsx(input, "font-mono text-[12.5px]")}
          />
        </div>

        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-[13px] font-medium">Standing rules — the house style</span>
            <button
              onClick={() => set({ organization_guidance: draft.organization_guidance_default })}
              disabled={draft.organization_guidance === draft.organization_guidance_default}
              className="text-[11px] text-muted transition-colors hover:text-accent disabled:opacity-40"
            >
              Reset to default
            </button>
          </div>
          <p className="mb-1.5 text-[12px] leading-snug text-muted">
            These apply to all three agents above and are always in effect — edit them to
            change how everything is organized. Cairn also learns from tags you add or
            delete by hand.
          </p>
          <textarea
            value={draft.organization_guidance}
            onChange={(e) => set({ organization_guidance: e.target.value })}
            rows={9}
            spellCheck={false}
            className={clsx(input, "resize-y font-mono text-[11.5px] leading-relaxed")}
          />
        </div>

        {/* Two steps: build & REVIEW the hierarchy first, then tag every item. */}
        <div className="space-y-2 rounded-lg border border-line/60 bg-surface/40 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => {
                if (dirty) save.mutate(draft); // apply edited prompts + facets first
                if (
                  window.confirm(
                    "⚠️ Step 1 ERASES every current topic / method / task / contribution tag, then proposes concepts and builds a fresh hierarchy (Agents 1 & 2, ~10 min). It does NOT tag items yet — you review the new tree in the sidebar first.\n\nKEPT: manual project/… tags, BibTeX, notes, status, and computed venue / type / site.\n\nBuild the hierarchy now?",
                  )
                )
                  startJob("build-vocab");
              }}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3.5 py-2 text-[12.5px] font-medium text-white transition-all hover:brightness-105 active:scale-[0.98]"
            >
              <Wand2 size={13} /> 1 · Build hierarchy
            </button>
            <span className="text-[11.5px] text-muted">
              erases topics, then proposes + builds the tree — <b>review it in the sidebar</b>
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => {
                if (
                  window.confirm(
                    "Tag every item into the current hierarchy (Agent 3, per-item, batches of 10, ~10–15 min)? Watch the job bar — it reports 'tagging items: X/N'.",
                  )
                )
                  startJob("tag-all");
              }}
              className="inline-flex items-center gap-1.5 rounded-lg border border-accent px-3.5 py-2 text-[12.5px] font-medium text-accent transition-all hover:bg-accent hover:text-white active:scale-[0.98]"
            >
              <Wand2 size={13} /> 2 · Tag everything
            </button>
            <span className="text-[11.5px] text-muted">
              only once the hierarchy looks right — shows “tagging items: X / N”
            </span>
          </div>
        </div>
      </div>

      <div>
        <SectionLabel>Never track these domains</SectionLabel>
        <p className="mb-2 text-[12.5px] text-muted">
          Pages on these hosts are never stored. Add your own below — they apply on
          top of the built-in list shown here.
        </p>
        <details className="mb-3 rounded-lg border border-line/60 bg-surface/40">
          <summary className="cursor-pointer select-none px-3 py-2 text-[12.5px] text-muted">
            Built in — always excluded ({draft.builtin_blocklist.reduce((n, g) => n + g.hosts.length, 0)} hosts)
          </summary>
          <div className="space-y-2 border-t border-line/60 px-3 py-2.5">
            {draft.builtin_blocklist.map((g) => (
              <div key={g.label}>
                <p className="text-[11px] font-medium text-muted">{g.label}</p>
                <div className="mt-1 flex flex-wrap gap-1">
                  {g.hosts.map((h) => (
                    <span
                      key={h}
                      className="rounded bg-line/40 px-1.5 py-0.5 font-mono text-[10.5px] text-muted"
                    >
                      {h}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </details>
        <p className="mb-1.5 text-[12px] font-medium">Your domains</p>
        <div className="mb-2 flex flex-wrap gap-1.5">
          {draft.blocklist.map((d) => (
            <span
              key={d}
              className="inline-flex items-center gap-1 rounded-md bg-accent-soft px-2 py-0.5 font-mono text-[11.5px] text-accent"
            >
              {d}
              <button onClick={() => set({ blocklist: draft.blocklist.filter((x) => x !== d) })}>
                <X size={10} />
              </button>
            </span>
          ))}
          {draft.blocklist.length === 0 && (
            <span className="text-[12px] text-muted">none added yet</span>
          )}
        </div>
        <div className="flex gap-1.5">
          <input
            value={newDomain}
            onChange={(e) => setNewDomain(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addDomain()}
            placeholder="example.com"
            className={clsx(input, "max-w-xs")}
          />
          <button onClick={addDomain} className="rounded-lg border border-line px-2 text-muted hover:text-accent">
            <Plus size={14} />
          </button>
        </div>
      </div>

      <div>
        <SectionLabel>Save to Cairn — browser extension</SectionLabel>
        <p className="mb-2 text-[12.5px] text-muted">
          One shortcut, one way to save. Press <span className="font-mono">⌘⇧E</span> (or click the
          toolbar icon) on any page and a small popup lets you file the tab into a collection /
          branch, Zotero-style — then it saves to your library. No macOS permissions; it shows a
          clear “Saved&nbsp;✓”.
        </p>
        <ol className="ml-4 list-decimal space-y-1 text-[12.5px] text-muted">
          <li>
            Open <span className="font-mono text-ink">chrome://extensions</span> and turn on{" "}
            <b>Developer mode</b> (top-right).
          </li>
          <li>
            Click <b>Load unpacked</b> and choose the{" "}
            <span className="font-mono text-ink">extension</span> folder in the Cairn project.
          </li>
          <li>
            Set/verify the key at{" "}
            <span className="font-mono text-ink">chrome://extensions/shortcuts</span> — it suggests{" "}
            <span className="font-mono">⌘⇧E</span>; change it to whatever you like.
          </li>
        </ol>
        <p className="mt-2 font-mono text-[11px] text-muted">folder: &lt;cairn-project&gt;/extension</p>
      </div>

      <div className="flex items-center gap-3 border-t border-line pt-4">
        <Button onClick={() => save.mutate(draft)} disabled={!dirty || save.isPending}>
          {save.isPending ? (
            <Loader2 size={13} className="animate-spin" />
          ) : saved ? (
            <Check size={13} />
          ) : null}
          {saved ? "Saved" : "Save changes"}
        </Button>
        {dirty && !save.isPending && <span className="text-[12px] text-muted">unsaved changes</span>}
        <span className="ml-auto font-mono text-[10.5px] text-muted">
          agents: {Object.values(draft.agents).filter((s) => s === "loaded").length}/
          {Object.keys(draft.agents).length} loaded
        </span>
      </div>
    </div>
  );
}
