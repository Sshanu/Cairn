"""BibTeX for a paper, preferring the published venue over the arXiv preprint.

The rule the whole module is built around: a researcher citing a paper wants the
conference or journal entry, not `@misc{...archivePrefix=arXiv}`. So resolution
is two stages -- first work out where the paper was actually *published*, then
fetch a clean, canonical BibTeX for that publication:

  * a DOI resolves straight to the publisher's own BibTeX (CrossRef) -- published
    by definition;
  * an ACL Anthology id serves its conference BibTeX directly;
  * an arXiv id is the hard case: the preprint id tells you nothing about where
    the work landed, so we look it up on DBLP (by author + title, which is
    precise where a bare-title search drowns in common words) and take the
    non-preprint entry; failing that we ask Semantic Scholar, which dedupes
    preprint and publication into one record and names the venue.

Only when every published source comes up empty do we fall back to an arXiv
`@misc` entry -- and we say so, so the caller can show "preprint" rather than
imply a publication that does not exist. Every network call fails soft: the
worst outcome is a BibTeX built from the metadata already in the row.
"""

from __future__ import annotations

import html as _html
import json
import re
import sqlite3
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from . import db, meta
from .canonical import PAPER_SOURCES, is_paper, source_of

# Conference sites that print the exact BibTeX on the paper page itself.
_ONPAGE_SOURCES = {"cvf", "ecva"}

# Venues that mean "this is still a preprint", never a publication to cite as one.
_PREPRINT_VENUES = {"", "arxiv", "corr", "arxiv preprint", "arxiv.org"}

# Venue-host papers whose published entry we look up on DBLP.
_VENUE_SOURCES = {"cvf", "neurips", "pmlr", "aaai", "ecva", "ieee", "openreview"}


@dataclass
class BibResult:
    bibtex: str
    source: str          # machine tag: crossref | dblp | acl | venue-page | arxiv
    venue: str | None    # human venue the entry is for, for display
    published: bool      # True if this is the published version, False for a preprint
    error: str | None = None
    title: str | None = None  # the paper title, when a source carried it (one fetch, both fixed)

    @property
    def ok(self) -> bool:
        return bool(self.bibtex) and self.error is None


