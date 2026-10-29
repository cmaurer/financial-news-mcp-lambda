"""Financial News MCP server for AWS Lambda (function URL).

A stdlib-only port of github.com/cmaurer/financial-news-mcp (stdio, httpx + BeautifulSoup
+ lxml) to MCP's stateless streamable-HTTP transport, so claude.ai and its scheduled tasks
can reach it from the cloud. Tool names, descriptions, input schemas and the markdown
output format match the original. Differences:
  - feeds are fetched in parallel threads with urllib instead of asyncio + httpx;
  - XML is parsed with ElementTree, with a final repair pass (HTML entities, stray '&')
    for feeds the original would drop; HTML stripped with html.parser instead of BeautifulSoup;
  - sources that return nothing are NAMED at the end of the output instead of being
    silently dropped, so a caller can report them as data gaps;
  - feed responses are cached for a few minutes per warm Lambda instance;
  - cnbc_business gets its display name ("CNBC"), which the original was missing;
  - dead feeds replaced (Oct 2026), keeping their source keys: Reuters via Google News
    (reuters.com stories only), Yahoo Finance via Google News (finance.yahoo.com stories only),
    and MarketWatch markets via the breaking-news bulletins feed.

The only secret is the bearer token guarding this endpoint:
Secrets Manager JSON {"bearer_token": "..."}. Standard library + boto3 only.
"""

import concurrent.futures
import hmac
import html
import html.entities
import html.parser
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger()
log.setLevel(logging.INFO)

SERVER_NAME = "financial-news-mcp"
SERVER_VERSION = "0.1.0-lambda.3"
SUPPORTED_PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SECRET_TTL_SECONDS = 300
FEED_CACHE_SECONDS = 300
FEED_TIMEOUT_SECONDS = 15
INSTRUCTIONS = ("Latest financial news headlines from public RSS feeds (MarketWatch, CNBC, Financial Times, "
                "Seeking Alpha, Investing.com, Bloomberg, WSJ, Fox Business, and others). Use list_news_sources "
                "for source keys. Sources that returned nothing are listed at the end of each result.")

# --------------------------------------------------------------------------
# Sources (from the original scrapers.py)
# --------------------------------------------------------------------------
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _google_news(query):
    """Google News RSS search URL (personal, non-commercial feed-reader use per Google's feed terms)."""
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(query)
            + "&hl=en-US&gl=US&ceid=US:en")


# Reuters retired feeds.reuters.com in 2020, Yahoo retired finance.yahoo.com/rss/ (and rate-limits AWS
# addresses with HTTP 429 on its remaining feeds), and MarketWatch's mw_marketpulse stopped updating in
# July 2025; the original keys are kept with working sources.
RSS_FEEDS = {
    "reuters_markets": _google_news("site:reuters.com markets when:2d"),
    "reuters_finance": _google_news("site:reuters.com (finance OR banking OR economy) when:2d"),
    "yahoo_finance": _google_news("site:finance.yahoo.com markets when:2d"),
    "marketwatch_top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "marketwatch_markets": "https://feeds.content.dowjones.io/public/rss/mw_bulletins",
    "cnbc_finance": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "cnbc_business": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
    "cnbc_markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "ft_markets": "https://www.ft.com/rss/home/uk",
    "ft_international": "https://www.ft.com/rss/home/international",
    "seeking_alpha": "https://seekingalpha.com/feed.xml",
    "investing_com": "https://www.investing.com/rss/news.rss",
    "bloomberg_markets": "https://feeds.bloomberg.com/markets/news.rss",
    "wsj_business": "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "wsj_markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "wsj_economy": "https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
    "fox_business_economy": "https://moxie.foxbusiness.com/google-publisher/economy.xml",
    "fox_business_markets": "https://moxie.foxbusiness.com/google-publisher/markets.xml",
    "fox_business_latest": "https://moxie.foxbusiness.com/google-publisher/latest.xml",
}

