# Finance Pulse: Python client and MCP server

<!-- mcp-name: io.github.SlothyAfk/finance-pulse -->

Financial news as structured, sourced data. [Finance Pulse](https://fintopic.news/docs) reads financial news as it
is published and turns every article into **statements**: one-sentence facts with sentiment, importance, tickers,
sector, source and publication time. Statements about the same development are clustered into **topics**, and
topics into 19 **themes**. [fintopic.news](https://fintopic.news/) is a front page built on the same API.

The same setup guide, with the other guides and the API reference, is on the docs site:
[Use Finance Pulse from Claude, Cursor or Python](https://fintopic.news/docs/use-with-claude-and-python).

This package gives you two ways to use it:

- a **Python client** for scripts, notebooks and pipelines;
- an **MCP server**, so Claude, Cursor and other MCP clients can query the news directly.

**You need a RapidAPI key that is subscribed to Finance Pulse.** Create a RapidAPI account, open the
[plans page](https://rapidapi.com/evankos/api/finance-pulse/pricing) and subscribe to a plan: Basic is free and is
enough to try everything below. Then copy your `X-RapidAPI-Key` from the API's page on RapidAPI (the endpoint playground shows it). A key that
is not subscribed to a plan is answered with 403.

## Use it from Claude or Cursor (MCP)

The server runs with `uvx`, which is part of [uv](https://docs.astral.sh/uv/getting-started/installation/):
install uv first. The package itself needs no separate install.

**Claude Code**

```bash
claude mcp add --env RAPIDAPI_KEY=YOUR_RAPIDAPI_KEY --transport stdio finance-pulse -- uvx finance-pulse
```

**Claude Desktop** and **Cursor**

- Claude Desktop: Settings > Developer > Edit Config opens `claude_desktop_config.json`
  (macOS `~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`). Quit and restart Claude Desktop
  completely afterwards.
- Cursor: `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "finance-pulse": {
      "command": "uvx",
      "args": ["finance-pulse"],
      "env": { "RAPIDAPI_KEY": "YOUR_RAPIDAPI_KEY" }
    }
  }
}
```

If the server does not start in Claude Desktop, it usually cannot find `uvx`: put the full path (the output of
`which uvx`, or `where uvx` on Windows) in `"command"`.

`uvx` keeps the version it installed first. To move to a new release, use `finance-pulse@latest` in place of
`finance-pulse` once, or run `uv cache clean finance-pulse`.

Then ask things like:

- "What is the news saying about Nvidia today? Only the key facts, with sources."
- "Which tickers are suddenly getting more coverage than yesterday?"
- "What are the top developing stories in Energy, and how did the biggest one grow?"
- "Show me Tesla's daily news sentiment for the last two weeks."

### Tools

| Tool | What it does |
|---|---|
| `search_statements` | Statements by symbol, sector, theme, topic, sentiment, minimum importance and time range |
| `trending` | The developments most outlets are reporting now, or per 2-hour slot over 7 days |
| `find_topics` | Developments for a symbol, sector or theme, by velocity, size or recency |
| `get_topic` | One development: counts, its mix of news, analysis and reddit statements, newest statements with their outlets |
| `sentiment_series` | Daily or hourly news volume and sentiment for a symbol, sector, theme or topic |
| `screen` | Symbols (optionally only equities and ETFs) or sectors ranked by news attention and its change against the previous period |
| `themes` | The standing subjects with their volume and sentiment, optionally only those a symbol or sector appears in |
| `reference` | The data window, known data incidents, theme ids and accepted filter values |

Each tool call is one API request. Results are trimmed and default to small pages.

## Use it from Python

```bash
pip install finance-pulse
export RAPIDAPI_KEY=YOUR_RAPIDAPI_KEY
```

The MCP SDK is installed with the package even if you only use the client.

```python
from datetime import datetime, timedelta, timezone

from finance_pulse import FinancePulse

fp = FinancePulse()          # or FinancePulse("YOUR_RAPIDAPI_KEY")

# The newest key facts about a ticker
for s in fp.statements(symbol="NVDA", importance_min="high", limit=5)["data"]:
    print(s["published_at"][:16], s["sentiment"], s["statement"], f"({s['source_domain']})")

# Which tickers are suddenly in the news (kind= leaves out rates, central banks, countries, ...)
for row in fp.symbols(period="d", sort="change", kind=["equity", "etf"], limit=10)["data"]:
    print(row["id"], row["mentions"], row["change"])

# Daily news volume and sentiment, last two weeks
since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z")
for b in fp.series(symbol="TSLA", interval="day", since=since)["data"]["buckets"]:
    print(b["t"][:10], b["count"], b["score"])

# What most outlets are reporting right now
for t in fp.trending(kind="live")["data"][:10]:
    print(t["sources"], "sources:", t["name"])
```

Every method returns the API's JSON unchanged: `{"data": ..., "next_cursor": ..., "snapshot": {...}}`. The
`snapshot` block says how fresh the data is.

### Follow the news without missing anything

`poll_feed` yields every new statement in the order it entered the API and keeps its position in a file, so it
survives restarts: after a crash nothing is skipped and only the statement you were working on is delivered
again. The first run, with no saved position, starts 24 hours back, and the position is first stored once that
first page has been handled. After a long pause the poller reads the whole backlog since its saved position, one
request per 100 statements; delete the cursor file to start 24 hours back instead.

```python
for s in fp.poll_feed(sector="Energy", importance_min="high", cursor_file="energy.cursor.json"):
    print(s["indexed_at"], s["statement"], s["source_url"])
```

The API builds new data about every 2.5 minutes, so the poller waits 150 seconds between polls by default. Timeouts and server errors are retried; a used-up quota (429)
is raised.

### More than one page

```python
rows = list(fp.iter_statements(symbol=["NVDA", "AMD"], importance_min="medium", max_items=1000))
```

Each page of 100 is one request. Without `max_items` it stops after 500 rows; `max_items=None` reads to the end
of the window.

### Errors

```python
from finance_pulse import FinancePulseError

try:
    fp.statements(symbol="S&P")
except FinancePulseError as e:
    print(e.status, e.title, e.detail, e.param)
    # 400 | Invalid symbol | 'S&P' is not a ticker | symbol
```

A `429` means the plan's request quota or rate limit is used up. Timeouts and network failures are raised as
`FinancePulseError` too, with `status` 0; `e.transient` is true for those and for 5xx answers.

### Methods

| Method | Endpoint |
|---|---|
| `statements`, `iter_statements`, `statement` | `/v2/statements`, `/v2/statements/{id}` |
| `feed`, `poll_feed` | `/v2/feed` |
| `series` | `/v2/series` |
| `symbols`, `sectors` | `/v2/symbols`, `/v2/sectors` |
| `trending` | `/v2/trending` |
| `topics`, `topic` | `/v2/topics`, `/v2/topics/{id}` |
| `themes`, `theme` | `/v2/themes`, `/v2/themes/{id}` |
| `meta` | `/v2/meta` |

The package targets Finance Pulse API 2.0 (the `/v2` endpoints). Parameters and response fields are documented in
the [API reference](https://fintopic.news/docs/reference), and the
[guides](https://fintopic.news/docs) show complete worked examples.

## What the data is, and is not

- **Symbols are canonical entity codes with a kind.** Equities and ETFs appear under their ticker (`NVDA`,
  `0700.HK`); indices, rates, FX, crypto, commodities and organisations under short codes (`SPX`, `US10Y`, `USD`,
  `BTC`, `GOLD`, `FED`). Use `kind=["equity", "etf"]` for tradable tickers only.
- **Sentiment describes the statement**, not a price forecast. A symbol's sentiment is the count of its
  statements' labels.
- **The window is a rolling 62 days.** `meta()` lists known outages and delays under `incidents`.

See [the docs](https://fintopic.news/docs) for the full list of caveats.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use canned responses and make no API requests.

### Releasing

1. Set the new version in `src/finance_pulse/__init__.py` and in `server.json` (two places).
2. Build and upload to PyPI (`uv build && uv publish`). From GitHub Actions, use `pypa/gh-action-pypi-publish`
   v1.14.2 or newer: older versions reject the metadata version this build writes.
3. Only then run `mcp-publisher publish`: the registry verifies the package on PyPI (it looks for the
   `mcp-name` line in its description), so the release must be there first.

## Licence

MIT
