"""URL to stable identity.

Pure functions. No I/O, no model, no network. This module is the reason the
library collapses five arXiv variants into one record, and it contains no
intelligence at all -- just a table of rules.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# Parameters that carry no identity, only attribution.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_reader",
        "utm_referrer",
        "gclid",
        "gclsrc",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "ref_src",
        "ref_url",
        "spm",
        "at_medium",
        "at_campaign",
        "oly_anon_id",
        "oly_enc_id",
    }
)

# New-style (2401.12345v2) and old-style (cs.CL/0501001v1) arXiv identifiers.
_ARXIV_RE = re.compile(
    r"(?:(\d{4}\.\d{4,5})|([a-z][a-z-]*(?:\.[A-Za-z]{2})?/\d{7}))(v\d+)?"
)

# Hosts that mirror arXiv under a path containing the arXiv id.
_ARXIV_HOSTS = ("arxiv.org", "ar5iv.org", "alphaxiv.org", "xxx.lanl.gov")

# 2023.acl-long.1 (modern) and P19-1001 (legacy) anthology ids.
_ACL_RE = re.compile(r"^(?:\d{4}\.[a-z0-9\-]+\.\d+|[A-Z]\d{2}-\d{4})$")

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+")

# Hosts where a DOI in the path is authoritative rather than incidental.
_DOI_PATH_HOSTS = ("link.springer.com", "onlinelibrary.wiley.com", "dl.acm.org")

# Conference sites that publish papers under their own URL scheme.
_VENUE_HOSTS = {
    "openaccess.thecvf.com": "cvf",
    "proceedings.neurips.cc": "neurips",
    "papers.nips.cc": "neurips",
    "proceedings.mlr.press": "pmlr",
    "ojs.aaai.org": "aaai",
    "ecva.net": "ecva",
    "ieeexplore.ieee.org": "ieee",
}


def canonicalize(url: str) -> str:
    """Map a raw URL onto the identity it shares with its variants."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        # chrome://, file://, about: -- leave verbatim, they are not library material.
        return url

    host = (parts.hostname or "").lower()
    path = parts.path or ""
    query = dict(parse_qsl(parts.query, keep_blank_values=True))

    arxiv_id = _arxiv_id(host, path, query)
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"

    openreview_id = _openreview_id(host, query)
    if openreview_id:
        return f"https://openreview.net/forum?id={openreview_id}"

    acl_id = _acl_id(host, path)
    if acl_id:
        return f"https://aclanthology.org/{acl_id}/"

    venue = _venue(host, path)
    if venue:
        return venue

    doi = _doi(host, path, query)
    if doi:
        # arXiv mints a DOI for every preprint: 10.48550/arXiv.2510.00855 IS
        # arxiv.org/abs/2510.00855. Collapse it onto the arXiv identity so the
        # preprint and its DOI are one item, not two duplicates.
        from_arxiv = _arxiv_from_doi(doi)
        if from_arxiv:
            return f"https://arxiv.org/abs/{from_arxiv}"
        # ACL mints 10.18653/v1/<anthology-id>: the id is right there, so treat
        # it as the Anthology paper -- which gets the venue facet and the real
        # on-page BibTeX rather than a bare doi.org row with no conference.
        from_acl = _acl_from_doi(doi)
        if from_acl:
            return f"https://aclanthology.org/{from_acl}/"
        return f"https://doi.org/{doi}"

    return _generic(parts)


def _arxiv_from_doi(doi: str) -> str | None:
    """The arXiv id inside an arXiv-issued DOI, or None for any other DOI."""
    match = re.match(r"10\.48550/arxiv\.(.+)$", doi, re.IGNORECASE)
    return _normalize_arxiv(match.group(1)) if match else None


def _acl_from_doi(doi: str) -> str | None:
    """The Anthology id inside an ACL-issued DOI, or None for any other DOI."""
    match = re.match(r"10\.18653/v1/(.+)$", doi, re.IGNORECASE)
    if not match:
        return None
    anthology_id = match.group(1)
    return anthology_id if _ACL_RE.match(anthology_id) else None


