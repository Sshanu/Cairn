export type Item = {
  id: number;
  canonical_url: string;
  raw_url: string | null;
  title: string | null;
  authors: string | null;
  venue: string | null;
  year: number | null;
  abstract: string | null;
  summary: string | null;
  notes: string | null;
  source: string | null;
  kind: string | null;
  status: string;
  bucket: string;
  first_seen: string | null;
  saved_at: string | null;
  age_days: number | null;
  tags: string[];
  has_bibtex?: boolean;
  bibtex?: string | null;
  bibtex_source?: string | null;
  bibtex_venue?: string | null;
  bibtex_published?: number | null;
  alt_urls?: string[];
  relevance?: string | null;
};

export type RelatedItem = {
  id: number;
  title: string | null;
  canonical_url: string;
  source: string | null;
  venue: string | null;
  year: number | null;
  shared: number;
};

export type Bibtex = {
  bibtex: string | null;
  source: string | null;
  venue: string | null;
  published: boolean;
  found?: boolean;
};

export type HistoryItem = Pick<
  Item,
  "id" | "canonical_url" | "title" | "source" | "venue" | "year"
>;
export type HistoryDay = {
  date: string;
  count: number;
  items: HistoryItem[];
};

export type MapItem = {
  id: number;
  title: string | null;
  topic: string | null;
  source: string | null;
};

export type Digest = {
  added_week: number;
  read: number;
  reading: number;
  stale_unread: number;
  clusters: { name: string; count: number }[];
  read_next: { id: number; title: string | null; canonical_url: string; age_days: number | null }[];
};

export type ServerSettings = {
  ingest_interval_min: number;
  poll_interval_min: number;
  backup_interval_hours: number;
  backup_destination: string;
  backup_folder: string;
  github_repo: string;
  blocklist: string[];
  builtin_blocklist: { label: string; hosts: string[] }[];
  backup_here: string;
  cloud_options: string[];
  auto_organize: boolean;
  auto_file_projects: boolean;
  auto_summary: boolean;
  related_citations: boolean;
  weekly_digest: boolean;
  organization_guidance: string;
  organization_guidance_default: string;
  classify_prompt: string;
  classify_prompt_default: string;
  tag_proposal_prompt: string;
  tag_proposal_prompt_default: string;
  hierarchy_prompt: string;
  hierarchy_prompt_default: string;
  facets: string[];
  facets_default: string[];
  agent_provider: string;
  agent_model: string;
  agent_base_url: string;
  agent_api_key_set: boolean;
  agent_api_key?: string; // write-only
  reasoning_effort: string;
  tracking_paused: boolean;
  agents: Record<string, string>;
  purged_blocked?: number; // response-only: items removed because a domain was blocked
};

export type TagNode = {
  name: string;
  label: string;
  depth: number;
  count: number;
  own: number;
  description?: string | null;
};

export type Stats = {
  total: number;
  untagged: number;
  no_topic: number;
  queued: number;
  stale: number;
  database: string;
  status: Record<string, number>;
  sources: Record<string, number>;
  buckets: Record<string, number>;
  tags: { name: string; count: number }[];
};

export type StaleTab = {
  canonical_url: string;
  raw_url: string;
  title: string | null;
  first_seen: string | null;
  age_days: number | null;
};

export type Answer = {
  question: string;
  answer: string;
  retrieved: number;
  items: { id: number; title: string | null; canonical_url: string }[];
};

export type Job = {
  id: string;
  kind: string;
  state: "running" | "done" | "failed";
  log: string[];
  result: Record<string, unknown> | null;
  error: string | null;
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // Never hang forever: abort after 90s (long enough for a slow agent call, short
  // enough that a wedged request surfaces as an error the UI can show).
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 90_000);
  let res: Response;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...init,
      signal: controller.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error("Request timed out — the server may be busy. Try again.");
    }
    throw new Error("Can't reach the server. Is it running?");
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
}

export type ItemQuery = {
  q?: string;
  tag?: string;
  status?: string;
  source?: string;
  bucket?: string;
  untagged?: boolean;
  no_topic?: boolean;
  sort?: string;
  limit?: number;
  offset?: number;
};