def pending(
    conn: sqlite3.Connection,
    *,
    redo: bool = False,
    upgrade: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Paper items needing a BibTeX.

    Default: only those with none yet. ``upgrade`` also re-takes anything that
    landed as a preprint or a bare constructed stub -- so a second run, when
    DBLP and Semantic Scholar are not throttling, can promote those to the
    published entry without disturbing the ones already confirmed published.
    ``redo`` re-resolves everything.
    """
    placeholders = ",".join("?" for _ in PAPER_SOURCES)
    where = f"source IN ({placeholders})"
    params: list = list(PAPER_SOURCES)
    if redo:
        pass
    elif upgrade:
        where += " AND (bibtex IS NULL OR bibtex_published = 0)"
    else:
        where += " AND bibtex IS NULL"
    sql = f"SELECT * FROM items WHERE {where} ORDER BY saved_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def backfill(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    workers: int = 4,
    on_result: Callable[[sqlite3.Row, "BibResult"], None] | None = None,
) -> list[tuple[sqlite3.Row, "BibResult"]]:
    """Resolve many items concurrently and store each entry.

    The network work runs in a thread pool; the SQLite writes happen back on the
    caller's thread as each future lands, respecting SQLite's single-writer rule.
    Concurrency is kept modest on purpose -- DBLP and CrossRef throttle bursts,
    and the retry ladder in meta already backs off on 429.
    """
    resolved: list[tuple[sqlite3.Row, BibResult]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(for_item, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # a resolver bug must not sink the batch
                result = BibResult("", "error", None, False, error=str(exc))
            if result.ok:
                db.set_bibtex(
                    conn, row["id"],
                    bibtex=result.bibtex, source=result.source,
                    venue=result.venue, published=result.published,
                )
                # One fetch fixed both: if the page carried the real title and the
                # stored one is the site's generic chrome, repair it here too.
                from .ingest import looks_generic

                if result.title and looks_generic(row["title"]) and not looks_generic(result.title):
                    db.set_fields(conn, row["id"], title=result.title)
            elif row["bibtex"] and row["bibtex_source"] != "manual":
                # Re-resolution found nothing this time -- clear a previously
                # stored auto entry, but never touch one the user typed.
                db.clear_bibtex(conn, row["id"])
            if on_result:
                on_result(row, result)
            resolved.append((row, result))
    return resolved


def for_item(item: sqlite3.Row | dict) -> BibResult:
    """Best available BibTeX for one item. Never raises."""
    get = item.__getitem__
    canonical = get("canonical_url")
    source, source_id = source_of(canonical)
    if not is_paper(source):
        return BibResult("", "none", None, False, error="not a paper")

    title = _field(item, "title")
    authors = _field(item, "authors")
    venue = _field(item, "venue")
    year = _year(item)

    # 1. ACL Anthology serves the published conference BibTeX as a file.
    if source == "acl" and source_id:
        got = _from_acl(source_id)
        if got:
            return got

    # 2. A DOI is a published-version pointer; CrossRef gives the publisher's bib.
    if source == "doi" and source_id:
        got = _from_crossref(source_id)
        if got:
            return got

    # 3. arXiv: the preprint id says nothing about publication. Go find it.
    if source == "arxiv" and source_id:
        got = _resolve_arxiv(source_id, title, authors, year)
        if got:
            return got

    # 4. CVF and ECVA print the exact entry on the page -- the authoritative
    #    citation, and it carries the real title even when the stored tab title
    #    is the generic "Open Access Repository". Prefer it over everything.
    if source in _ONPAGE_SOURCES:
        got = _from_venue_page(canonical)
        if got:
            return got

    # 5. Other venue-host papers (NeurIPS, PMLR, OpenReview, ...): the published
    #    entry from DBLP if it has one.
    if source in _VENUE_SOURCES:
        got = _from_dblp(title, authors, year)
        if got:
            return got

    # 6. Nothing to extract. We never fabricate an entry -- an empty BibTeX the
    #    user can paste a real one into is honest; a generated one that looks
    #    real is not. The caller stores nothing and the UI offers a paste box.
    return BibResult("", "none", None, False, error="no BibTeX found online")


# --- arXiv -> published resolution ------------------------------------------


def _resolve_arxiv(arxiv_id: str, title: str, authors: str, year: int | None) -> BibResult | None:
    """The heart of the module: find where an arXiv paper was published."""
    # DBLP first: clean canonical output, no rate limit, and its author+title
    # search is precise. A non-CoRR hit is the published version.
    got = _from_dblp(title, authors, year)
    if got:
        return got

    # DBLP found nothing published -- maybe the title-less/authorless row gave a
    # weak query. Semantic Scholar keys on the arXiv id directly and dedupes the
    # preprint into the publication, so it can still name the venue.
    detected = _semanticscholar(arxiv_id)
    if detected and detected[0]:
        got = _from_crossref(detected[0])  # published DOI -> publisher BibTeX
        if got:
            return got

    # Not published anywhere we can find: arXiv's OWN BibTeX export -- a real
    # entry served by arXiv, extracted, never assembled by us.
    return _from_arxiv_export(arxiv_id)


# --- individual sources -----------------------------------------------------


def _from_arxiv_export(arxiv_id: str) -> BibResult | None:
    """arXiv's own BibTeX export for a preprint -- extracted, not generated."""
    try:
        text = meta._get(
            f"https://arxiv.org/bibtex/{arxiv_id}", accept="application/x-bibtex"
        ).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, meta.MetaError):
        return None
    text = _tidy(text)
    if not text.lstrip().startswith("@"):
        return None
    return BibResult(text, "arxiv", _venue_of(text), published=False)


def _from_venue_page(url: str) -> BibResult | None:
    """Extract the BibTeX a conference site prints on the paper page itself.

    CVF and ECVA render the exact entry inside a `<div class="bibref">` -- every
    author, the full booktitle, the page range. Nothing we could assemble from
    stored metadata competes with the citation the publisher wrote, so this is
    tried before DBLP and before any construction.
    """
    if not url:
        return None
    try:
        page = meta._get(url, accept="text/html").decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, meta.MetaError):
        return None
    match = re.search(r'<div[^>]*class="bibref[^"]*"[^>]*>(.*?)</div>', page, re.S | re.I)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", "", match.group(1))  # strip any inline markup
    text = _tidy(_html.unescape(text))
    if not text.lstrip().startswith("@"):
        return None
    # Same fetch also yields the real title, so a generic tab title ("Open
    # Access Repository") is fixed without a second request to a rate-limited host.
    title = None
    meta_title = re.search(r'<meta name="citation_title" content="([^"]+)"', page)
    if meta_title:
        title = _html.unescape(meta_title.group(1)).strip()
    else:
        bib_title = re.search(r"title\s*=\s*\{(.+?)\}\s*,", text, re.S)
        if bib_title:
            title = _WS.sub(" ", _html.unescape(bib_title.group(1))).strip()
    return BibResult(text, "venue-page", _venue_of(text), published=True, title=title)


def _from_acl(anthology_id: str) -> BibResult | None:
    try:
        text = meta._get(
            f"https://aclanthology.org/{anthology_id}.bib", accept="application/x-bibtex"
        ).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, meta.MetaError):
        return None
    text = _tidy(text)
    if "@" not in text:
        return None
    return BibResult(text, "acl", _venue_of(text), published=True)


def _from_crossref(doi: str) -> BibResult | None:
    doi = doi.strip().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    try:
        text = meta._get(
            f"https://doi.org/{doi}", accept="application/x-bibtex"
        ).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, meta.MetaError):
        return None
    text = _tidy(text)
    if "@" not in text:
        return None
    return BibResult(text, "crossref", _venue_of(text), published=True)