SOURCE_CATEGORIES = {
    "reuters_markets": "Business", "reuters_finance": "Finance", "yahoo_finance": "Markets",
    "marketwatch_top": "Top Stories", "marketwatch_markets": "Markets", "cnbc_finance": "Finance",
    "cnbc_business": "Finance", "cnbc_markets": "Markets", "ft_markets": "Markets",
    "ft_international": "International", "seeking_alpha": "Investing", "investing_com": "Markets",
    "bloomberg_markets": "Markets", "wsj_business": "Business", "wsj_markets": "Markets",
    "wsj_economy": "Economy", "fox_business_economy": "Economy", "fox_business_markets": "Markets",
    "fox_business_latest": "Top Stories",
}

SOURCE_NAMES = {
    "reuters_markets": "Reuters", "reuters_finance": "Reuters", "yahoo_finance": "Yahoo Finance",
    "marketwatch_top": "MarketWatch", "marketwatch_markets": "MarketWatch", "cnbc_finance": "CNBC",
    "cnbc_business": "CNBC", "cnbc_markets": "CNBC", "ft_markets": "Financial Times",
    "ft_international": "Financial Times", "seeking_alpha": "Seeking Alpha", "investing_com": "Investing.com",
    "bloomberg_markets": "Bloomberg", "wsj_business": "Wall Street Journal", "wsj_markets": "Wall Street Journal",
    "wsj_economy": "Wall Street Journal", "fox_business_economy": "Fox Business",
    "fox_business_markets": "Fox Business", "fox_business_latest": "Fox Business",
}

ALL_SOURCES = list(RSS_FEEDS.keys())
CATEGORY_SOURCES = {
    "markets": ["reuters_markets", "marketwatch_markets", "cnbc_markets", "investing_com", "bloomberg_markets"],
    "finance": ["reuters_finance", "cnbc_finance", "yahoo_finance"],
    "investing": ["seeking_alpha", "yahoo_finance", "investing_com"],
    "top": ["reuters_markets", "marketwatch_top", "cnbc_finance", "cnbc_business"],
    "all": ALL_SOURCES,
}

# Google News titles end in " - <Publisher>" and their descriptions only repeat the title.
GOOGLE_NEWS_SOURCES = {"reuters_markets", "reuters_finance", "yahoo_finance"}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

# --------------------------------------------------------------------------
# Tool definitions (verbatim from the original server.py)
# --------------------------------------------------------------------------
_CATEGORY_ENUM = ["markets", "finance", "investing", "top", "all"]
TOOLS = [
    {
        "name": "get_latest_news",
        "description": ("Fetch the latest financial news headlines from major sources including "
                        "Reuters, MarketWatch, CNBC, Yahoo Finance, Financial Times, Seeking Alpha, "
                        "and Investing.com."),
        "inputSchema": {"type": "object", "properties": {
            "category": {"type": "string",
                         "description": "News category: 'markets', 'finance', 'investing', 'business', 'top', 'all'. Defaults to 'top'.",
                         "enum": _CATEGORY_ENUM, "default": "top"},
            "limit": {"type": "integer", "description": "Max articles per source (1-20). Defaults to 5.",
                      "minimum": 1, "maximum": 20, "default": 5},
        }},
    },
    {
        "name": "search_financial_news",
        "description": ("Search for financial news articles matching a keyword or phrase across "
                        "multiple news sources."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search keyword or phrase (e.g. 'Apple', 'Fed interest rates')."},
            "category": {"type": "string",
                         "description": "Limit to source category. Options: 'markets', 'finance', 'investing', 'top', 'all'.",
                         "enum": _CATEGORY_ENUM, "default": "all"},
            "limit": {"type": "integer", "description": "Max results (1-50). Defaults to 10.",
                      "minimum": 1, "maximum": 50, "default": 10},
        }, "required": ["query"]},
    },
    {
        "name": "list_news_sources",
        "description": "List all available financial news sources and their categories.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_news_by_source",
        "description": "Fetch the latest news from a specific financial news source.",
        "inputSchema": {"type": "object", "properties": {
            "source": {"type": "string", "description": "Source key. Use list_news_sources to see options."},
            "limit": {"type": "integer", "description": "Max articles (1-20). Defaults to 10.",
                      "minimum": 1, "maximum": 20, "default": 10},
        }, "required": ["source"]},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}

