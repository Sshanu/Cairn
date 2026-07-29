import { useState } from "react";
import { RotateCcw } from "lucide-react";

import { DEFAULTS, load, reset, save, type Settings as S } from "../settings";
import { Button, SectionLabel } from "../ui";
import { BackendSettings } from "../components/BackendSettings";

const ACCENTS = ["#d97757", "#5b8266", "#4a6fa5", "#8a6aa8", "#b5651d", "#1f1e1d"];

/** Everything the user can change, in one box: appearance first (local, resets
 *  freely), then the server-backed settings — cadence, backup, what's never
 *  tracked, and the three organizing agents. Nothing hidden.
 */
export function SettingsPage() {
  const [settings, setSettings] = useState<S>(load);
  const set = (patch: Partial<S>) => setSettings(save(patch));

  return (
    <div className="mx-auto max-w-2xl px-8 py-6">
      <div className="mb-6 flex items-baseline gap-3">
        <h1 className="font-display text-[26px] leading-none">Settings</h1>
        <button
          onClick={() => setSettings(reset())}
          className="ml-auto inline-flex items-center gap-1 text-[12.5px] text-muted hover:text-accent"
        >
          <RotateCcw size={12} /> reset all
        </button>
      </div>

      <p className="mb-6 text-sm text-muted">
        Appearance is local to this browser and resets freely. Everything below the
        divider is server-backed — cadence, backup, what’s never tracked, and how the
        three organizing agents work.
      </p>

      <div className="rounded-xl border border-line bg-surface/40 p-5">
        {/* Appearance — local, at the top. */}
        <div className="space-y-7">
          <div>
            <SectionLabel>Accent</SectionLabel>
            <div className="flex gap-2">
              {ACCENTS.map((c) => (
                <button
                  key={c}
                  onClick={() => set({ accent: c })}
                  aria-label={c}
                  className="h-7 w-7 rounded-full border-2 transition-transform hover:scale-110"
                  style={{
                    background: c,
                    borderColor: settings.accent === c ? "var(--color-ink)" : "transparent",
                  }}
                />
              ))}
              <input
                type="color"
                value={settings.accent}
                onChange={(e) => set({ accent: e.target.value })}
                className="h-7 w-7 cursor-pointer rounded-full border border-line bg-transparent"
              />
            </div>
          </div>

          <div>
            <SectionLabel>Theme</SectionLabel>
            <div className="flex gap-1.5">
              {(["system", "light", "dark"] as const).map((t) => (
                <Button
                  key={t}
                  variant={settings.theme === t ? "primary" : "ghost"}
                  className="!px-3 !py-1 !text-xs"
                  onClick={() => set({ theme: t })}
                >
                  {t}
                </Button>
              ))}
            </div>
          </div>

          <div>
            <SectionLabel>Row density</SectionLabel>
            <div className="flex gap-1.5">
              {(["comfortable", "compact"] as const).map((d) => (
                <Button
                  key={d}
                  variant={settings.density === d ? "primary" : "ghost"}
                  className="!px-3 !py-1 !text-xs"
                  onClick={() => set({ density: d })}
                >
                  {d}
                </Button>
              ))}
            </div>
          </div>

          <div>
            <SectionLabel>In the item list</SectionLabel>
            <label className="flex items-center gap-2 py-1 text-sm">
              <input
                type="checkbox"
                checked={settings.showAgeSpine}
                onChange={(e) => set({ showAgeSpine: e.target.checked })}
                className="h-3.5 w-3.5 accent-[var(--color-accent)]"
              />
              Age spine — the left rule that thickens as a paper goes unread
            </label>
            <label className="flex items-center gap-2 py-1 text-sm">
              <input
                type="checkbox"
                checked={settings.showTagsInList}
                onChange={(e) => set({ showTagsInList: e.target.checked })}
                className="h-3.5 w-3.5 accent-[var(--color-accent)]"
              />
              Tags under each row
            </label>
          </div>

          {/* Facet order lives where you actually change it -- by dragging the
              sidebar headers -- so a read-only copy here was just clutter. The one
              still-useful control (clearing renamed labels) stays, conditionally. */}
          {Object.keys(settings.facetLabels).length > 0 && (
            <div>
              <SectionLabel>Renamed facet labels</SectionLabel>
              <button
                onClick={() => set({ facetLabels: {} })}
                className="text-[12.5px] text-muted hover:text-accent"
              >
                Reset {Object.keys(settings.facetLabels).length} renamed label
                {Object.keys(settings.facetLabels).length === 1 ? "" : "s"} to defaults
              </button>
            </div>
          )}

          <div>
            <SectionLabel>Defaults</SectionLabel>
            <p className="font-mono text-[11.5px] leading-relaxed text-muted">
              accent {DEFAULTS.accent} · sidebar {DEFAULTS.sidebarWidth}px · view{" "}
              {DEFAULTS.view} · density {DEFAULTS.density}
            </p>
          </div>
        </div>

        <div className="my-7 border-t border-line" />

        {/* Server-backed: agent, features, cadence, backup, blocklist, organization. */}
        <BackendSettings />
      </div>
    </div>
  );
}
