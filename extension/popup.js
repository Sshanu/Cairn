// Zotero-style save popup with a DRILL-DOWN collection picker: start at the top-level
// branches, click to go deeper, breadcrumb to go back, or type to search everything.
const API = "http://localhost:8765";
const $ = (id) => document.getElementById(id);
let ALL = []; // fileable branch names (topics + collections; computed facets excluded)
let path = ""; // current location in the tree AND the chosen destination
let tab = null;
let META = {}; // {title, abstract, authors} scraped from the loaded page
let metaPromise = null; // in-flight metadata load; save() awaits it so a fast click still sends it

// Every network call is bounded: a restarting or wedged server must never leave the
// popup hanging (that was the "takes a minute to load" -- a fetch with no timeout will
// sit on a half-open localhost socket for ~60s). Short timeout, throws on expiry.
async function fetchJSON(path, ms = 3000, opts = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    const r = await fetch(API + path, { ...opts, signal: ctrl.signal });
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

// Read metadata straight from the loaded page. The Cairn server can't fetch a
// bot-protected site (OpenReview sits behind a Cloudflare wall), but the browser has
// already rendered it -- so the paper's citation_* meta tags are right here to hand over.
// This is what lets OpenReview / JS-heavy papers arrive with a real title + abstract.
async function scrapePageMeta(tabId) {
  try {
    const [hit] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const one = (sel) => (document.querySelector(sel)?.content || "").trim();
        const many = (sel) =>
          [...document.querySelectorAll(sel)].map((m) => (m.content || "").trim()).filter(Boolean);
        return {
          title:
            one('meta[name="citation_title"]') ||
            one('meta[property="og:title"]') ||
            (document.title || "").trim(),
          abstract:
            one('meta[name="citation_abstract"]') ||
            one('meta[name="description"]') ||
            one('meta[property="og:description"]'),
          authors: many('meta[name="citation_author"]').join(", "),
        };
      },
    });
    return (hit && hit.result) || {};
  } catch {
    return {}; // restricted page or no host access -- fall back to the tab title
  }
}

// OpenReview's API 403s the Cairn server (Cloudflare), and its PDF viewer has no HTML
// to scrape -- so read it from the browser instead. This fetch carries the openreview
// cookies the user's session already cleared, so it gets through where the server can't,
// and it works from the /pdf viewer too (the id is right there in the URL).
async function fetchOpenReview(url) {
  const m = url.match(/openreview\.net\/(?:forum|pdf|attachment|references)\?[^#]*\bid=([\w-]+)/i);
  if (!m) return {};
  const id = m[1];
  const val = (f) => (f && typeof f === "object" && "value" in f ? f.value : f);
  for (const host of ["https://api2.openreview.net", "https://api.openreview.net"]) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 4000); // never hang the save on a stalled API
      const r = await fetch(`${host}/notes?id=${encodeURIComponent(id)}`, {
        credentials: "include",
        signal: ctrl.signal,
      });
      clearTimeout(timer);
      if (!r.ok) continue;
      const c = (((await r.json()).notes || [])[0] || {}).content || {};
      const title = String(val(c.title) || "").trim();
      if (!title) continue;
      let authors = val(c.authors);
      authors = Array.isArray(authors) ? authors.join(", ") : String(authors || "");
      return {
        title,
        abstract: String(val(c.abstract) || "").trim(),
        authors,
        // OpenReview's own generated BibTeX -- the server can't fetch it (Cloudflare),
        // so hand it over from the browser, where we already have the note.
        bibtex: String(val(c._bibtex) || "").trim(),
      };
    } catch {
      /* try the next host */
    }
  }
  return {};
}

function setStatus(msg, kind) {
  const s = $("status");
  s.textContent = msg;
  s.className = "status " + (kind || "muted");
}

// distinct next segments under a path, from the flat name list
function childrenOf(p) {
  const pre = p ? p + "/" : "";
  const set = new Set();
  for (const n of ALL) {
    if (!n.startsWith(pre)) continue;
    const rest = n.slice(pre.length);
    if (rest) set.add(rest.split("/")[0]);
  }
  return [...set].sort();
}
const hasKids = (full) => childrenOf(full).length > 0;

// The branch a typed name would create. A "/" makes it an absolute path; otherwise it
// is created UNDER the branch you're currently in (drill into project, type "thesis"
// -> project/thesis). Normalised the same way the server stores tags.
function createTarget(f) {
  const norm = f
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^a-z0-9/\-]/g, "")
    .replace(/\/+/g, "/")
    .replace(/^\/+|\/+$/g, "");
  if (!norm) return "";
  // A "/" makes it absolute; a bare name is created UNDER wherever you're drilled in.
  return norm.includes("/") ? norm : path ? path + "/" + norm : norm;
}

