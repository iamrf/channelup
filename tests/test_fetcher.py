"""Fetcher: HTML cleaning, image extraction, source parsing."""
import feedparser

from channelup.fetcher import clean, fetch_sources, image_of, parse_source

FEED = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><item>
  <title>Rate cut &amp; rally</title>
  <link>https://ex.com/a</link>
  <description>&lt;p&gt;Markets &lt;b&gt;rose&lt;/b&gt; today.&lt;/p&gt;</description>
  <media:content url="https://ex.com/i.jpg"/>
</item><item>
  <title>No media</title>
  <link>https://ex.com/b</link>
  <description>Plain &lt;img src="https://ex.com/inline.png"/&gt; body</description>
</item></channel></rss>"""


def test_clean_strips_html_and_unescapes():
    assert clean("<p>Markets <b>rose</b> today.</p>") == "Markets rose today."
    assert clean("Rate cut &amp; rally") == "Rate cut & rally"


def test_image_of_media_content():
    entry = feedparser.parse(FEED).entries[0]
    assert image_of(entry) == "https://ex.com/i.jpg"


def test_image_of_inline_img():
    entry = feedparser.parse(FEED).entries[1]
    assert image_of(entry) == "https://ex.com/inline.png"


def test_parse_source_normalizes_items():
    items = parse_source(FEED)
    assert [i["link"] for i in items] == ["https://ex.com/a", "https://ex.com/b"]
    assert items[0]["title"] == "Rate cut & rally"
    assert items[1]["image"] == "https://ex.com/inline.png"


def test_fetch_sources_skips_failures_without_aborting(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("channelup.fetcher.parse_source", boom)
    assert fetch_sources(["https://bad/rss"]) == []