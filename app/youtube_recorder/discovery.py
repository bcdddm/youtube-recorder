"""RSS discovery (v0.2 design §6.2 Discovery).

- Official channel feed: https://www.youtube.com/feeds/videos.xml?channel_id=UC...
- Conditional GET with ETag / Last-Modified.
- Entries older than the channel's `not_before` are ignored (no historical backfill).
- Every accepted entry is inserted as `discovered`; the per-run processing cap
  is applied later by the pipeline, never here (C08).
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass


def _ssl_context() -> ssl.SSLContext:
    """python.org macOS builds often lack system CA certs; prefer certifi."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()

from . import BRANDING, __version__

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
TIMEOUT = 20
USER_AGENT = f"{BRANDING} v{__version__} (personal use)"


@dataclass
class FeedEntry:
    video_id: str
    title: str
    published: str  # ISO8601


@dataclass
class FeedResult:
    status: str  # ok | not_modified | error
    entries: list[FeedEntry]
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


def parse_feed(xml_text: str) -> list[FeedEntry]:
    root = ET.fromstring(xml_text)
    out: list[FeedEntry] = []
    for e in root.findall("atom:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        out.append(FeedEntry(
            video_id=vid,
            title=e.findtext("atom:title", default="", namespaces=NS),
            published=e.findtext("atom:published", default="", namespaces=NS),
        ))
    return out


def fetch_feed(channel_id: str, etag: str | None = None,
               last_modified: str | None = None) -> FeedResult:
    req = urllib.request.Request(FEED_URL.format(cid=channel_id))
    req.add_header("User-Agent", USER_AGENT)
    if etag:
        req.add_header("If-None-Match", etag)
    if last_modified:
        req.add_header("If-Modified-Since", last_modified)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=_ssl_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return FeedResult(
                status="ok",
                entries=parse_feed(body),
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return FeedResult(status="not_modified", entries=[])
        return FeedResult(status="error", entries=[], error=f"http_{e.code}")
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
        return FeedResult(status="error", entries=[], error=type(e).__name__)


def accept_entry(entry: FeedEntry, not_before: str | None) -> bool:
    """not_before is ISO8601 (UTC). RSS `published` is ISO8601 with offset —
    string comparison on the common prefix is safe for UTC 'Z' vs '+00:00'
    after normalisation."""
    if not not_before or not entry.published:
        return True
    pub = entry.published.replace("+00:00", "Z")
    return pub >= not_before