function crumb(label, p) {
  const s = document.createElement("span");
  s.className = "crumb";
  s.textContent = label;
  s.onclick = () => {
    path = p;
    $("filter").value = "";
    render();
  };
  return s;
}

function render() {
  // breadcrumb: all › topic › vision-language-models › …
  const c = $("crumbs");
  c.innerHTML = "";
  c.appendChild(crumb("all", ""));
  let acc = "";
  for (const seg of path ? path.split("/") : []) {
    acc = acc ? acc + "/" + seg : seg;
    c.appendChild(document.createTextNode(" › "));
    c.appendChild(crumb(seg, acc));
  }

  // list: filtered global search, or the children at the current level
  const list = $("list");
  list.innerHTML = "";
  const f = $("filter").value.trim();
  const fl = f.toLowerCase();

  // Create-new row: when the typed name isn't already a branch, offer to make it.
  const target = f ? createTarget(f) : "";
  if (target && !ALL.includes(target)) {
    const row = document.createElement("div");
    row.className = "row create";
    const lbl = document.createElement("span");
    lbl.className = "lbl";
    lbl.appendChild(document.createTextNode("＋ create & file into "));
    const b = document.createElement("b");
    b.textContent = target;
    lbl.appendChild(b);
    row.appendChild(lbl);
    row.onclick = () => {
      path = target; // doesn't exist yet -> the save creates it
      $("filter").value = "";
      render();
    };
    list.appendChild(row);
  }

  const rows = f
    ? ALL.filter((n) => n.toLowerCase().includes(fl))
        .slice(0, 60)
        .map((n) => ({ full: n, label: n, leaf: !hasKids(n) }))
    : (() => {
        const segs = childrenOf(path);
        // Always offer "collection" and "project" at the top level, even with none yet,
        // so making your FIRST collection is possible: drill in, then type a name ->
        // collection/<name>. (Typing a bare name at the top would make a loose tag, not
        // a collection -- this is what makes the right prefix discoverable.)
        if (!path) for (const root of ["collection", "project"]) if (!segs.includes(root)) segs.push(root);
        segs.sort();
        return segs.map((seg) => {
          const full = path ? path + "/" + seg : seg;
          const forcedFolder = !path && (seg === "collection" || seg === "project");
          return { full, label: seg, leaf: forcedFolder ? false : !hasKids(full) };
        });
      })();
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "row";
    const lbl = document.createElement("span");
    lbl.className = "lbl";
    lbl.textContent = r.label;
    row.appendChild(lbl);
    if (!r.leaf) {
      const a = document.createElement("span");
      a.className = "arr";
      a.textContent = "›";
      row.appendChild(a);
    }
    row.onclick = () => {
      path = r.full;
      $("filter").value = "";
      render();
    };
    list.appendChild(row);
  }
  if (!list.children.length) {
    list.innerHTML = '<div class="empty">nothing deeper</div>';
  }

  // chosen destination (the current path); "clear" files into the inbox instead
  const ch = $("chosen");
  ch.innerHTML = "";
  if (path) {
    ch.appendChild(document.createTextNode("Filing into: "));
    const b = document.createElement("b");
    b.textContent = path;
    ch.appendChild(b);
    const clr = document.createElement("span");
    clr.className = "clr";
    clr.textContent = "clear";
    clr.onclick = () => {
      path = "";
      render();
    };
    ch.appendChild(clr);
  } else {
    const m = document.createElement("span");
    m.className = "muted";
    m.textContent = "Filing into: Inbox (no collection)";
    ch.appendChild(m);
  }
}

// The URL is already in the library: show it, and the collections/branches it's in.
function renderExisting(d) {
  const tags = d.tags || [];
  const cols = tags.filter((t) => t.startsWith("collection/")).map((t) => t.slice("collection/".length));
  const other = tags.filter((t) => !t.startsWith("collection/"));
  const el = $("existing");
  el.innerHTML = "";
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.textContent = "✓ Already in your library";
  el.appendChild(badge);
  const chips = document.createElement("div");
  chips.className = "chips";
  const labels = [...cols.map((c) => "📁 " + c), ...other];
  if (labels.length) {
    for (const t of labels) {
      const s = document.createElement("span");
      s.className = "chip";
      s.textContent = t;
      chips.appendChild(s);
    }
  } else {
    const s = document.createElement("span");
    s.className = "none";
    s.textContent = "not filed in any collection yet";
    chips.appendChild(s);
  }
  el.appendChild(chips);
  $("fileLabel").textContent = "Add to a collection / branch (optional)";
  $("save").textContent = "Update";
}