def source_of(canonical_url: str) -> tuple[str, str | None]:
    """Classify a canonical URL as (source, source_id)."""
    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").lower()
    if host == "arxiv.org":
        return "arxiv", parts.path.removeprefix("/abs/")
    if host == "openreview.net":
        return "openreview", dict(parse_qsl(parts.query)).get("id")
    if host == "aclanthology.org":
        return "acl", parts.path.strip("/")
    if host == "doi.org":
        return "doi", parts.path.lstrip("/")
    for base, name in _VENUE_HOSTS.items():
        if _host_matches(host, base):
            return name, parts.path.strip("/") or None
    return "web", None


# Sources whose items are papers regardless of what the page looks like.
PAPER_SOURCES = frozenset(
    {"arxiv", "openreview", "acl", "doi", *_VENUE_HOSTS.values()}
)


def is_paper(source: str) -> bool:
    return source in PAPER_SOURCES


# --- per-source rules -------------------------------------------------------


def _host_matches(host: str, base: str) -> bool:
    return host == base or host.endswith("." + base)


def _arxiv_id(host: str, path: str, query: dict[str, str]) -> str | None:
    raw = None
    if any(_host_matches(host, h) for h in _ARXIV_HOSTS):
        raw = _find_arxiv(path) or _find_arxiv(query.get("id", ""))
    elif _host_matches(host, "huggingface.co") and path.startswith("/papers/"):
        raw = _find_arxiv(path)
    elif _host_matches(host, "semanticscholar.org") and "/arxiv/" in path.lower():
        raw = _find_arxiv(path)
    if not raw:
        return None
    return _normalize_arxiv(raw)


def _find_arxiv(text: str) -> str | None:
    match = _ARXIV_RE.search(text or "")
    return match.group(0) if match else None


def _normalize_arxiv(raw: str) -> str:
    """Drop the version suffix and normalize old-style archive/subject casing."""
    match = _ARXIV_RE.fullmatch(raw)
    if not match:
        return raw
    new_style, old_style, _version = match.groups()
    if new_style:
        return new_style
    archive, number = old_style.split("/", 1)
    if "." in archive:
        head, tail = archive.split(".", 1)
        archive = head.lower() + "." + tail.upper()
    else:
        archive = archive.lower()
    return f"{archive}/{number}"


def _openreview_id(host: str, query: dict[str, str]) -> str | None:
    if not _host_matches(host, "openreview.net"):
        return None
    # forum?id=, pdf?id= and attachment?id= all name the same note.
    return query.get("id") or None


def _acl_id(host: str, path: str) -> str | None:
    if not _host_matches(host, "aclanthology.org"):
        return None
    segment = path.strip("/")
    for suffix in (".pdf", ".bib", ".abs", ".xml"):
        if segment.lower().endswith(suffix):
            segment = segment[: -len(suffix)]
            break
    if _ACL_RE.match(segment):
        return segment
    # Newer anthology PDF URLs nest the id in a path, e.g.
    # /anthology-files/anthology-files/pdf/findings/2025.findings-emnlp.98.pdf --
    # the anthology id is the final path component, not the whole path.
    last = segment.rsplit("/", 1)[-1]
    return last if _ACL_RE.match(last) else None


# --- conference venues ------------------------------------------------------
#
# Every rule below collapses the PDF and the landing page for one paper onto a
# single identity. Without them a CVPR paper opened twice -- once from the PDF
# link, once from the abstract page -- is two records.


def _cvf(path: str) -> str | None:
    """openaccess.thecvf.com: /content{,_CVPR_2019}/[CONF/]{papers,html}/<slug>.{pdf,html}"""
    match = re.match(
        r"^/(content[^/]*)(?:/([^/]+))?/(?:papers|html)/(.+?)(?:\.pdf|\.html)?$", path
    )
    if not match:
        return None
    segments = [s for s in (match.group(1), match.group(2), "html") if s]
    return f"https://openaccess.thecvf.com/{'/'.join(segments)}/{match.group(3)}.html"


