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

harness.cleanup()
raise SystemExit(check.report())
