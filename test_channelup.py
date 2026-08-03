"""Self-check: parsing, image extraction, dedup. Run: python test_channelup.py"""
import os
import tempfile

os.environ.update(
    TELEGRAM_BOT_TOKEN="1:x", LLM_API_KEY="k",
    TARGET_CHANNEL_IDS="-1001", RSS_SOURCES="http://example.invalid/feed",
    DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"),
)

import feedparser

import channelup as c

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

entries = feedparser.parse(FEED).entries

assert c.clean(entries[0].summary) == "Markets rose today.", c.clean(entries[0].summary)
assert c.clean(entries[0].title) == "Rate cut & rally"
assert c.image_of(entries[0]) == "https://ex.com/i.jpg"
assert c.image_of(entries[1]) == "https://ex.com/inline.png"  # scraped from summary HTML

assert c.seen("https://ex.com/a") is False
c.mark("https://ex.com/a")
assert c.seen("https://ex.com/a") is True
c.mark("https://ex.com/a")  # idempotent, must not raise
assert c.seen("https://ex.com/b") is False

assert "{language}" not in c.SYSTEM_PROMPT.format(language="en")
print("ok")