_secret_cache = {"value": None, "loaded_at": 0.0}
_feed_cache = {}  # source_key -> (fetched_at, raw_bytes)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
class _TextExtractor(html.parser.HTMLParser):
    """Equivalent of BeautifulSoup(html).get_text(separator=" ", strip=True)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s:
                self.parts.append(s)


def _strip_html(text):
    if not text:
        return ""
    p = _TextExtractor()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup: fall back to a crude strip
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()[:500]
    return " ".join(p.parts)[:500]


_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_BARE_AMP = re.compile(r"&(?!#\d+;|#x[0-9a-fA-F]+;|(?:amp|lt|gt|quot|apos);)")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_XML_DECL = re.compile(r"<\?xml[^>]*\?>")


def _repair_xml(text):
    """HTML named entities -> numeric references, any other stray '&' escaped, control chars dropped."""
    def named(m):
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name)
        return f"&#{cp};" if cp else m.group(0)

    return _CTRL.sub("", _BARE_AMP.sub("&amp;", _NAMED_ENTITY.sub(named, text)))


def _parse_xml(data):
    """Parse feed XML: as-is first; then, like the original, with xml declarations stripped;
    then (beyond the original, which gave up here) with HTML entities and stray '&' repaired."""
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    raw = data.decode("utf-8", errors="replace")
    stripped_lines = "\n".join(line for line in raw.splitlines() if "<?xml" not in line)
    repaired = _repair_xml(_XML_DECL.sub("", raw).lstrip().lstrip(chr(0xFEFF)).lstrip())
    for candidate in (stripped_lines, repaired):
        try:
            return ET.fromstring(candidate.encode("utf-8"))
        except ET.ParseError:
            continue
    return None


def _text(el, tag, ns=None):
    found = el.find(tag)
    if found is not None and found.text:
        return found.text.strip()
    if ns:
        found = el.find(f"{{{NS[ns]}}}{tag}")
        if found is not None and found.text:
            return found.text.strip()
    return ""


def _parse_feed(data, source_key, limit):
    root = _parse_xml(data)
    if root is None:
        return None
    source_name = SOURCE_NAMES.get(source_key, source_key)
    category = SOURCE_CATEGORIES.get(source_key)
    items = root.findall(".//item") or root.findall(f".//{{{NS['atom']}}}entry")
    articles = []
    for item in items[:limit]:
        title = _text(item, "title") or _text(item, "title", "atom") or "No title"
        title = _strip_html(title) or title
        link = _text(item, "link")
        if not link:
            link_el = item.find(f"{{{NS['atom']}}}link")
            if link_el is not None:
                link = link_el.get("href", "")
        summary = (_text(item, "description") or _text(item, "summary", "atom")
                   or _text(item, "encoded", "content") or _text(item, "content", "content") or "")
        published = (_text(item, "pubDate") or _text(item, "published", "atom")
                     or _text(item, "updated", "atom") or _text(item, "date", "dc"))
        summary = _strip_html(summary)
        if source_key in GOOGLE_NEWS_SOURCES:
            suffix = f" - {source_name}"
            if title.endswith(suffix):
                title = title[: -len(suffix)]
            summary = ""
        articles.append({"title": title, "url": link, "source": source_name,
                         "summary": summary, "published": published or None,
                         "category": category})
    return articles


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def _download(source_key):
    """Returns (bytes, None) or (None, short error description). Cached per warm instance."""
    now = time.time()
    cached = _feed_cache.get(source_key)
    if cached and now - cached[0] < FEED_CACHE_SECONDS:
        return cached[1], None
    req = urllib.request.Request(RSS_FEEDS[source_key], headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=FEED_TIMEOUT_SECONDS) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return None, "timeout" if "timed out" in str(reason) else f"unreachable ({str(reason)[:60]})"
    except TimeoutError:
        return None, "timeout"
    except Exception as e:  # noqa: BLE001
        return None, f"error ({type(e).__name__})"
    _feed_cache[source_key] = (now, data)
    return data, None


def fetch_feed(source_key, limit):
    """Returns (articles, error_or_None)."""
    data, err = _download(source_key)
    if err:
        return [], err
    articles = _parse_feed(data, source_key, limit)
    if articles is None:
        return [], "unparseable feed"
    if not articles:
        return [], "no items"
    return articles, None


def fetch_many(sources, limit_per_source):
    """Fetches sources in parallel; returns (articles in source order, {source: error})."""
    keys = [s for s in sources if s in RSS_FEEDS]
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(keys))) as pool:
        futures = {pool.submit(fetch_feed, k, limit_per_source): k for k in keys}
        for fut in concurrent.futures.as_completed(futures):
            results[futures[fut]] = fut.result()
    articles, failures = [], {}
    for k in keys:
        arts, err = results[k]
        articles.extend(arts)
        if err:
            failures[k] = err
    return articles, failures


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def _format_articles(articles, max_articles=20):
    if not articles:
        return "No articles found."
    lines = []
    for i, a in enumerate(articles[:max_articles], 1):
        lines.append(f"{i}. **{a['title']}**")
        lines.append(f"   Source: {a['source']}" + (f" | Category: {a['category']}" if a["category"] else ""))
        if a["published"]:
            lines.append(f"   Published: {a['published']}")
        if a["summary"]:
            lines.append(f"   {a['summary']}")
        lines.append(f"   URL: {a['url']}")
        lines.append("")
    return "\n".join(lines)


def _failures_note(failures):
    if not failures:
        return ""
    parts = [f"{k} ({v})" for k, v in failures.items()]
    return "\n\n_Sources with no articles: " + ", ".join(parts) + "_"


def _int_arg(args, key, default, lo, hi):
    try:
        v = int(args.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _call_tool(name, a):
    if name == "get_latest_news":
        category = a.get("category") or "top"
        limit = _int_arg(a, "limit", 5, 1, 20)
        sources = CATEGORY_SOURCES.get(category, CATEGORY_SOURCES["top"])
        articles, failures = fetch_many(sources, limit)
        text = f"## Latest Financial News — {category.title()}\n\n"
        text += _format_articles(articles, max_articles=limit * len(sources))
        return text + _failures_note(failures)

    if name == "search_financial_news":
        query = (a.get("query") or "").strip()
        category = a.get("category") or "all"
        limit = _int_arg(a, "limit", 10, 1, 50)
        if not query:
            return "Please provide a search query."
        sources = CATEGORY_SOURCES.get(category, ALL_SOURCES)
        everything, failures = fetch_many(sources, 25)
        q = query.lower()
        matched = [x for x in everything if q in x["title"].lower() or q in x["summary"].lower()][:limit]
        text = f"## Financial News Search: '{query}'\n\n"
        if not matched:
            text += f"No articles found matching '{query}'."
        else:
            text += f"Found {len(matched)} article(s):\n\n" + _format_articles(matched, max_articles=limit)
        return text + _failures_note(failures)

    if name == "list_news_sources":
        lines = ["## Available Financial News Sources\n"]
        for key, name_str in SOURCE_NAMES.items():
            lines.append(f"- **{key}** — {name_str} ({SOURCE_CATEGORIES.get(key, 'General')})")
        lines.append("\n### Category Groups")
        for cat, srcs in CATEGORY_SOURCES.items():
            lines.append(f"- **{cat}**: {', '.join(srcs)}")
        return "\n".join(lines)

    if name == "get_news_by_source":
        source = a.get("source") or ""
        limit = _int_arg(a, "limit", 10, 1, 20)
        if source not in RSS_FEEDS:
            return f"Unknown source '{source}'. Available: {', '.join(RSS_FEEDS.keys())}"
        articles, err = fetch_feed(source, limit)
        text = f"## Latest News from {SOURCE_NAMES.get(source, source)}\n\n"
        text += _format_articles(articles, max_articles=limit)
        return text + (_failures_note({source: err}) if err else "")

    return f"Unknown tool: {name}"


# --------------------------------------------------------------------------
# Secret / auth
# --------------------------------------------------------------------------
def _get_secret():
    now = time.time()
    if _secret_cache["value"] and now - _secret_cache["loaded_at"] < SECRET_TTL_SECONDS:
        return _secret_cache["value"]
    import boto3  # bundled in the Lambda runtime

    raw = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ARN"])["SecretString"]
    _secret_cache.update(value=json.loads(raw), loaded_at=now)
    return _secret_cache["value"]


def _authorized(headers):
    # Accept "Bearer <token>" and the common paste slip "Bearer<token>".
    supplied = headers.get("authorization", "").strip()
    if not supplied.lower().startswith("bearer"):
        return False
    expected = _get_secret().get("bearer_token", "")
    return bool(expected) and hmac.compare_digest(supplied[6:].strip().encode(), expected.encode())


def _auth_format(auth):
    """Describes the Authorization header's SHAPE only. Never returns any part of its value."""
    if not auth:
        return "missing"
    a = auth.strip()
    if a[:7].lower() == "bearer ":
        return "bearer-with-space"
    if a[:6].lower() == "bearer":
        return "bearer-no-space"
    return "other-scheme"


