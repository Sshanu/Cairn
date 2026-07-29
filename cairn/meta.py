"""arXiv, OpenReview, ACL Anthology, Crossref and generic web metadata.

Deterministic first: every one of these sources returns clean structured data
over HTTP. The one legitimate model job at capture time is extracting author and
date from an unstructured blog, and that is a fallback in enrich.py, not the path.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

from .canonical import source_of

USER_AGENT = "cairn/0.1 (+https://github.com/local/cairn)"
TIMEOUT = 12


class MetaError(RuntimeError):
    pass


def _get(url: str, accept: str = "*/*", timeout: int = TIMEOUT, attempts: int = 3) -> bytes:
    """Fetch with a short retry ladder.

    Conference sites rate-limit bursts, and a concurrent backfill is a burst:
    a single-shot fetch silently returned nothing for 45 CVF papers that serve
    perfectly well one at a time.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": accept}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(4_000_000)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            last = exc
            code = getattr(exc, "code", None)
            if code and code not in (429, 500, 502, 503, 504):
                raise           # a real 404 will not improve on retry
            time.sleep(1.5 * (attempt + 1))
    raise last if last else MetaError("fetch failed")


def resolve(canonical_url: str, raw_url: str | None = None, fetch_body: bool = True) -> dict:
    """Best-effort metadata for a canonical URL. Never raises on network failure.

    On failure the caller still gets source, source_id and kind -- all of which
    come from the URL alone -- plus an `_error` key describing what went wrong.
    Metadata is a nicety; identity and storage are the contract.
    """
    source, source_id = source_of(canonical_url)
    meta: dict = {"source": source, "source_id": source_id, "kind": "paper"}

    try:
        if source == "arxiv" and source_id:
            meta.update(_arxiv(source_id))
        elif source == "openreview" and source_id:
            meta.update(_openreview(source_id))
        elif source == "acl" and source_id:
            meta.update(_acl(source_id))
        elif source == "doi" and source_id:
            meta.update(_crossref(source_id))
        else:
            meta["kind"] = "paper" if source != "web" else "blog"
            # Fetch the canonical landing page, not the raw tab URL: for a
            # conference site the raw URL is usually the PDF, which has no
            # citation_* meta tags to read.
            fields = _generic(canonical_url, fetch_body=fetch_body)
            if not fields.get("title") and raw_url and raw_url != canonical_url:
                fields = _generic(raw_url, fetch_body=fetch_body)
            meta.update(fields)
    except (
        MetaError,
        urllib.error.URLError,
        OSError,
        ElementTree.ParseError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        meta["_error"] = f"{type(exc).__name__}: {exc}"

    return {key: value for key, value in meta.items() if value not in (None, "")}


# --- arXiv ------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def arxiv_batch(arxiv_ids: list[str], chunk: int = 50) -> dict[str, dict]:
    """Resolve many arXiv ids in a few calls instead of one call each.

    The arXiv API takes a comma-separated id_list, so 153 papers cost 4 requests.
    Failures are silent per chunk: callers fall back to the tab title.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(arxiv_ids), chunk):
        batch = [i for i in arxiv_ids[start : start + chunk] if i]
        if not batch:
            continue
        try:
            raw = _get(
                "http://export.arxiv.org/api/query?id_list="
                + ",".join(batch)
                + f"&max_results={len(batch)}",
                timeout=40,
            )
            root = ElementTree.fromstring(raw)
        except (urllib.error.URLError, OSError, ElementTree.ParseError):
            continue

        for entry in root.findall(f"{_ATOM}entry"):
            entry_id = entry.findtext(f"{_ATOM}id") or ""
            match = re.search(r"abs/(.+?)(v\d+)?$", entry_id)
            if not match:
                continue
            out[match.group(1)] = _entry_fields(entry)
    return out


def _entry_fields(entry) -> dict:
    published = (entry.findtext(f"{_ATOM}published") or "")[:4]
    journal = entry.findtext(f"{_ARXIV_NS}journal_ref")
    category = entry.find(f"{_ARXIV_NS}primary_category")
    return {
        "kind": "paper",
        "source": "arxiv",
        "title": _clean(entry.findtext(f"{_ATOM}title")),
        "abstract": _clean(entry.findtext(f"{_ATOM}summary")),
        "authors": ", ".join(
            _clean(author.findtext(f"{_ATOM}name")) or ""
            for author in entry.findall(f"{_ATOM}author")
        ).strip(", "),
        "year": int(published) if published.isdigit() else None,
        "venue": _clean(journal) or (category.get("term") if category is not None else None),
    }


def _arxiv(arxiv_id: str) -> dict:
    raw = _get(f"http://export.arxiv.org/api/query?id_list={arxiv_id}&max_results=1")
    root = ElementTree.fromstring(raw)
    entry = root.find(f"{_ATOM}entry")
    if entry is None:
        return {}

    published = (entry.findtext(f"{_ATOM}published") or "")[:4]
    journal = entry.findtext(f"{_ARXIV_NS}journal_ref")
    category = entry.find(f"{_ARXIV_NS}primary_category")
    return {
        "title": _clean(entry.findtext(f"{_ATOM}title")),
        "abstract": _clean(entry.findtext(f"{_ATOM}summary")),
        "authors": ", ".join(
            _clean(author.findtext(f"{_ATOM}name")) or ""
            for author in entry.findall(f"{_ATOM}author")
        ).strip(", "),
        "year": int(published) if published.isdigit() else None,
        "venue": _clean(journal) or (category.get("term") if category is not None else None),
    }


# --- OpenReview -------------------------------------------------------------


def _openreview(note_id: str) -> dict:
    """api2 first, then the legacy api1 host.

    Both currently answer scripted clients with 403 ChallengeRequiredError --
    OpenReview put its API behind browser bot-protection. Nothing to work around
    from here, so the failure is reported rather than swallowed: the item still
    saves, keeping Chrome's tab title (which on a forum page is the paper title).
    """
    last: Exception | None = None
    for host in ("api2.openreview.net", "api.openreview.net"):
        try:
            payload = json.loads(_get(f"https://{host}/notes?id={note_id}"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            last = exc
            continue
        notes = payload.get("notes") or []
        if not notes:
            continue
        content = notes[0].get("content") or {}
        return {
            "title": _or_value(content.get("title")),
            "abstract": _or_value(content.get("abstract")),
            "authors": _or_list(content.get("authors")),
            "venue": _or_value(content.get("venue")) or _or_value(content.get("venueid")),
            "year": _year_from(_or_value(content.get("venue")) or ""),
        }
    if last is not None:
        raise MetaError(f"OpenReview API unreachable ({last})")
    return {}


def _or_value(field):
    """api2 wraps every field as {"value": ...}; api1 returns it bare."""
    if isinstance(field, dict):
        field = field.get("value")
    return _clean(field) if isinstance(field, str) else None


def _or_list(field):
    if isinstance(field, dict):
        field = field.get("value")
    if isinstance(field, list):
        return ", ".join(str(x) for x in field)
    return _clean(field) if isinstance(field, str) else None


# --- ACL Anthology ----------------------------------------------------------

_BIB_FIELD = re.compile(r"^\s*(\w+)\s*=\s*[\"{](.*?)[\"}],?\s*$", re.MULTILINE | re.DOTALL)


def _acl(anthology_id: str) -> dict:
    text = _get(f"https://aclanthology.org/{anthology_id}.bib").decode("utf-8", "replace")
    fields = {key.lower(): _clean(value) for key, value in _BIB_FIELD.findall(text)}
    # BibTeX wraps words in braces to lock their case ("{CLIP}"); strip them so the
    # stored title reads "CLIP", not "{CLIP}".
    def unbrace(value: str | None) -> str | None:
        return value.replace("{", "").replace("}", "") if value else value

    year = fields.get("year", "")
    return {
        "title": unbrace(fields.get("title")),
        "abstract": unbrace(fields.get("abstract")),
        "authors": (fields.get("author") or "").replace(" and ", ", ") or None,
        "venue": unbrace(fields.get("booktitle") or fields.get("journal")),
        "year": int(year) if year.isdigit() else None,
    }


# --- DOI / Crossref ---------------------------------------------------------


def _crossref(doi: str) -> dict:
    payload = json.loads(_get(f"https://api.crossref.org/works/{doi}"))
    work = payload.get("message") or {}
    authors = ", ".join(
        " ".join(filter(None, [a.get("given"), a.get("family")]))
        for a in work.get("author", [])
    )
    parts = (work.get("issued") or {}).get("date-parts") or [[]]
    year = parts[0][0] if parts and parts[0] else None
    return {
        "title": _clean((work.get("title") or [None])[0]),
        "abstract": _strip_tags(work.get("abstract")),
        "authors": authors or None,
        "venue": _clean((work.get("container-title") or [None])[0]),
        "year": int(year) if isinstance(year, int) else None,
    }


# --- generic web ------------------------------------------------------------


class _MetaParser(HTMLParser):
    """Meta tags, <title>, and any element that announces itself as the abstract.

    Conference sites do not agree on this. arXiv and Crossref expose the
    abstract over an API, but CVF, ECVA and several others put it only in a
    `<div id="abstract">` -- so a meta-tags-only parser silently returns
    nothing for a large slice of a real library.
    """

    ABSTRACT_MARKERS = ("abstract", "abstracttext", "abstract-text")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.abstract_parts: list[str] = []
        self._in_title = False
        self._abstract_depth = 0
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if self._abstract_depth:
            self._depth += 1
        elif (attributes.get("id") or "").lower() in self.ABSTRACT_MARKERS or (
            attributes.get("class") or ""
        ).lower() in self.ABSTRACT_MARKERS:
            self._abstract_depth = 1
            self._depth = 1

        if tag == "title":
            self._in_title = True
            return
        if tag != "meta":
            return
        key = attributes.get("name") or attributes.get("property") or ""
        content = attributes.get("content")
        if key and content:
            self.meta.setdefault(key.lower(), content)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self._abstract_depth:
            self._depth -= 1
            if self._depth <= 0:
                self._abstract_depth = 0

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._abstract_depth:
            self.abstract_parts.append(data)


def _generic(url: str, fetch_body: bool = True) -> dict:
    html = _get(url, accept="text/html").decode("utf-8", "replace")
    parser = _MetaParser()
    parser.feed(html)
    meta = parser.meta

    title = (
        meta.get("citation_title")
        or meta.get("og:title")
        or _clean("".join(parser.title_parts))
    )
    date = (
        meta.get("citation_publication_date")
        or meta.get("article:published_time")
        or meta.get("date")
        or ""
    )
    # Prefer a real abstract element over a meta description, which on a
    # conference page is usually boilerplate about the proceedings.
    in_page = _clean("".join(parser.abstract_parts))
    if in_page and in_page.lower().startswith("abstract"):
        in_page = _clean(in_page[len("abstract"):].lstrip(":. "))

    return {
        "title": _clean(title),
        "abstract": in_page
        or _clean(meta.get("citation_abstract"))
        or _clean(meta.get("description") or meta.get("og:description")),
        "authors": _clean(meta.get("citation_author") or meta.get("author")),
        "venue": _clean(meta.get("og:site_name") or (urlsplit(url).hostname or "")),
        "year": _year_from(date),
        "body": _extract_body(html) if fetch_body else None,
    }


def _extract_body(html: str) -> str | None:
    """Full text via trafilatura when the [extract] extra is installed."""
    try:
        import trafilatura  # type: ignore
    except ImportError:
        return None
    try:
        return trafilatura.extract(html) or None
    except Exception:  # pragma: no cover - third-party parser
        return None


# --- helpers ----------------------------------------------------------------


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip() or None


def _strip_tags(text: str | None) -> str | None:
    if not text:
        return None
    return _clean(re.sub(r"<[^>]+>", " ", text))


def _year_from(text: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", text or "")
    return int(match.group(0)) if match else None


def openreview_id_from(url: str) -> str | None:
    return dict(parse_qsl(urlsplit(url).query)).get("id")
