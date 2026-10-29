"""Offline tests for the Lambda Financial News MCP server (network and secret mocked).

Run: python3 tests/test_server.py
"""
import importlib.util
import io
import json
import logging
import pathlib
import time
import urllib.error

root = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("index", root / "src" / "server.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
app._secret_cache.update(value={"bearer_token": "tok123"}, loaded_at=time.time() + 1e9)

# --- fixture feeds -------------------------------------------------------
RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>X</title>
<item><title><![CDATA[Fed holds <b>rates</b> steady]]></title><link>https://ex.com/a</link>
  <description><![CDATA[<p>The Fed&nbsp;held rates. <a href="x">More</a></p><script>bad()</script>]]></description>
  <pubDate>Tue, 27 Oct 2026 13:56:42 -0400</pubDate></item>
<item><title>AT&amp;T earnings beat</title><link>https://ex.com/b</link><description>Strong quarter for AT&amp;T &#8212; shares up</description>
  <dc:date>2026-10-27</dc:date></item>
<item><title>Apple guidance</title><link>https://ex.com/c</link><description></description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><title>Oil slides on Iran news</title><link href="https://ex.com/d"/>
  <summary>Brent falls 3%</summary><updated>2026-10-27T22:04:57Z</updated></entry>
</feed>"""

# HTML entity outside CDATA + bare ampersand: invalid XML that real feeds do ship
BROKEN = b"""<?xml version="1.0"?><rss><channel>
<item><title>Q3 results &mdash; Smith & Wesson</title><link>https://ex.com/e</link>
<description>Margins &amp; more&nbsp;here</description></item></channel></rss>"""

# Google News search feed (used for Reuters): " - Reuters" title suffix, description only repeats the title
GNEWS = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>Oil falls as Israeli vote count drags on - Reuters</title><link>https://news.google.com/rss/articles/CBMi1</link>
<pubDate>Tue, 27 Oct 2026 22:41:00 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMi1"&gt;Oil falls as Israeli vote count drags on&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;Reuters&lt;/font&gt;</description>
<source url="https://www.reuters.com">Reuters</source></item></channel></rss>"""

FEEDS = {"cnbc_finance": RSS, "bloomberg_markets": ATOM, "investing_com": BROKEN, "reuters_finance": GNEWS}
calls = []


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def fake_urlopen(req, timeout):
    calls.append(req.full_url)
    key = next(k for k, u in app.RSS_FEEDS.items() if u == req.full_url)
    if key in FEEDS:
        return Resp(FEEDS[key])
    if key == "reuters_markets":
        raise urllib.error.URLError("[Errno -2] Name or service not known")
    if key == "yahoo_finance":
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))
    if key == "seeking_alpha":
        return Resp(b"<html><body>Access denied</body></html>")
    return Resp(b"<rss><channel></channel></rss>")


app.urllib.request.urlopen = fake_urlopen

# --- parsing -------------------------------------------------------------
arts = app._parse_feed(RSS, "cnbc_finance", 10)
assert arts[0]["title"] == "Fed holds rates steady", arts[0]
assert arts[0]["summary"] == "The Fed\xa0held rates. More", repr(arts[0]["summary"])
assert arts[0]["published"] == "Tue, 27 Oct 2026 13:56:42 -0400" and arts[0]["source"] == "CNBC"
assert arts[1]["title"] == "AT&T earnings beat" and arts[1]["summary"] == "Strong quarter for AT&T — shares up"
assert arts[1]["published"] == "2026-10-27" and arts[2]["summary"] == "" and arts[2]["published"] is None
atom = app._parse_feed(ATOM, "bloomberg_markets", 10)
assert atom == [{"title": "Oil slides on Iran news", "url": "https://ex.com/d", "source": "Bloomberg",
                 "summary": "Brent falls 3%", "published": "2026-10-27T22:04:57Z", "category": "Markets"}], atom
broken = app._parse_feed(BROKEN, "investing_com", 10)
assert broken and broken[0]["title"] == "Q3 results — Smith & Wesson", broken
assert app._parse_feed(b"not xml at all <<<", "x", 5) is None
g = app._parse_feed(GNEWS, "reuters_finance", 5)
assert g == [{"title": "Oil falls as Israeli vote count drags on", "url": "https://news.google.com/rss/articles/CBMi1",
              "source": "Reuters", "summary": "", "published": "Tue, 27 Oct 2026 22:41:00 GMT",
              "category": "Finance"}], g
# the suffix is only stripped for Google News sources
assert app._parse_feed(GNEWS, "cnbc_finance", 5)[0]["title"].endswith(" - Reuters")
assert app.RSS_FEEDS["reuters_markets"].startswith("https://news.google.com/rss/search?q=site%3Areuters.com+markets")
assert "feeds.reuters.com" not in "".join(app.RSS_FEEDS.values())
assert app.RSS_FEEDS["yahoo_finance"].startswith("https://news.google.com/rss/search?q=site%3Afinance.yahoo.com")
y = app._parse_feed(GNEWS.replace(b"- Reuters", b"- Yahoo Finance"), "yahoo_finance", 5)[0]
assert y["title"] == "Oil falls as Israeli vote count drags on" and y["source"] == "Yahoo Finance" and y["summary"] == ""
assert app._parse_feed(RSS, "cnbc_finance", 1)[0]["url"] == "https://ex.com/a" and len(app._parse_feed(RSS, "cnbc_finance", 1)) == 1