# --------------------------------------------------------------------------
# JSON-RPC / MCP
# --------------------------------------------------------------------------
def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _source_sha256():
    import hashlib
    with open(__file__, encoding="utf-8") as f:
        return hashlib.sha256(f.read().strip().encode("utf-8")).hexdigest()


def _handle_message(msg):
    """Returns a response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            return None
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")

    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(mid, {
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [dict(t, annotations={"readOnlyHint": True, "openWorldHint": True})
                                       for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_NAMES:
            return _error(mid, -32602, f"Unknown tool: {name}")
        try:
            text = _call_tool(name, params.get("arguments") or {})
            return _result(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return _result(mid, {"content": [{"type": "text", "text": "Internal error fetching news."}],
                                 "isError": True})
    if method == "server/source_sha256":
        return _result(mid, {"sha256": _source_sha256()})
    if method in ("resources/list", "prompts/list"):
        return _result(mid, {method.split("/")[0]: []})
    if "id" not in msg:
        return None
    return _error(mid, -32601, f"Method not found: {method}")


def _http(status, body=None, extra_headers=None):
    headers = {"Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    resp = {"statusCode": status, "headers": headers}
    if body is not None:
        headers["Content-Type"] = "application/json"
        resp["body"] = json.dumps(body, separators=(",", ":"))
    return resp


def _rpc_summary(raw):
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return "unparseable"
    out = []
    for m in payload if isinstance(payload, list) else [payload]:
        if isinstance(m, dict):
            name = (m.get("params") or {}).get("name") if m.get("method") == "tools/call" else None
            out.append(f"{m.get('method')}:{name}" if name else str(m.get("method")))
    return ",".join(out)


def handler(event, context):
    """Logs one access line per request (never the token), then dispatches."""
    started = time.time()
    resp = _dispatch(event)
    try:
        http = event.get("requestContext", {}).get("http", {})
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        log.info(json.dumps({
            "access": True, "method": http.get("method"), "path": event.get("rawPath"),
            "status": resp.get("statusCode"), "auth_format": _auth_format(headers.get("authorization", "")),
            "ua": headers.get("user-agent"), "rpc": _rpc_summary(raw) if raw else None,
            "resp_bytes": len(resp.get("body") or ""), "ms": int((time.time() - started) * 1000),
        }))
    except Exception:  # noqa: BLE001 - logging must never break a request
        log.exception("access log failed")
    return resp


def _dispatch(event):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path = event.get("rawPath", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if path.startswith("/.well-known/"):
        return _http(404, {"error": "not_found"})
    if not _authorized(headers):
        return _http(401, {"error": "unauthorized"})
    if method == "DELETE":
        return _http(200)
    if method != "POST":
        return _http(405, None, {"Allow": "POST"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        payload = json.loads(raw)
    except ValueError:
        return _http(400, _error(None, -32700, "Parse error"))

    if isinstance(payload, list):
        responses = [r for r in (_handle_message(m) for m in payload) if r is not None]
        return _http(200, responses) if responses else _http(202)
    response = _handle_message(payload)
    return _http(200, response) if response is not None else _http(202)
