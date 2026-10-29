# financial-news-mcp-lambda

The remote (web) version of [financial-news-mcp](https://github.com/cmaurer/financial-news-mcp): the same four tools, running on AWS Lambda behind a public function URL so claude.ai — including scheduled tasks in Anthropic's cloud — can read financial news without your computer being on.

It is a standard-library port of the original stdio server. Tool names, descriptions, input schemas, the 19 RSS sources, the category groups and the markdown output format are the same. Differences:

- **No dependencies.** `urllib`, `xml.etree` and `html.parser` replace httpx, lxml and BeautifulSoup, so the whole server is one file embedded in the CloudFormation template — no build step, fast cold starts. `tests/test_server.py` checks the parser against the original's on the same feeds.
- **Failed sources are named.** The original silently drops a feed that errors or returns nothing. Here each result ends with e.g. `_Sources with no articles: reuters_markets (unreachable …), yahoo_finance (HTTP 429)_`, so a caller (like the morning brief) can report data gaps.
- **More feeds parse.** After the original's two parse attempts, a third pass repairs HTML entities (`&mdash;`, `&nbsp;`) and stray `&` that real feeds ship but XML parsers reject.
- **Parallel and cached.** Feeds are fetched in parallel threads and cached for 5 minutes per warm Lambda instance.
- **`cnbc_business`** shows as "CNBC" (the original lacked its display name).
- **Dead feeds replaced** (see below), keeping their source keys so callers don't change.

## Replaced feeds (Oct 2026)

| Source key | Original feed | Problem | Replacement |
|---|---|---|---|
| `reuters_markets` | `feeds.reuters.com/reuters/businessNews` | Reuters shut down `feeds.reuters.com` in 2020 (DNS no longer resolves) | Google News RSS search: `site:reuters.com markets`, last 2 days |
| `reuters_finance` | `feeds.reuters.com/news/wealth` | same | Google News RSS search: `site:reuters.com (finance OR banking OR economy)`, last 2 days |
| `yahoo_finance` | `finance.yahoo.com/rss/` | HTTP 404; Yahoo's remaining feeds return HTTP 429 to AWS addresses | Google News RSS search: `site:finance.yahoo.com markets`, last 2 days |
| `marketwatch_markets` | Dow Jones `mw_marketpulse` | Responds, but frozen since July 2025 | Dow Jones `mw_bulletins` (MarketWatch breaking news) |

For the three Google News-backed sources, the " - Reuters" / " - Yahoo Finance" suffix Google appends to titles is removed and the summary is left empty (Google's description only repeats the headline). Article links go through `news.google.com` redirects to the publisher. Google's feed terms limit use to personal, non-commercial feed reading. `RSS_FEEDS` in `src/server.py` is the place to change any of these.

## What's deployed

| Item | Value |
|---|---|
| AWS account / region | 381491912865 / us-east-1 |
| CloudFormation stack | `financial-news-mcp` |
| Function | `financial-news-mcp` (Python 3.13, arm64, 512 MB, 60 s) |
| Endpoint | `https://ppl6oixxhm3pzruet2wvqtxosq0zmvil.lambda-url.us-east-1.on.aws/` (stack output `FunctionUrl`) |
| Secret | `financial-news-mcp/config` — `bearer_token` only |
| Logs | `/aws/lambda/financial-news-mcp` (30-day retention) |
| Warm-up | EventBridge Scheduler `financial-news-mcp-warmup`: `/health` invoke at 6:30 America/Chicago, Mon–Fri |
| claude.ai connector | custom connector, "No sign in", `Authorization: Bearer <token>` header |

## How it works

- **Transport:** MCP streamable HTTP, stateless: one POST per JSON-RPC message, plain JSON back.
- **Auth:** `Authorization: Bearer <token>` on every request (401 otherwise); `Bearer<token>` without the space is also accepted.
- **Access log:** one JSON line per request — method, path, status, MCP method/tool, response size, duration, user agent, and the Authorization header's *shape* only (`missing`, `bearer-with-space`, `bearer-no-space`, `other-scheme`). No part of the token is ever logged; the tests assert this.
- **`/health`:** unauthenticated (any method) and returns `{"ok": true}` — or 503 `{"ok": false}` if the secret can't be loaded. It does the same cold-start work as a real request (imports, boto3, Secrets Manager client, secret fetch) and returns nothing else.
- **Warm-up:** an EventBridge Scheduler schedule (`financial-news-mcp-warmup`) invokes the function with a `/health` event at 6:30 America/Chicago on weekdays — five minutes before the 6:35 CT morning-brief scheduled task — so the task's connector doesn't hit a cold start and report the server unavailable. The schedule uses the Chicago timezone so it follows DST along with the task (11:30 UTC in summer, 12:30 UTC in winter). If the task's time changes, change `ScheduleExpression` in `build_template.py` to match.
- **IAM:** the function role can write its own log group and read its one secret.

## Files

```
src/server.py          MCP server + RSS fetching and parsing (the Lambda code)
build_template.py      Writes build/template.json with src/server.py embedded inline
tests/test_server.py   Offline tests (network and secret mocked) + parity check vs. the original parser
```

`build/` is generated and git-ignored.

## Test

```bash
python3 tests/test_server.py
# parity check against the original parser (needs httpx, beautifulsoup4, lxml installed):
ORIGINAL_SRC=~/Documents/GitHub/financial-news-mcp/src python3 tests/test_server.py
```

## Deploy changes

```bash
python3 build_template.py
aws cloudformation deploy --region us-east-1 --stack-name financial-news-mcp \
  --template-file build/template.json --capabilities CAPABILITY_IAM
```

To add, remove or replace a feed, edit `RSS_FEEDS`, `SOURCE_CATEGORIES`, `SOURCE_NAMES` and (if needed) `CATEGORY_SOURCES` in `src/server.py`, run the tests and redeploy.

## Connect to claude.ai

```bash
# copies "Bearer <token>" — the complete header value — to the clipboard without displaying it
printf 'Bearer %s' "$(aws secretsmanager get-secret-value --region us-east-1 --secret-id financial-news-mcp/config \
  --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["bearer_token"])')" | pbcopy
```

Customize → Connectors → + Add → Add custom connector: name `Financial News`, the endpoint URL, **No sign in**, request header `Authorization` = paste.

## Check the live endpoint

```bash
URL=$(aws cloudformation describe-stacks --region us-east-1 --stack-name financial-news-mcp \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionUrl'].OutputValue" --output text)
TOKEN=$(aws secretsmanager get-secret-value --region us-east-1 --secret-id financial-news-mcp/config \
  --query SecretString --output text | python3 -c 'import json,sys;print(json.load(sys.stdin)["bearer_token"])')

curl -s -o /dev/null -w '%{http_code}\n' "$URL" -H 'Content-Type: application/json' -d '{}'   # expect 401
curl -s \"${URL%/}/health\"; echo   # expect {\"ok\":true}, no token needed

# compare with the sha256 printed by tests/test_server.py
curl -s "$URL" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"server/source_sha256"}'; echo

curl -s "$URL" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_latest_news","arguments":{"category":"all","limit":1}}}'; echo
```

## Rotate the bearer token

```bash
NEW=$(openssl rand -hex 24)
aws secretsmanager put-secret-value --region us-east-1 --secret-id financial-news-mcp/config \
  --secret-string "{\"bearer_token\":\"$NEW\"}" > /dev/null
printf 'Bearer %s' "$NEW" | pbcopy; unset NEW
```

Paste into the connector's `Authorization` header; allow up to 5 minutes for the function's cached copy to expire.

## Tear down

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name financial-news-mcp
```

Then remove the connector in claude.ai.