export const api = {
  build: () => req<{ build: string }>("/api/build"),
  revision: () =>
    req<{ revision: string }>("/api/revision", { cache: "no-store" }),
  agentTest: () =>
    req<{
      ok: boolean;
      name: string | null;
      model: string | null;
      latency_ms: number;
      error: string | null;
      codex_path?: string | null;
      codex_version?: string | null;
      codex_min_version?: string;
      supports_output_schema?: boolean;
    }>("/api/agent/test", { method: "POST" }),

  stats: () => req<Stats>("/api/stats"),

  items: (params: ItemQuery) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "" && v !== false) qs.set(k, String(v));
    });
    return req<{ items: Item[]; total: number }>(`/api/items?${qs}`);
  },

  item: (id: number) => req<Item>(`/api/items/${id}`),

  related: (id: number) =>
    req<{ enabled: boolean; related: RelatedItem[] }>(`/api/items/${id}/related`),

  summarize: (id: number) =>
    req<{ summary?: string; relevance?: string }>(`/api/items/${id}/summarize`, {
      method: "POST",
    }),

  capture: (url: string, title: string, tags = "") =>
    req<{ saved: boolean; title?: string; reason?: string }>(
      `/api/capture?url=${encodeURIComponent(url)}&title=${encodeURIComponent(title)}&tags=${encodeURIComponent(tags)}`,
      { method: "POST" },
    ),

  getSettings: () => req<ServerSettings>("/api/settings"),

  putSettings: (patch: Partial<ServerSettings>) =>
    req<ServerSettings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),

  clearOrganization: (keepCustom: boolean) =>
    req<{ removed_tags: number }>(
      `/api/organization/clear?keep_custom=${keepCustom}`,
      { method: "POST" },
    ),

  digest: () => req<Digest>("/api/digest"),

  map: (bucket: string) =>
    req<{ items: MapItem[] }>(`/api/map?bucket=${encodeURIComponent(bucket)}`),

  history: (opts: { bucket?: string; before?: string; days?: number } = {}) => {
    const qs = new URLSearchParams();
    if (opts.bucket) qs.set("bucket", opts.bucket);
    if (opts.before) qs.set("before", opts.before);
    if (opts.days) qs.set("days", String(opts.days));
    return req<{ days: HistoryDay[]; next_before: string | null }>(
      `/api/history?${qs}`,
    );
  },

  bibtex: (id: number) => req<Bibtex>(`/api/items/${id}/bibtex`),

  refreshBibtex: (id: number) =>
    req<Bibtex>(`/api/items/${id}/bibtex`, { method: "POST" }),

  tags: () => req<{ tags: TagNode[]; flat: { name: string; count: number }[] }>("/api/tags"),

  createTag: (name: string) =>
    req<{ name: string }>("/api/tags", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  renameTag: (from: string, to: string) =>
    req<{ renamed: number; merged: number }>(`/api/tags/${encodeURI(from)}`, {
      method: "PATCH",
      body: JSON.stringify({ name: to }),
    }),

  describeTag: (name: string, description: string) =>
    req<{ name: string; description: string }>(
      `/api/tags/${encodeURI(name)}/description`,
      { method: "PUT", body: JSON.stringify({ description }) },
    ),

  deleteTag: (name: string, withChildren = false) =>
    req<{ removed: number }>(
      `/api/tags/${encodeURI(name)}?with_children=${withChildren}`,
      { method: "DELETE" },
    ),

  mergeTags: (sources: string[], target: string) =>
    req<{ moved: number }>("/api/tags/merge", {
      method: "POST",
      body: JSON.stringify({ sources, target }),
    }),

  patchItem: (
    id: number,
    patch: {
      status?: string;
      notes?: string;
      bucket?: string;
      title?: string;
      authors?: string;
      venue?: string;
      year?: number;
      bibtex?: string;
    },
  ) =>
    req<Item>(`/api/items/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),

  deleteItem: (id: number) =>
    req<{ deleted: number }>(`/api/items/${id}`, { method: "DELETE" }),

  bulkDelete: (ids: number[]) =>
    req<{ deleted: number }>("/api/items/bulk/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  mergeItems: (ids: number[]) =>
    req<{ primary: number; merged: number }>("/api/items/merge", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),

  exportBibtex: (body: {
    ids?: number[];
    q?: string;
    tag?: string;
    status?: string;
    bucket?: string;
    untagged?: boolean;
    no_topic?: boolean;
  }) =>
    req<{
      bibtex: string;
      total: number;
      with_bibtex: number;
      missing: { id: number; title: string | null }[];
    }>("/api/export/bibtex", { method: "POST", body: JSON.stringify(body) }),

  addTags: (id: number, tags: string[]) =>
    req<Item>(`/api/items/${id}/tags`, {
      method: "POST",
      body: JSON.stringify({ tags }),
    }),

  removeTag: (id: number, name: string) =>
    req<Item>(`/api/items/${id}/tags/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  bulk: (
    ids: number[],
    patch: {
      status?: string;
      tags?: string[];
      remove_tags?: string[];
      bucket?: string;
    },
  ) =>
    req<{ changed: number }>("/api/items/bulk", {
      method: "POST",
      body: JSON.stringify({ ids, ...patch }),
    }),

  triage: (days: number) =>
    req<{ tabs: StaleTab[]; error: string | null; days: number }>(
      `/api/triage?days=${days}`,
    ),

  closeTabs: (urls: string[]) =>
    req<{ closed: number }>("/api/triage/close", {
      method: "POST",
      body: JSON.stringify({ urls }),
    }),

  dupes: () =>
    req<{ groups: { id: number; title: string; source: string; canonical_url: string }[][] }>(
      "/api/dupes",
    ),

  mergeDupes: () =>
    req<{ groups: number; removed: number }>("/api/dupes/merge", { method: "POST" }),

  ask: (question: string) =>
    req<Answer>("/api/ask", { method: "POST", body: JSON.stringify({ question }) }),

  startJob: (kind: string, body: Record<string, unknown> = {}) =>
    req<{ job: string }>(`/api/jobs/${kind}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  job: (id: string) => req<Job>(`/api/jobs/${id}`),
};