def _neurips(path: str) -> str | None:
    """proceedings.neurips.cc: the PDF and the abstract page are one paper."""
    if "/paper" not in path:
        return None
    normalized = path.replace("/file/", "/hash/")
    normalized = re.sub(r"-Paper", "-Abstract", normalized)
    normalized = re.sub(r"\.pdf$", ".html", normalized)
    if not re.search(r"/hash/[0-9a-f]+-Abstract", normalized):
        return None
    return f"https://proceedings.neurips.cc{normalized}"


def _pmlr(path: str) -> str | None:
    """proceedings.mlr.press: /v202/name23a.html and /v202/name23a/name23a.pdf"""
    match = re.match(r"^/(v\d+)/([^/]+?)(?:/[^/]+)?\.(?:html|pdf)$", path)
    if not match:
        return None
    return f"https://proceedings.mlr.press/{match.group(1)}/{match.group(2)}.html"


def _aaai(path: str) -> str | None:
    """ojs.aaai.org: /article/view/<id> and the /view/<id>/<galley> download."""
    match = re.match(r"^(/index\.php/[^/]+/article/view/\d+)(?:/\d+)?/?$", path)
    if not match:
        return None
    return f"https://ojs.aaai.org{match.group(1)}"


def _ecva(path: str) -> str | None:
    """ecva.net: ECCV papers, PDF and landing page."""
    match = re.match(r"^/papers/([^/]+)/papers_ECCV/(?:papers|html)/(.+?)\.(?:pdf|php)$", path)
    if not match:
        return None
    return (
        f"https://www.ecva.net/papers/{match.group(1)}/papers_ECCV/html/"
        f"{match.group(2)}.php"
    )


def _ieee(path: str) -> str | None:
    match = re.match(r"^/document/(\d+)", path)
    if not match:
        return None
    return f"https://ieeexplore.ieee.org/document/{match.group(1)}"


_VENUE_RULES: tuple[tuple[str, str, object], ...] = (
    ("openaccess.thecvf.com", "cvf", _cvf),
    ("proceedings.neurips.cc", "neurips", _neurips),
    ("papers.nips.cc", "neurips", _neurips),
    ("proceedings.mlr.press", "pmlr", _pmlr),
    ("ojs.aaai.org", "aaai", _aaai),
    ("ecva.net", "ecva", _ecva),
    ("ieeexplore.ieee.org", "ieee", _ieee),
)

def _venue(host: str, path: str) -> str | None:
    for base, _name, rule in _VENUE_RULES:
        if _host_matches(host, base):
            return rule(path)
    return None


def _doi(host: str, path: str, query: dict[str, str]) -> str | None:
    if host in {"doi.org", "dx.doi.org", "www.doi.org"}:
        match = _DOI_RE.search(unquote(path))
    elif any(_host_matches(host, h) for h in _DOI_PATH_HOSTS):
        match = _DOI_RE.search(unquote(path))
    elif query.get("doi"):
        match = _DOI_RE.search(unquote(query["doi"]))
    elif "/doi/" in path:
        match = _DOI_RE.search(unquote(path))
    else:
        return None
    if not match:
        return None
    return match.group(0).rstrip(".,;)").lower()


def _generic(parts) -> str:
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    # `parts.port` raises ValueError on a malformed/out-of-range port (e.g. a
    # localhost:99999 tab). One such open tab must not abort the whole ingest.
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        host = f"{host}:{port}"

    path = parts.path or ""
    if len(path) > 1:
        path = path.rstrip("/")
    if path == "/":
        path = ""

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    kept.sort()

    return urlunsplit(("https", host, path, urlencode(kept), ""))
