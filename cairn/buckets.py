"""Where a URL belongs: the library, the documents bucket, or nowhere.

Three outcomes, decided from the URL alone with no model and no network:

  library  research reading -- papers, blogs, repos, Claude artifacts
  docs     working documents -- Google Docs/Slides, Notion, Word, Overleaf
  never    entertainment, news, sport, and assistant chat transcripts

`never` is a hard rule, not a filter: those pages are not stored at all, so
nothing about them reaches the model or the database. Both host lists can be
extended from the environment without editing this file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .canonical import canonicalize

# --- never stored -----------------------------------------------------------

VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "netflix.com",
    "primevideo.com", "hotstar.com", "disneyplus.com", "dailymotion.com",
)

# Real-time meeting rooms. A call URL is ephemeral and never research reading, so
# it is dropped like a video host rather than saved as a "web" page.
MEETING_HOSTS = (
    "meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com",
    "webex.com", "whereby.com", "meet.jit.si", "gotomeeting.com",
)

# Live editing surfaces: the URL points at a document you are writing, not
# something you read. Nothing to file, and the page changes constantly.
EDITING_HOSTS = ("overleaf.com", "www.overleaf.com", "sharelatex.com")

ASSISTANT_HOSTS = (
    "chatgpt.com", "chat.openai.com", "gemini.google.com", "bard.google.com",
    "copilot.microsoft.com", "poe.com",
)

NEWS_HOSTS = (
    "nytimes.com", "washingtonpost.com", "wsj.com", "ft.com", "bloomberg.com",
    "bbc.com", "bbc.co.uk", "cnn.com", "reuters.com", "apnews.com",
    "theguardian.com", "aljazeera.com", "foxnews.com", "nbcnews.com",
    "news.google.com", "news.ycombinator.com", "ndtv.com", "indiatimes.com",
    "timesofindia.indiatimes.com", "hindustantimes.com", "thehindu.com",
    "indianexpress.com", "techcrunch.com", "theverge.com", "engadget.com",
    "arstechnica.com", "cnbc.com", "forbes.com", "businessinsider.com",
)

SPORTS_HOSTS = (
    "espn.com", "espncricinfo.com", "cricbuzz.com", "nba.com", "nfl.com",
    "fifa.com", "skysports.com", "goal.com", "sports.yahoo.com",
    "olympics.com", "formula1.com",
)

SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "tiktok.com", "snapchat.com",
    "pinterest.com", "threads.net",
)

# --- stored, but kept apart from the reading library ------------------------

DOC_HOSTS = (
    "docs.google.com", "drive.google.com", "sheets.google.com",
    "slides.google.com", "forms.google.com",
    "notion.so", "notion.site", "notion.com",
    "office.com", "office.live.com", "onedrive.live.com", "sharepoint.com",
    "dropbox.com", "paper.dropbox.com",
    "quip.com", "coda.io", "airtable.com", "figma.com", "miro.com",
    "canva.com", "evernote.com", "onenote.com",
)

# Chat assistants are never stored, except for the artifacts they produce,
# which are real work product rather than a conversation transcript.
ARTIFACT_PATTERNS = (
    re.compile(r"^/(?:code/)?artifacts?/", re.I),
    re.compile(r"^/public/artifacts/", re.I),
)
ARTIFACT_HOSTS = ("claude.ai", "claude.com")

# Structural junk: local, utility and search-results pages. Anything that only
# resolves on this machine or this network has no stable identity worth storing
# -- the URL means nothing on any other device, including a restored backup.
JUNK_HOST_PATTERNS = (
    r"^localhost$",
    r"\.localhost$",
    r"^127\.\d+\.\d+\.\d+$",          # loopback
    r"^0\.0\.0\.0$",
    r"^\[?::1\]?$",                    # IPv6 loopback
    r"^10\.\d+\.\d+\.\d+$",           # RFC1918 private
    r"^192\.168\.\d+\.\d+$",
    r"^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$",
    r"^169\.254\.\d+\.\d+$",          # link-local
    r"\.local$",                       # mDNS / Bonjour
    r"\.lan$",
    r"\.internal$",
    r"\.home$",
    r"^accounts\.google\.",
    r"^status\.",
)

JUNK_PATH_PATTERNS = (
    (r"google\.", r"^/search"),
    (r"scholar\.google\.", r"^/(scholar|citations)$"),
    (r"bing\.com", r"^/search"),
    (r"duckduckgo\.com", r"^/$"),
)

# Private working surfaces: your own consoles and mail. Stored only with an
# explicit opt-in, because organising them means sending titles to a model.
PRIVATE_HOSTS = (
    "mail.google.com", "outlook.com", "outlook.office.com",
    "calendar.google.com", "console.runpod.io", "console.cloud.google.com",
    "wandb.ai", "app.slack.com", "web.whatsapp.com", "linkedin.com",
    "x.com", "twitter.com",
)

def builtin_never_groups() -> list[dict]:
    """The built-in 'never stored' rules, grouped for display in Settings.

    Read-only and always in effect, on top of the user's own blocklist. Kept
    here next to the lists they mirror so the two never drift apart.
    """
    return [
        {"label": "Video & streaming", "hosts": list(VIDEO_HOSTS)},
        {"label": "Meetings", "hosts": list(MEETING_HOSTS)},
        {"label": "Chat assistants (except artifacts)", "hosts": list(ASSISTANT_HOSTS)},
        {"label": "News", "hosts": list(NEWS_HOSTS)},
        {"label": "Sport", "hosts": list(SPORTS_HOSTS)},
        {"label": "Social", "hosts": list(SOCIAL_HOSTS)},
        {"label": "Live editors", "hosts": list(EDITING_HOSTS)},
        {
            "label": "Local & private network",
            "hosts": ["localhost", "127.0.0.1", "*.local", "*.internal", "private IPs (10.x, 192.168.x)"],
        },
        {
            "label": "Search-result pages",
            "hosts": ["google.com/search", "bing.com/search", "duckduckgo.com", "scholar.google.com"],
        },
    ]


LIBRARY = "library"
DOCS = "docs"
NEVER = "never"


@dataclass(frozen=True)
class Decision:
    bucket: str
    reason: str

    @property
    def stored(self) -> bool:
        return self.bucket != NEVER


def _env_hosts(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _matches(host: str, candidates) -> bool:
    return any(host == c or host.endswith("." + c) for c in candidates)


def is_artifact(host: str, path: str) -> bool:
    """A Claude artifact is work product; a Claude chat is not."""
    if not _matches(host, ARTIFACT_HOSTS):
        return False
    return any(pattern.match(path) for pattern in ARTIFACT_PATTERNS)


def classify(url: str, *, include_private: bool = False) -> Decision:
    canonical = canonicalize(url)
    if not canonical.startswith("http"):
        return Decision(NEVER, "not a web page")

    parts = urlsplit(canonical)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"

    # An artifact outranks the assistant-host rule that would otherwise drop it.
    if is_artifact(host, path):
        return Decision(LIBRARY, "claude artifact")

    from . import config

    if config.is_blocked(canonical):
        return Decision(NEVER, "blocklist")
    if any(re.search(pattern, host) for pattern in JUNK_HOST_PATTERNS):
        return Decision(NEVER, "local or utility host")
    for host_pattern, path_pattern in JUNK_PATH_PATTERNS:
        if re.search(host_pattern, host) and re.search(path_pattern, path):
            return Decision(NEVER, "search results")

    if _matches(host, _env_hosts("CAIRN_NEVER_HOSTS")):
        return Decision(NEVER, "never list (env)")
    if _matches(host, EDITING_HOSTS):
        return Decision(NEVER, "live editing surface")
    if _matches(host, VIDEO_HOSTS):
        return Decision(NEVER, "video")
    if _matches(host, MEETING_HOSTS):
        return Decision(NEVER, "meeting")
    if _matches(host, ASSISTANT_HOSTS) or _matches(host, ARTIFACT_HOSTS):
        return Decision(NEVER, "assistant chat")
    if _matches(host, NEWS_HOSTS):
        return Decision(NEVER, "news")
    if _matches(host, SPORTS_HOSTS):
        return Decision(NEVER, "sport")
    if _matches(host, SOCIAL_HOSTS):
        return Decision(NEVER, "social")

    if _matches(host, DOC_HOSTS) or _matches(host, _env_hosts("CAIRN_DOC_HOSTS")):
        return Decision(DOCS, "working document")

    if not include_private and _matches(host, PRIVATE_HOSTS):
        return Decision(NEVER, "private (use --include-private)")
    if not include_private and host.endswith("mbzuai.ac.ae"):
        return Decision(NEVER, "private (use --include-private)")

    return Decision(LIBRARY, "library")
