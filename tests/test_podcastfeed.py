"""Reading a pasted feed address: what is shown before anybody subscribes."""
import harness

harness.setup()

from datetime import datetime, timezone  # noqa: E402

from app import podcastfeed  # noqa: E402

check = harness.Check("podcastfeed")

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title> Wooden Overcoats </title>
    <description><![CDATA[<p>Rudyard Funn and his equally miserable sister Antigone run their family's failing funeral parlour.</p><p>A sitcom.<br/>Piffling Vale, Channel Islands.</p>]]></description>
    <itunes:author>Wooden Overcoats Ltd</itunes:author>
    <itunes:image href="https://example.invalid/art.jpg"/>
    <itunes:category text="Comedy"><itunes:category text="Comedy Fiction"/></itunes:category>
    <itunes:category text="Fiction"/>
    <item><title>Episode 2</title><pubDate>Fri, 30 Mar 2016 12:00:00 +0000</pubDate></item>
    <item><title>Episode 1</title><pubDate>Thu, 24 Mar 2016 06:00:00 PST</pubDate></item>
    <item><title>Trailer</title></item>
  </channel>
</rss>"""

preview = podcastfeed.parse("https://example.invalid/feed.xml", FEED)
check.equal(preview.title, "Wooden Overcoats", "the title is read and trimmed")
check.equal(preview.author, "Wooden Overcoats Ltd", "the iTunes author is who makes it")
check.equal(preview.description,
            "Rudyard Funn and his equally miserable sister Antigone run their family's failing funeral parlour.\n\nA sitcom.\nPiffling Vale, Channel Islands.",
            "markup in the description becomes text with its paragraphs")
check.equal(preview.image_url, "https://example.invalid/art.jpg", "the iTunes image is the picture")
check.equal(preview.categories, ["Comedy", "Comedy Fiction", "Fiction"],
            "categories are flattened, children after their parents")
check.equal(preview.episode_count, 3, "every item is an episode, dated or not")
check.equal(preview.latest, datetime(2016, 3, 30, 12, 0, tzinfo=timezone.utc),
            "the newest dated episode is the latest, whatever order the feed lists them in")

# A day name that does not match the date, and a zone name Python can read.
check.equal(podcastfeed.parse_date("Fri, 30 Mar 2016 12:00:00 +0000"),
            datetime(2016, 3, 30, 12, 0, tzinfo=timezone.utc), "a wrong day name does not lose the date")
check.equal(podcastfeed.parse_date("Thu, 24 Mar 2016 06:00:00 PST"),
            datetime(2016, 3, 24, 14, 0, tzinfo=timezone.utc),
            "an American zone name is honoured (Python knows PST is eight hours behind)")
check.equal(podcastfeed.parse_date("soon"), None, "an unreadable date is None")

# The sparse feed: the RSS image and the managing editor stand in.
SPARSE = b"""<rss version="2.0"><channel><title>Sparse</title>
<managingEditor>someone@example.invalid (Some One)</managingEditor>
<image><url>https://example.invalid/rss.jpg</url></image></channel></rss>"""
sparse = podcastfeed.parse("https://example.invalid/sparse", SPARSE)
check.equal(sparse.author, "", "managingEditor is not treated as the author (it is an address)")
check.equal(sparse.image_url, "https://example.invalid/rss.jpg", "the RSS image stands in")
check.equal(sparse.episode_count, 0, "no items, no episodes")
check.equal(sparse.latest, None, "and no latest date")

# Not a feed at all.
check.raises(podcastfeed.Unreadable,
             lambda: podcastfeed.parse("https://example.invalid/", b"<html><body>hi</body></html>"),
             "a web page is refused with a sentence")
check.raises(podcastfeed.Unreadable,
             lambda: podcastfeed.parse("https://example.invalid/", b"not xml at all <<<"),
             "rubbish is refused with a sentence")
check.raises(podcastfeed.Unreadable,
             lambda: podcastfeed.parse("https://example.invalid/", b"<rss><channel><title></title></channel></rss>"),
             "a feed with no title cannot be added")

# What counts as an address in the search box.
check.that(podcastfeed.looks_like_url("https://feeds.example.invalid/show.rss"), "an https address is one")
check.that(not podcastfeed.looks_like_url("the magnus archives"), "words are not")
check.that(not podcastfeed.looks_like_url("https://a b"), "an address with a space is not")

# A feed cut mid-item is closed at its last whole item.
truncated = FEED[:FEED.index(b"<item><title>Trailer")] + b"<item><title>Half an it"
closed = podcastfeed._close_truncated(truncated)
cut = podcastfeed.parse("https://example.invalid/big", closed)
check.equal(cut.episode_count, 2, "a truncated feed still parses, counting the whole items")

# The fetch refuses anything that is not an address without touching the network.
check.raises(podcastfeed.Unreadable, lambda: podcastfeed.fetch("not an address"),
             "words are refused before any request")

# An address inside the house -- this machine, the Docker network, the LAN,
# a link-local range -- is refused before any request is made. Names are
# judged by what they resolve to, so a public-looking name that points inside
# is refused too, and a name nothing resolves cannot be a feed.
import httpx  # noqa: E402

_real_resolve = podcastfeed._resolve
_names = {
    "feeds.example": ["8.8.8.8"],
    "internal.example": ["10.0.0.5"],
    "both.example": ["8.8.8.8", "192.168.1.181"],
    "sixmapped.example": ["::ffff:192.168.1.181"],
    "nowhere.example": [],
}
podcastfeed._resolve = lambda host: _names[host] if host in _names else _real_resolve(host)
asked = []
podcastfeed._transport = httpx.MockTransport(
    lambda request: asked.append(str(request.url)) or httpx.Response(200, content=FEED))
for inside in ("http://127.0.0.1/feed", "http://localhost:8096/feed", "http://[::1]/feed",
               "http://192.168.1.181:8096/feed", "http://10.1.2.3/x", "http://172.18.0.5/x",
               "http://169.254.169.254/latest/meta-data", "http://100.64.0.1/x",
               "http://jellyfin:8096/feed", "http://dockge.local/x", "http://internal.example/feed",
               "http://both.example/feed", "http://sixmapped.example/feed", "http://nowhere.example/feed",
               "http://0.0.0.0/feed", "http://224.0.0.1/feed"):
    check.raises(podcastfeed.Unreadable, lambda inside=inside: podcastfeed.fetch(inside),
                 f"{inside} is refused")
check.equal(asked, [], "none of them was requested")
check.equal(podcastfeed.public_address_problem("https://feeds.example/show.rss"), None,
            "a public address is allowed")
check.equal(podcastfeed.public_address_problem("https://1.1.1.1/show.rss"), None,
            "a public literal address is allowed")
check.that(podcastfeed.public_address_problem("https://203.0.113.10/show.rss") is not None,
           "a documentation-range address is not public either")
check.equal(podcastfeed.fetch("https://feeds.example/show.rss").title, "Wooden Overcoats",
            "and read")

# A redirect is checked the same way at every hop: a public hop is followed,
# an inside one is refused without being requested, and a loop is cut off.
asked.clear()


def _hops(request):
    asked.append(str(request.url))
    path = request.url.path
    if path == "/redirect-inside":
        return httpx.Response(302, headers={"Location": "http://192.168.1.181:8096/feed"})
    if path == "/redirect-public":
        return httpx.Response(302, headers={"Location": "https://feeds.example/show.rss"})
    if path == "/loop":
        return httpx.Response(302, headers={"Location": "https://feeds.example/loop"})
    return httpx.Response(200, content=FEED, headers={"Content-Type": "application/rss+xml"})


podcastfeed._transport = httpx.MockTransport(_hops)
check.raises(podcastfeed.Unreadable, lambda: podcastfeed.fetch("https://feeds.example/redirect-inside"),
             "a redirect to an inside address is refused")
check.that(not any("192.168" in url for url in asked), "and the inside address was never requested")
check.equal(podcastfeed.fetch("https://feeds.example/redirect-public").title, "Wooden Overcoats",
            "a redirect to a public address is followed")
check.raises(podcastfeed.Unreadable, lambda: podcastfeed.fetch("https://feeds.example/loop"),
             "a redirect loop is cut off")
check.that(asked.count("https://feeds.example/loop") <= podcastfeed.MAX_REDIRECTS + 1,
           "after a bounded number of hops")
podcastfeed._transport = None
podcastfeed._resolve = _real_resolve

harness.cleanup()
raise SystemExit(check.report())