def _from_dblp(title: str, authors: str, year: int | None) -> BibResult | None:
    """Find the published DBLP entry for a title and fetch its clean .bib.

    Queried by first-author surname plus title: a bare-title search buries
    common-word titles ("... diffusion models") under everything that shares a
    word, while adding the surname pins the exact paper.
    """
    if not title:
        return None
    surname = _first_surname(authors)
    # Feed DBLP words, not punctuation: a colon or quote in the title derails its
    # query parser and a clearly-indexed paper comes back with no hits.
    query = _WS.sub(" ", f"{surname} {_NONWORD.sub(' ', title.lower())}").strip()
    try:
        payload = json.loads(
            meta._get(
                "https://dblp.org/search/publ/api?"
                + urllib.parse.urlencode({"q": query, "format": "json", "h": 30})
            )
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError, meta.MetaError):
        return None

    hits = (((payload.get("result") or {}).get("hits") or {}).get("hit")) or []
    want = _norm(title)
    published: list[tuple[int, str, str]] = []
    for hit in hits:
        info = hit.get("info") or {}
        if _similar(_norm(info.get("title", "")), want) < 0.93:
            continue
        venue = info.get("venue")
        venue = venue if isinstance(venue, str) else "/".join(venue) if isinstance(venue, list) else ""
        key = info.get("key") or ""
        if not key or _norm(venue) == "corr":
            continue  # CoRR is the arXiv mirror -- skip, we want the publication
        hit_year = int(info["year"]) if str(info.get("year", "")).isdigit() else 0
        published.append((hit_year, venue, key))

    if not published:
        return None
    # Prefer the earliest real publication (the venue it first appeared at),
    # which for a conference paper is the entry a reader expects to cite.
    _, venue, key = min(published, key=lambda p: p[0] or 9999)
    try:
        text = meta._get(f"https://dblp.org/rec/{key}.bib?param=1").decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, meta.MetaError):
        return None
    text = _tidy(text)
    if "@" not in text:
        return None
    return BibResult(text, "dblp", _venue_of(text) or venue, published=True)


def _semanticscholar(arxiv_id: str) -> tuple[str | None, str | None] | None:
    """Detect the published (doi, venue) for an arXiv id. Detector only."""
    fields = "venue,year,externalIds,publicationVenue,publicationTypes"
    try:
        data = json.loads(
            meta._get(
                f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{arxiv_id}?fields={fields}",
                accept="application/json",
            )
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError, meta.MetaError):
        return None
    doi = (data.get("externalIds") or {}).get("DOI")
    venue = data.get("venue") or ((data.get("publicationVenue") or {}).get("name"))
    return (doi, venue)


# --- text helpers -----------------------------------------------------------

_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9]+")
# booktitle/journal anywhere (CrossRef emits one line). Braces allow one level
# of nesting so DBLP's {{IEEE/CVF} Conference ...} is captured whole; the quoted
# alternative is how ACL Anthology delimits the same field.
_BOOKTITLE = re.compile(
    r"(?:booktitle|journal)\s*=\s*"
    r"(?:\{((?:[^{}]|\{[^{}]*\})*)\}|\"([^\"]*)\")",
    re.IGNORECASE,
)
# Tab titles keep a site suffix the real title never has.
_TAB_SUFFIX = re.compile(
    r"\s*[|–—-]\s*(openreview(\.net)?|arxiv(\.org)?|papers with code|"
    r"proceedings.*|neurips.*|semantic scholar|acl anthology)\s*$",
    re.IGNORECASE,
)


def _field(item, name: str) -> str:
    try:
        value = item[name]
    except (KeyError, IndexError):
        return ""
    return (value or "").strip() if isinstance(value, str) else ""


def _year(item) -> int | None:
    try:
        value = item["year"]
    except (KeyError, IndexError):
        return None
    return value if isinstance(value, int) else None


def _norm(text: str) -> str:
    return _NONWORD.sub(" ", (text or "").lower()).strip()


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def _is_preprint_venue(venue: str | None) -> bool:
    return _norm(venue or "") in _PREPRINT_VENUES


def _first_surname(authors: str) -> str:
    """Last token of the first author -- good enough to disambiguate a search."""
    if not authors:
        return ""
    first = re.split(r"[,;]| and ", authors.strip())[0].strip()
    parts = first.split()
    return parts[-1] if parts else ""


def _clean_title(title: str) -> str:
    """Drop the ' | OpenReview'-style site suffix an unenriched tab title keeps."""
    title = (title or "").strip()
    prev = None
    while title != prev:  # a title can carry more than one suffix
        prev = title
        title = _TAB_SUFFIX.sub("", title).strip()
    return title


def _venue_of(bibtex: str) -> str | None:
    match = _BOOKTITLE.search(bibtex or "")
    if not match:
        return None
    raw = match.group(1) or match.group(2) or ""
    venue = _WS.sub(" ", raw).replace("{", "").replace("}", "").strip()
    return venue or None


def _tidy(bibtex: str) -> str:
    return (bibtex or "").strip() + "\n"
