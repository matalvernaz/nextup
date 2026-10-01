"""Reading a podcast's RSS feed, for the moment somebody pastes its address.

Enough of the feed to show a person what they are about to subscribe to and
to name the ledger row: title, who makes it, what it is about, its picture,
how many episodes it lists and when the newest came out. The full reading --
categories, per-episode detail -- is the Jellyfin fork's job once the files
are there; this is the preview.
"""
import html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx

from . import logs

log = logs.get("podcastfeed")

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

#: A feed is one request while somebody waits on a search box.
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

#: Feeds run to megabytes for a long-running show; a preview needs the channel
#: block and a count of items, not every show note ever written. Anything past
#: this is cut, and the count is then "at least".
MAX_BYTES = 4_000_000

#: A pasted address is followed through this many redirects at most, each
#: hop checked the way the first was.
MAX_REDIRECTS = 5

#: A transport for tests to answer requests with; None for the network.
_transport: httpx.BaseTransport | None = None

_TAG = re.compile(r"<[^>]+>")
#: A paragraph's end becomes a blank line; a line break, a line. The same
#: reading the Jellyfin fork gives a description, so both show one text.
_PARAGRAPH_END = re.compile(r"<\s*/(?:p|div|li|h[1-6])\s*>", re.IGNORECASE)
_BREAK = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_SPACES = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class FeedPreview(NamedTuple):
    """What a feed says about itself, cleaned for a page."""

    url: str
    title: str
    author: str
    description: str
    image_url: str
    episode_count: int
    latest: datetime | None
    categories: list[str]


class Unreadable(Exception):
    """The address did not yield a podcast feed. Carries a sentence to show."""


def looks_like_url(text: str) -> bool:
    """Whether a search box holds an address rather than words."""
    text = (text or "").strip()
    return text.startswith(("http://", "https://")) and " " not in text


def _resolve(host: str) -> list[str]:
    """Every address a host name resolves to; a literal address is itself."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        found = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        # gaierror is one of these; so is a resolver with no network at all.
        return []
    return sorted({entry[4][0] for entry in found})


def public_address_problem(url: str) -> str | None:
    """Why a feed address may not be fetched, in a sentence, or None when it may.

    Anybody with an account can paste an address, and the server would fetch
    it on their behalf, so an address that points back inside the house --
    this machine, the Docker network, the LAN, a link-local or reserved range
    -- is refused before any request is made, and again at every redirect.
    An address nothing resolves is refused too, since a feed nobody can reach
    is not a feed.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https"):
        return "That is not a web address."
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        return "That address has no host name."
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") \
            or host.endswith(".internal") or "." not in host and not _is_ip(host):
        return "That address points inside this network, so it cannot be used as a feed."
    addresses = _resolve(host)
    if not addresses:
        return "That address could not be resolved."
    for text in addresses:
        try:
            address = ipaddress.ip_address(text.split("%", 1)[0])
        except ValueError:
            return "That address could not be resolved."
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if not address.is_global or address.is_multicast:
            return "That address points inside this network, so it cannot be used as a feed."
    return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def strip_html(text: str | None) -> str:
    """A description reduced to text: tags dropped, entities decoded, paragraphs kept."""
    if not text:
        return ""
    text = _PARAGRAPH_END.sub("\n\n", text)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()
    return _BLANK_LINES.sub("\n\n", text)


def parse_date(text: str | None) -> datetime | None:
    """An RFC 822 date as feeds write them, in UTC, or None."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def parse(url: str, body: bytes) -> FeedPreview:
    """A preview out of a feed's bytes. Raises Unreadable for anything else."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise Unreadable("That address did not answer with a podcast feed.") from exc
    channel = root.find("channel")
    if channel is None:
        raise Unreadable("That address answered with something that is not a podcast feed.")

    def text(element) -> str:
        return (element.text or "").strip() if element is not None else ""

    title = text(channel.find("title"))
    if not title:
        raise Unreadable("That feed has no title, so it cannot be added.")
    author = (text(channel.find(ITUNES + "author"))
              or text(channel.find(ITUNES + "owner/" + ITUNES + "name")))
    description = strip_html(
        text(channel.find("description"))
        or text(channel.find(ITUNES + "summary"))
        or text(channel.find(ITUNES + "subtitle")))
    image = channel.find(ITUNES + "image")
    image_url = (image.get("href", "").strip() if image is not None else "") \
        or text(channel.find("image/url"))
    items = channel.findall("item")
    dates = [parse_date(text(item.find("pubDate"))) for item in items]
    latest = max((d for d in dates if d is not None), default=None)
    categories: list[str] = []
    for category in channel.findall(ITUNES + "category"):
        for element in (category, *category.findall(ITUNES + "category")):
            name = html.unescape(element.get("text", "")).strip()
            if name and name.casefold() not in {c.casefold() for c in categories}:
                categories.append(name)
    return FeedPreview(url, title, author, description, image_url,
                       len(items), latest, categories)


def fetch(url: str) -> FeedPreview:
    """Fetch and read one feed. Raises Unreadable with a sentence to show."""
    url = (url or "").strip()
    if not looks_like_url(url):
        raise Unreadable("That is not a web address.")
    problem = public_address_problem(url)
    if problem:
        log.info("feed refused url=%s: %s", url, problem)
        raise Unreadable(problem)
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=_transport,
                          headers={"User-Agent": "nextup (podcast preview)"}) as client:
            hops = 0
            while True:
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        hops += 1
                        if hops > MAX_REDIRECTS:
                            raise Unreadable("That address redirects too many times.")
                        target = str(response.next_request.url) if response.next_request else ""
                        problem = public_address_problem(target)
                        if problem:
                            log.info("feed refused url=%s redirect=%s: %s", url, target, problem)
                            raise Unreadable(problem)
                        url = target
                        continue
                    if response.status_code >= 400:
                        raise Unreadable(
                            f"That address answered {response.status_code}, "
                            "so the feed could not be read.")
                    body = b""
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    break
    except httpx.HTTPError as exc:
        log.info("feed unreadable url=%s (%s)", url, exc)
        raise Unreadable("That address could not be reached.") from exc
    if len(body) > MAX_BYTES:
        # Cut mid-document; close what is open so the channel block still
        # parses. Item counts past the cut are simply not counted.
        body = _close_truncated(body)
    return parse(str(url), body)


def _close_truncated(body: bytes) -> bytes:
    """A truncated feed, cut back to its last whole item and closed."""
    cut = body.rfind(b"</item>")
    if cut < 0:
        return body
    return body[:cut + len(b"</item>")] + b"</channel></rss>"