async function init() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  // Draw the popup immediately with what we already have -- the tab title. Nothing
  // below blocks the first paint.
  $("title").textContent = (tab && (tab.title || tab.url)) || "(no tab)";
  $("url").textContent = (tab && tab.url) || "";
  $("filter").addEventListener("input", render);
  $("save").addEventListener("click", save);
  $("tags").addEventListener("keydown", (e) => e.key === "Enter" && save());

  // Branches drive the picker. Render INSTANTLY from the last cached list so the popup
  // is never blank while the server answers (or if it's mid-restart), then refresh in
  // the background with a hard timeout.
  try {
    const cached = (await chrome.storage.local.get("branches")).branches;
    if (Array.isArray(cached) && cached.length) ALL = cached;
  } catch {
    /* no cache yet */
  }
  render();
  try {
    const fresh = (await fetchJSON("/api/branches", 3000)).branches;
    if (Array.isArray(fresh)) {
      ALL = fresh;
      chrome.storage.local.set({ branches: fresh });
      render();
    }
  } catch {
    if (!ALL.length) setStatus("Cairn isn't running — open the app first.", "err");
  }

  // Enrichment runs in the BACKGROUND so a slow network round-trip (OpenReview's API)
  // never delays the popup. The title updates when metadata lands; save() awaits the
  // same promise, so even a quick click still sends the real title/abstract.
  if (tab && /^https?:/i.test(tab.url || "")) {
    metaPromise = loadMeta();
    loadExisting();
  }
}

async function loadMeta() {
  // OpenReview: its API from the browser (works on the PDF viewer too). Everything
  // else: the page's meta tags.
  if (/openreview\.net/i.test(tab.url)) META = await fetchOpenReview(tab.url);
  if ((!META || !META.title) && tab.id != null) META = await scrapePageMeta(tab.id);
  // Still nothing? A PDF viewer has no HTML to scrape -- so ask the server what a save
  // would extract (it resolves ACL / arXiv / DOI / conference metadata over the web).
  // This is what puts the real title in the popup for a PDF instead of the file name.
  if (!META || !META.title) {
    try {
      // /api/resolve fetches the page server-side (network), so give it longer -- but
      // still bounded so it can't hang the save's metadata wait forever.
      const d = await fetchJSON("/api/resolve?url=" + encodeURIComponent(tab.url), 8000);
      if (d && d.title) META = { title: d.title, abstract: d.abstract || "", authors: d.authors || "" };
    } catch {
      /* offline or not resolvable -- fall back to the tab title */
    }
  }
  if (META && META.title) $("title").textContent = META.title;
}

async function loadExisting() {
  try {
    const d = await fetchJSON("/api/lookup?url=" + encodeURIComponent(tab.url), 3000);
    if (d && d.exists) renderExisting(d);
  } catch {
    /* offline / server down -- already surfaced by the branches fetch */
  }
}

async function save() {
  if (!tab || !/^https?:/i.test(tab.url || "")) {
    setStatus("Nothing to save on this tab.", "err");
    return;
  }
  const parts = [];
  if (path) parts.push(path);
  for (const t of $("tags").value.split(",")) {
    const s = t.trim();
    if (s) parts.push(s);
  }
  $("save").disabled = true;
  setStatus("Saving…", "muted");
  // Wait for the background metadata, but only briefly -- never hang the save on it.
  // If it isn't ready in time, save with the tab title and let the server fill the rest.
  if (metaPromise) {
    await Promise.race([
      metaPromise.catch(() => {}),
      new Promise((r) => setTimeout(r, 4000)),
    ]);
  }
  // Prefer the fetched title/abstract/authors (real metadata even for a bot-protected
  // site); the abstract is capped so the request line stays sane.
  const u =
    API +
    "/api/capture?url=" +
    encodeURIComponent(tab.url) +
    "&title=" +
    encodeURIComponent((META && META.title) || tab.title || "") +
    "&abstract=" +
    encodeURIComponent(((META && META.abstract) || "").slice(0, 5000)) +
    "&authors=" +
    encodeURIComponent((META && META.authors) || "") +
    "&bibtex=" +
    encodeURIComponent((META && META.bibtex) || "") +
    "&tags=" +
    encodeURIComponent(parts.join(","));
  try {
    // Bounded: a dead or wedged server must surface an error, not spin "Saving…" forever.
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 15000);
    const res = await fetch(u, { method: "POST", signal: ctrl.signal });
    clearTimeout(timer);
    const data = await res.json().catch(() => ({}));
    if (data.saved === false) {
      setStatus("Skipped: " + (data.reason || "blocked"), "err");
      $("save").disabled = false;
      return;
    }
    setStatus("Saved ✓" + (path ? " → " + path : ""), "ok");
    setTimeout(() => window.close(), 850);
  } catch (e) {
    setStatus(
      e && e.name === "AbortError"
        ? "Timed out — is Cairn running at localhost:8765?"
        : "Cairn isn't running — open the app first.",
      "err",
    );
    $("save").disabled = false;
  }
}

init();
