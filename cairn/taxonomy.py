"""The three organising axes, and which of them a model is allowed to decide.

The previous single `type` facet conflated three unrelated questions, so it
answered none of them well. They are now separate:

  type/          what kind of thing the URL is       -- computed, never guessed
  contribution/  what a paper contributes            -- model, papers only
  topic/         what it is about                    -- model, everything

`type` is derived from the URL because the URL already knows: arxiv.org is a
paper, huggingface.co/datasets is a dataset, github.com is code. Asking a model
to infer it produced 229 `type/paper` tags against 275 actual paper URLs.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .canonical import PAPER_SOURCES

# --- axis 1: type, computed from the URL ------------------------------------

TYPES = (
    "type/paper",
    "type/preprint",
    "type/dataset",
    "type/model",
    "type/code",
    "type/blog",
    "type/docs",
    "type/collection",
    "type/thesis",
    "type/other",
)

_BLOG_HOSTS = (
    "transformer-circuits.pub", "distill.pub", "openai.com", "ai.meta.com",
    "deepmind.google", "anthropic.com", "blog.google", "research.google",
    "medium.com", "substack.com", "lilianweng.github.io", "colah.github.io",
    "thegradient.pub", "bair.berkeley.edu", "ai.googleblog.com",
)

_DOCS_HOSTS = (
    "wikipedia.org", "readthedocs.io", "docs.python.org", "pytorch.org",
    "docs.ultralytics.com", "scikit-learn.org", "numpy.org",
)

_CODE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org")


def _host_matches(host: str, candidates) -> bool:
    return any(host == c or host.endswith("." + c) for c in candidates)


def type_for(canonical_url: str, source: str | None = None) -> str:
    """The one type tag for a URL. Deterministic: no model, no network."""
    if source is None:
        # Never depend on the caller to have looked it up: the URL knows.
        from .canonical import source_of

        source, _ = source_of(canonical_url)
    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    segments = [s for s in path.split("/") if s]

    if _host_matches(host, ("huggingface.co",)):
        head = segments[0] if segments else ""
        if head == "datasets":
            return "type/dataset"
        if head == "papers":
            return "type/preprint"
        if head in ("collections", "blog"):
            return "type/collection" if head == "collections" else "type/blog"
        if head in ("spaces",):
            return "type/code"
        return "type/model" if len(segments) >= 2 else "type/collection"

    if _host_matches(host, _CODE_HOSTS):
        return "type/code"
    if _host_matches(host, _BLOG_HOSTS):
        return "type/blog"
    if _host_matches(host, _DOCS_HOSTS):
        return "type/docs"

    # arXiv and OpenReview are preprints; a venue or DOI means it was published.
    if source == "arxiv" or host == "arxiv.org":
        return "type/preprint"
    if source == "openreview" or host == "openreview.net":
        return "type/preprint"
    if source in PAPER_SOURCES:
        return "type/paper"

    if re.search(r"\.(pdf)$", path, re.I) or "thesis" in path.lower():
        return "type/thesis" if "thesis" in path.lower() else "type/paper"
    # A personal academic site publishing a project page reads as a blog.
    if host.endswith(".github.io"):
        return "type/blog"

    return "type/other"


def is_paperish(type_tag: str) -> bool:
    return type_tag in ("type/paper", "type/preprint", "type/thesis")


# --- axis 1b: venue, computed for papers ------------------------------------
#
# Which conference a paper appeared at is a fact printed in its URL, and it is
# how researchers actually navigate ("the CVPR ones", "what did I read from
# NeurIPS 2024"). Deriving it costs nothing and cannot be wrong.

_CVF_CONF = re.compile(r"/(?:content_)?([A-Za-z]+)_?(\d{4})", re.I)
_ACL_MODERN = re.compile(r"^(\d{4})\.([a-z0-9\-]+?)(?:-\w+)?\.\d+$")
_ACL_LEGACY = re.compile(r"^([A-Z])(\d{2})-\d{4}$")

_ACL_VENUES = {
    "acl": "acl", "emnlp": "emnlp", "naacl": "naacl", "eacl": "eacl",
    "findings": "findings", "coling": "coling", "lrec": "lrec",
    "tacl": "tacl", "conll": "conll", "wmt": "wmt", "semeval": "semeval",
}
_ACL_LEGACY_LETTER = {
    "P": "acl", "D": "emnlp", "N": "naacl", "E": "eacl", "C": "coling",
    "W": "workshop", "L": "lrec", "Q": "tacl", "K": "conll",
}


def venue_tags(
    canonical_url: str,
    source: str | None = None,
    venue_text: str | None = None,
    year: int | None = None,
) -> list[str]:
    """`venue/<conference>[/<year>]` for a paper, or [] when it is not one."""
    parts = urlsplit(canonical_url)
    host = (parts.hostname or "").lower()
    path = parts.path or ""

    conference: str | None = None
    venue_year: int | None = year

    if _host_matches(host, ("openaccess.thecvf.com",)):
        match = _CVF_CONF.search(path)
        if match:
            conference = match.group(1).lower()
            venue_year = int(match.group(2))
    elif _host_matches(host, ("proceedings.neurips.cc", "papers.nips.cc")):
        conference = "neurips"
        match = re.search(r"/paper/(\d{4})/", path)
        if match:
            venue_year = int(match.group(1))
    elif _host_matches(host, ("proceedings.mlr.press",)):
        conference = "pmlr"
    elif _host_matches(host, ("ojs.aaai.org",)):
        conference = "aaai"
    elif _host_matches(host, ("ecva.net",)):
        conference = "eccv"
        match = re.search(r"eccv_(\d{4})", path)
        if match:
            venue_year = int(match.group(1))
    elif _host_matches(host, ("aclanthology.org",)):
        slug = path.strip("/")
        modern = _ACL_MODERN.match(slug)
        legacy = _ACL_LEGACY.match(slug)
        if modern:
            venue_year = int(modern.group(1))
            key = slug.split(".")[1] if "." in slug else modern.group(2)
            # `2025.findings-acl.258` is ACL Findings, not a venue called
            # "findings": strip the track prefix and keep the conference.
            parts_ = [p for p in key.split("-") if p]
            track = parts_[0] if parts_ and parts_[0] in ("findings",) else None
            if track:
                parts_ = parts_[1:]
            base = next((p for p in parts_ if p in _ACL_VENUES), parts_[0] if parts_ else "")
            conference = _ACL_VENUES.get(base, base)
            if track and conference:
                conference = f"{conference}/{track}"
        elif legacy:
            conference = _ACL_LEGACY_LETTER.get(legacy.group(1), "acl")
            venue_year = 1900 + int(legacy.group(2)) if int(legacy.group(2)) > 50 else 2000 + int(legacy.group(2))
    elif source == "arxiv" or host == "arxiv.org":
        conference = "arxiv"
    elif source == "openreview" or host == "openreview.net":
        # OpenReview hosts many venues; the note's own venue string is the only
        # reliable signal, and it is often absent because the API is blocked.
        conference = _venue_from_text(venue_text) or "openreview"
    elif source in PAPER_SOURCES:
        conference = _venue_from_text(venue_text)

    if not conference:
        return []

    conference = re.sub(r"[^a-z0-9\-/]", "", conference.lower()).strip("/")
    if not conference:
        return []
    tags = [f"venue/{conference}"]
    if venue_year and 1980 < venue_year < 2100:
        tags.append(f"venue/{conference}/{venue_year}")
    return tags


_KNOWN_VENUES = (
    "neurips", "nips", "icml", "iclr", "cvpr", "iccv", "eccv", "wacv", "bmvc",
    "acl", "emnlp", "naacl", "eacl", "coling", "aaai", "ijcai", "kdd", "sigir",
    "www", "chi", "interspeech", "icassp", "miccai", "siggraph", "uai", "aistats",
    # Speech/translation and *ACL satellites that turn up via DOI, where the
    # conference is named in the venue string but never in the URL.
    "iwslt", "wmt", "conll", "tacl", "lrec", "sigdial", "blackboxnlp", "starsem",
    "findings", "eamt", "mt-summit", "amta", "calcs", "loresmt",
    # ML/vision/data venues that appear in venue strings.
    "colm", "tmlr", "jmlr", "corl", "rss", "icra", "iros", "3dv", "eurographics",
    "recsys", "cikm", "wsdm", "icde", "vldb", "sigmod", "naacl-hlt", "aacl",
)


def _venue_from_text(text: str | None) -> str | None:
    low = (text or "").lower()
    for name in _KNOWN_VENUES:
        if re.search(rf"\b{name}\b", low):
            return "neurips" if name == "nips" else name
    return None


def computed_facets(
    canonical_url: str,
    source: str | None = None,
    venue_text: str | None = None,
    year: int | None = None,
) -> list[str]:
    """Every URL-derived tag for an item: its type, its venue, its site.

    This is the one entry point the pipeline calls, so venue organisation can
    never silently lapse again: a paper gets `type/` and `venue/` (parsed from
    the URL, or from the written venue when the URL does not encode it), and
    anything that is not a paper gets `type/` and `site/`.
    """
    if source is None:
        from .canonical import source_of

        source, _ = source_of(canonical_url)

    tags = [type_for(canonical_url, source)]
    if is_paperish(tags[0]) or source in PAPER_SOURCES:
        tags += venue_tags(canonical_url, source, venue_text, year)
    else:
        site = site_tag(canonical_url)
        if site:
            tags.append(site)
    return [t for t in dict.fromkeys(tags) if t]


# --- axis 1c: site, computed for everything that is not a paper -------------


_PAPER_HOSTS = (
    "arxiv.org", "openreview.net", "aclanthology.org", "thecvf.com", "ecva.net",
    "neurips.cc", "nips.cc", "mlr.press", "aaai.org", "ieee.org", "doi.org",
)


def site_tag(canonical_url: str) -> str | None:
    """`site/<domain>` so non-paper reading can be browsed by where it came from.

    Papers are organised by venue, not site: a `site/arxiv` tag alongside
    `venue/arxiv` is noise, and the two used to collide because this returned a
    site for every host. A paper host returns nothing.
    """
    host = (urlsplit(canonical_url).hostname or "").lower()
    if not host:
        return None
    if _host_matches(host, _PAPER_HOSTS):
        return None
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)

    # Keep the registrable-looking part: news.ycombinator.com -> ycombinator,
    # transformer-circuits.pub -> transformer-circuits.
    labels = host.split(".")
    if len(labels) >= 2:
        name = labels[-2]
        if name in ("co", "com", "ac", "org", "gov") and len(labels) >= 3:
            name = labels[-3]
    else:
        name = labels[0]
    name = re.sub(r"[^a-z0-9\-]", "", name)
    return f"site/{name}" if name else None


# --- axis 2: contribution, for papers only ----------------------------------
#
# What the paper *adds*, which is orthogonal to what it is about. A benchmark
# for hallucination and a method for hallucination belong to the same topic and
# are used completely differently.

CONTRIBUTIONS = {
    "contribution/method": "proposes a new technique, architecture or training procedure",
    "contribution/benchmark": "introduces an evaluation suite, metric or leaderboard",
    "contribution/dataset": "releases a data resource as its main artifact",
    "contribution/analysis": "empirical study, probing or interpretability finding with no new method",
    "contribution/survey": "review or systematic overview of a field",
    "contribution/theory": "formal or theoretical result",
    "contribution/application": "applies existing methods to a domain or product problem",
    "contribution/position": "opinion, agenda or position piece",
}


# --- axis 3: topic, seeded from the researcher's own vocabulary -------------
#
# These come from the folder tree the library was organised by hand into. They
# are the ground truth for what this researcher actually cares about, so the
# derived taxonomy starts here and extends rather than inventing its own words.

# Terms that describe the entire library and therefore separate nothing. A tag
# may only contain one of these as an intermediate segment with real depth
# beneath it -- never as a leaf, and never as the first segment after topic/.
TOO_BROAD = frozenset(
    {
        "computer-science", "computer-vision", "machine-learning", "deep-learning",
        "artificial-intelligence", "ai", "ml", "cs", "nlp",
        "natural-language-processing", "research", "papers", "general",
        "miscellaneous", "misc", "other", "unknown", "reference", "metadata",
        "neural-networks", "models", "algorithms", "science", "technology",
    }
)

# Populations, not research problems. "Model merging for VLMs" is merging work;
# filing it under `vlm` buries it among everything else that happens to involve
# a VLM. These may only appear as the last segment, qualifying a problem.
MODALITIES = frozenset(
    {
        "vlm", "vlms", "llm", "llms", "mllm", "mllms", "lvlm", "lvlms",
        "multimodal", "vision-language", "language-model", "language-models",
        "diffusion-model", "diffusion-models", "transformer", "transformers",
        "nlp", "computer-vision", "vision", "language", "text", "image", "video",
    }
)

MIN_TOPIC_DEPTH = 3  # topic/<modality>/<area>/<detail>


def reject_reason(tag: str) -> str | None:
    """Why a proposed tag is unusable, or None if it is fine."""
    segments = [s for s in tag.split("/") if s]
    if not segments:
        return "empty"
    facet = segments[0]

    if facet == "type":
        return "type is computed from the URL, not proposed"
    if facet not in ("topic", "method", "task", "contribution"):
        return f"unknown facet {facet!r}"
    if len(segments) < 2:
        return "a bare facet is not a tag"

    # A broad word is a fine signpost and a useless destination. `computer-vision`
    # as a leaf files a paper under "vision" and tells you nothing; the same word
    # as an intermediate node with real depth beneath it is how you browse from
    # the general to the specific, which is what a working library needs.
    if segments[-1] in TOO_BROAD:
        return f"{segments[-1]!r} is too broad to be a leaf -- go deeper"
    # Depth before breadth, enforced rather than merely requested.
    #
    # topic/ was given hierarchy rules and grew four levels deep; task/ and
    # method/ were not, and task/ grew to 121 flat siblings -- an alphabetical
    # list, not a taxonomy. A facet you can only browse by scrolling is one
    # nobody browses. Requiring a family segment forces the model to answer
    # "what kind of task is this?" before it invents another sibling, which is
    # also what makes near-duplicates visible: `question-answering/visual` and
    # `question-answering/video` sit together instead of drifting apart.
    if facet in ("topic", "method", "task") and len(segments) < 3:
        return (
            f"{tag!r} is too shallow -- {facet}/ needs at least a family and a "
            f"specific, e.g. {facet}/{segments[1]}/<specific> or "
            f"<family>/{segments[1]}. Depth before breadth: nest it under an "
            f"existing family rather than adding another top-level sibling"
        )

    if facet == "topic":
        # The research problem is the parent; the modality qualifies it.
        if segments[1] in MODALITIES:
            return (
                f"{segments[1]!r} is a modality, not a research problem -- it "
                f"belongs at the end of the path, e.g. topic/model-merging/"
                f"{segments[1]} rather than topic/{segments[1]}/model-merging"
            )
        if segments[1] in TOO_BROAD:
            return f"{segments[1]!r} is too broad to be a top-level topic"
    return None


def seed_topics() -> tuple[str, ...]:
    """Starting vocabulary: whatever the researcher already uses.

    Read from ~/.cairn/seeds.txt when present, so the starting point is not
    frozen into the source. The built-in list below is only a default, taken
    from one hand-made folder tree, and any library will outgrow it.
    """
    from . import config

    path = config.db_path().parent / "seeds.txt"
    if path.is_file():
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if lines:
            return tuple(lines)
    return SEED_TOPICS


SEED_TOPICS = (
    "topic/vlm/interpretability/neuron-analysis",
    "topic/vlm/interpretability/circuit-analysis",
    "topic/vlm/interpretability/representation-shift",
    "topic/vlm/interpretability/cross-attention",
    "topic/vlm/interpretability/vision-token-alignment",
    "topic/vlm/architecture/moe-vision-encoder",
    "topic/vlm/architecture/pretraining",
    "topic/vlm/capability/hallucination",
    "topic/vlm/capability/spatial-reasoning",
    "topic/vlm/capability/multi-image",
    "topic/vlm/capability/emotion",
    "topic/vlm/capability/video",
    "topic/vlm/capability/world-model",
    "topic/vlm/task/image-captioning",
    "topic/vlm/task/rag-captioning",
    "topic/vlm/task/visual-discovery",
    "topic/vlm/eval/culture-benchmark",
    "topic/vlm/personalization/methods",
    "topic/vlm/transformation",
    "topic/generation/personalized-generation",
    "topic/generation/diffusion",
    "topic/generation/reimagine",
    "topic/efficiency/compression",
    "topic/efficiency/test-time-inference",
    "topic/efficiency/model-merging",
    "topic/learning/concept-learning",
    "topic/learning/meta-learning",
    "topic/learning/memory",
    "topic/uncertainty/llm",
    "topic/uncertainty/vlm",
    "topic/prompting/prompt-optimization",
    "topic/prompting/pareto-front",
    "topic/embodied/vision-language-navigation",
    "topic/resources/datasets",
    "topic/resources/job-openings",
)