# --- parity with the original lxml/BeautifulSoup parser (skipped if those libs are absent) ----
try:
    import os
    import sys
    # Point ORIGINAL_SRC at the original repo's src/ (e.g. ~/Documents/GitHub/financial-news-mcp/src)
    sys.path.insert(0, os.environ.get("ORIGINAL_SRC", str(root.parent / "financial-news-mcp" / "src")))
    from financial_news_mcp import scrapers as orig  # noqa: E402
except Exception:  # noqa: BLE001
    orig = None
if orig is not None:
    for key, feed in (("cnbc_finance", RSS), ("bloomberg_markets", ATOM)):
        theirs = [a.__dict__ for a in orig._parse_rss_xml(feed, key, 10)]
        mine = app._parse_feed(feed, key, 10)
        assert mine == theirs, (key, mine, theirs)
    print("parity with original parser: OK")
else:
    print("parity with original parser: skipped (original repo or its deps not available)")

# --- transport / auth ----------------------------------------------------
def ev(body, auth="Bearer tok123", method="POST", path="/"):
    h = {"Content-Type": "application/json"}
    if auth:
        h["Authorization"] = auth
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "headers": h,
            "body": json.dumps(body) if body is not None else None}


def call(name, args=None):
    r = app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": args or {}}}), None)
    res = json.loads(r["body"])["result"]
    return res["isError"], res["content"][0]["text"]


assert app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, auth=None), None)["statusCode"] == 401
assert app.handler(ev(None, method="GET", path="/.well-known/oauth-protected-resource"), None)["statusCode"] == 404
assert app.handler(ev(None, method="GET"), None)["statusCode"] == 405
init = json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}}), None)["body"])["result"]
assert init["protocolVersion"] == "2025-06-18"
assert app.handler(ev({"jsonrpc": "2.0", "method": "notifications/initialized"}), None)["statusCode"] == 202
tools = json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), None)["body"])["result"]["tools"]
assert [t["name"] for t in tools] == ["get_latest_news", "search_financial_news", "list_news_sources", "get_news_by_source"]

# --- tools ----------------------------------------------------------------
err, text = call("get_latest_news", {"category": "markets", "limit": 2})
assert not err and text.startswith("## Latest Financial News — Markets")
# source order follows the category list: investing_com before bloomberg_markets
assert "1. **Q3 results — Smith & Wesson**" in text and "2. **Oil slides on Iran news**" in text, text
assert "_Sources with no articles: reuters_markets (unreachable" in text
assert "marketwatch_markets (no items)" in text and "cnbc_markets (no items)" in text

err, text = call("search_financial_news", {"query": "at&t", "category": "all"})
assert "Found 1 article(s)" in text and "AT&T earnings beat" in text
assert "yahoo_finance (HTTP 429)" in text and "seeking_alpha (no items)" in text
err, text = call("search_financial_news", {"query": "zzzz"})
assert "No articles found matching 'zzzz'." in text
assert call("search_financial_news", {})[1] == "Please provide a search query."

err, text = call("list_news_sources")
assert "- **cnbc_business** — CNBC (Finance)" in text and "### Category Groups" in text

err, text = call("get_news_by_source", {"source": "cnbc_finance", "limit": 1})
assert text.startswith("## Latest News from CNBC") and "Fed holds rates steady" in text and "AT&T" not in text
err, text = call("get_news_by_source", {"source": "nope"})
assert text.startswith("Unknown source 'nope'. Available: reuters_markets")
err, text = call("get_news_by_source", {"source": "yahoo_finance"})
assert "No articles found." in text and "yahoo_finance (HTTP 429)" in text

# feeds are cached: a repeat call makes no new requests
n = len(calls)
call("get_news_by_source", {"source": "cnbc_finance"})
assert len(calls) == n, "cache miss"

# --- auth shapes accepted, and the access log never contains the token --------
captured = []
h = logging.Handler()
h.emit = lambda record: captured.append(record.getMessage())
app.log.addHandler(h)
for hdr, expect in (("Bearertok123", 200), ("Bearer tok123", 200), ("Bearertok12", 401), ("tok123", 401)):
    assert app.handler(ev({"jsonrpc": "2.0", "id": 1, "method": "ping"}, auth=hdr), None)["statusCode"] == expect, hdr
assert captured and all("tok12" not in line for line in captured), captured
app.log.removeHandler(h)

sha = json.loads(app.handler(ev({"jsonrpc": "2.0", "id": 9, "method": "server/source_sha256"}), None)["body"])["result"]["sha256"]
print("source sha256", sha)
print("ALL OK")
