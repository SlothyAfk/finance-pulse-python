"""MCP server for Finance Pulse: lets an assistant query financial news as sourced statements.

Run:  RAPIDAPI_KEY=... uvx finance-pulse        (stdio transport; `finance-pulse-mcp` is the same command)

Every tool is one API request (plans have a monthly request quota), so results are trimmed to the fields an
assistant needs and default to small pages.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .client import RAPIDAPI, FinancePulse, FinancePulseError

BACKEND = RAPIDAPI

Sector = Literal["CommunicationServices", "ConsumerDiscretionary", "ConsumerStaples", "Energy", "Financials",
                 "HealthCare", "Industrials", "InformationTechnology", "Materials", "RealEstate", "Utilities"]
Sentiment = Literal["bullish", "bearish", "neutral"]
Importance = Literal["low", "medium", "high", "ultra"]
SourceType = Literal["news", "speculative", "reddit"]

# Argument types: the description sits on the field, where the model reads it when it fills the call.
Symbols = Annotated[Optional[str], Field(description="Ticker or entity code, e.g. NVDA. Comma-separate several (OR), up to 10.")]
OneSymbol = Annotated[Optional[str], Field(description="One ticker or entity code, e.g. NVDA")]
SectorArg = Annotated[Optional[Sector], Field(description="GICS sector")]
ThemeId = Annotated[Optional[str], Field(description="Theme id from `themes` or `reference`")]
TopicId = Annotated[Optional[str], Field(description="Topic (development) id, as returned in topic_id or topics[].id")]
Time = Annotated[Optional[str], Field(description="ISO-8601 time, e.g. 2026-10-01T00:00:00Z")]
Cursor = Annotated[Optional[str], Field(description="next_cursor from the previous call, for the next page")]
PageSize = Annotated[int, Field(ge=1, le=100, description="How many to return, 1-100")]

INSTRUCTIONS = """Finance Pulse turns financial news into statements: one-sentence facts extracted from articles,
each with sentiment (bullish/bearish/neutral), importance (low/medium/high/ultra), symbols, GICS sectors, the
source and the article's publication time. Statements about the same development are clustered into topics, and
topics belong to themes. Data covers a rolling window of about two months and is rebuilt every few minutes.

Each tool call uses one request of the user's plan quota, so prefer one well-filtered call over several:
news about a company or sector -> search_statements; what is big right now -> trending; which names are getting
unusual attention -> screen; how coverage or tone changed over time -> sentiment_series; which broad subjects a
company or sector is in the news for -> themes.
Cite `source` and `published_at` when you quote a statement; `source_type` says whether it comes from news,
speculative analysis or reddit. Symbols are codes extracted by a language model: most equities match their
ticker, but macro entities are codes too (FED, ECB, CRUDE, US10Y). Sentiment describes the statement, not a
price forecast. Every result carries `lag_days`, the age of the newest statement in days: if it is well above
zero, news collection is behind, so say the data may be stale. Call `reference` when you need theme ids or want
to know about gaps in the data."""

server = MCPServer("finance-pulse", title="Finance Pulse", instructions=INSTRUCTIONS, version=__version__,
                   website_url="https://fintopic.news/docs")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

_client: Optional[FinancePulse] = None


def client() -> FinancePulse:
    global _client
    if _client is None:
        if not (os.environ.get(BACKEND.key_env) or "").strip():
            raise ToolError(f"{BACKEND.key_env} is not set for the finance-pulse server. Add it to the server's "
                            f"env in the MCP client's configuration; get a free key at {BACKEND.signup_url}")
        try:
            _client = FinancePulse(backend=BACKEND)
        except ValueError as exc:       # a key that cannot be one
            raise ToolError(str(exc)) from exc
    return _client


def _call(method: str, *args, **kwargs) -> dict:
    """One API request; errors become messages the assistant can act on (ToolError text reaches the model,
    any other exception is reported only as "Error executing tool")."""
    api = client()
    try:
        return getattr(api, method)(*args, **kwargs)
    except FinancePulseError as exc:
        if exc.status == 429:
            raise ToolError(f"The plan's request quota or rate limit is used up ({exc.title}). "
                            f"Plans: {BACKEND.signup_url}") from exc
        if exc.status in (401, 403):
            raise ToolError(f"{BACKEND.key_env} is missing, wrong or not subscribed to Finance Pulse ({exc.title}). "
                            f"Subscribe (there is a free plan): {BACKEND.signup_url}") from exc
        if exc.transient:
            raise ToolError(f"Finance Pulse could not be reached or is temporarily unavailable ({exc}). "
                            f"Try again in a minute.") from exc
        raise ToolError(str(exc)) from exc


def _statement(s: dict) -> dict:
    out = {"id": s["id"], "statement": s["statement"], "published_at": s["published_at"],
           "sentiment": s["sentiment"], "importance": s["importance"], "source_type": s.get("source_type"),
           "symbols": s.get("symbols") or [], "sectors": s.get("sectors") or [],
           "source": s.get("source_domain") or "", "url": s.get("source_url"),
           "topic_id": s.get("topic_id"), "topic": s.get("topic_name")}
    if s.get("entities"):                       # what each symbol code stands for
        out["entities"] = [{k: e.get(k) for k in ("code", "name", "kind")} for e in s["entities"]]
    return out


def _topic(t: dict) -> dict:
    latest = t.get("latest_statement") or {}
    out = {"id": t["id"], "name": t["name"], "theme_id": t.get("theme_id"), "updated_at": t.get("updated_at"),
           "statements": t.get("statements"), "sources": t.get("sources"),
           "statements_7d": t.get("statements_7d"), "statements_24h": t.get("statements_24h"),
           "statements_4h": t.get("statements_4h"),
           "sentiment": t.get("sentiment"), "symbols": (t.get("symbols") or [])[:8],
           "latest_statement": latest.get("statement"), "latest_source": latest.get("source_domain")}
    if "trending" in t:
        out["trending"] = t["trending"]
    return out


def _fresh(response: dict) -> dict:
    snapshot = response.get("snapshot") or {}
    return {"built_at": snapshot.get("built_at"), "window_start": snapshot.get("window_start"),
            "lag_days": snapshot.get("lag_days")}


@server.tool(title="Search news statements", annotations=READ_ONLY)
def search_statements(
        symbol: Symbols = None, sector: SectorArg = None, theme_id: ThemeId = None, topic_id: TopicId = None,
        sentiment: Annotated[Optional[Sentiment], Field(description="Only statements with this sentiment")] = None,
        importance_min: Annotated[Optional[Importance], Field(description="Minimum importance; high = key facts only")] = None,
        source_type: Annotated[Optional[SourceType], Field(description="news leaves out speculative analysis and reddit")] = None,
        since: Time = None, until: Time = None, limit: PageSize = 15, cursor: Cursor = None) -> dict:
    """Search extracted news statements, newest first. Use for "what is the news saying about X".

    symbol: ticker or entity code, e.g. NVDA; comma-separate several (OR), no spaces inside a code.
    importance_min=high keeps only key facts. source_type=news leaves out speculative analysis and reddit.
    since/until: ISO-8601 times on the article's publication time, e.g. 2026-10-01T00:00:00Z.
    limit: 1-100. Pass next_cursor back as cursor for the next page (null means no more).
    """
    r = _call("statements", symbol=symbol, sector=sector, theme_id=theme_id, topic_id=topic_id, sentiment=sentiment,
              importance_min=importance_min, source_type=source_type, since=since, until=until, limit=limit,
              cursor=cursor)
    return {"statements": [_statement(s) for s in r["data"]], "next_cursor": r.get("next_cursor"), **_fresh(r)}


@server.tool(title="Trending developments", annotations=READ_ONLY)
def trending(
        kind: Annotated[Literal["live", "slot"], Field(description="live = last 2 hours; slot = 2-hour slots over 7 days")] = "live",
        symbol: OneSymbol = None, theme_id: ThemeId = None,
        limit: Annotated[int, Field(ge=1, le=300, description="How many to return, taken from the top of the API's ranking; `total` in the result says how many there are")] = 15) -> dict:
    """The developments most outlets are reporting. Use for "what is moving / what is the big news right now".

    kind=live: the last 2 hours; kind=slot: the top stories of each 2-hour slot over the last 7 days.
    For the whole day rather than the last 2 hours, use find_topics with sort=velocity (last 24 hours).
    kind=slot lists up to 10 stories per slot over 7 days, so raise limit when you need more than the top of it;
    each item's `trending` labels say which slots it was in and at what rank.
    Each item has its distinct source count, sentiment counts and newest statement. symbol: one code only.
    """
    r = _call("trending", kind=kind, symbol=symbol, theme_id=theme_id)
    return {"topics": [_topic(t) for t in r["data"][:limit]], "total": len(r["data"]), **_fresh(r)}


@server.tool(title="Get one development", annotations=READ_ONLY)
def get_topic(topic_id: Annotated[str, Field(description="Topic (development) id, as returned in topic_id or topics[].id")]) -> dict:
    """One development in full: its counts, how many of its statements come from news, speculative analysis and
    reddit (source_mix), and its 20 newest statements, each with its outlet. For older statements call
    search_statements with topic_id and the returned statements_cursor as cursor."""
    r = _call("topic", topic_id)
    t = r["data"]
    return {**_topic(t), "created_at": t.get("created_at"), "source_mix": t.get("source_mix"),
            "statements_page": [_statement(s) for s in t.get("statements_page") or []],
            "statements_cursor": t.get("statements_cursor"), **_fresh(r)}


@server.tool(title="Find developments", annotations=READ_ONLY)
def find_topics(
        symbol: Symbols = None, sector: SectorArg = None, theme_id: ThemeId = None,
        sort: Literal["recent", "size", "velocity", "oldest"] = "velocity",
        min_statements: Annotated[int, Field(ge=1, description="Leave out developments with fewer statements")] = 3,
        limit: PageSize = 15, cursor: Cursor = None) -> dict:
    """List developments for a symbol, sector or theme. sort=velocity: most statements in the last 24 hours;
    size: most statements in the window; recent: latest statement first. symbol: comma-separate several (OR)."""
    r = _call("topics", symbol=symbol, sector=sector, theme_id=theme_id, sort=sort, min_statements=min_statements,
              limit=limit, cursor=cursor)
    return {"topics": [_topic(t) for t in r["data"]], "next_cursor": r.get("next_cursor"), **_fresh(r)}


@server.tool(title="Themes", annotations=READ_ONLY)
def themes(symbol: Symbols = None, sector: SectorArg = None,
           sort: Annotated[Literal["size", "topics", "recent", "velocity"], Field(description="size = statements; topics = developments; recent = last statement; velocity = statements in the last 24 hours")] = "size") -> dict:
    """The standing subjects the news is grouped into (Energy, Central banks & rates, ...), each with its number
    of developments and statements, 24-hour volume, distinct sources and sentiment counts. With symbol or sector:
    only the themes that have statements about it; the counts are still those of the whole theme, so use
    search_statements with theme_id and symbol for the symbol's own statements. Use the returned id as theme_id
    in the other tools."""
    r = _call("themes", symbol=symbol, sector=sector, sort=sort)
    keep = ("id", "name", "topics", "statements", "statements_24h", "statements_4h", "sources", "sentiment",
            "sentiment_score", "updated_at")
    return {"themes": [{k: t.get(k) for k in keep} for t in r["data"]], **_fresh(r)}


@server.tool(title="News volume and sentiment over time", annotations=READ_ONLY)
def sentiment_series(symbol: OneSymbol = None, sector: SectorArg = None, theme_id: ThemeId = None,
                     topic_id: TopicId = None, interval: Literal["day", "hour"] = "day",
                     since: Time = None, until: Time = None) -> dict:
    """News volume and sentiment over time for exactly one of symbol, sector, theme_id or topic_id.

    Each bucket: t (UTC start), count, bullish, bearish, neutral, sources (distinct outlets) and
    score = (bullish - bearish) / count. Buckets with no statements are omitted here. Unless `until` is set, the
    series ends at the current, still incomplete day or hour: do not read a low last bucket as a drop. interval=hour covers at most 14 days and
    defaults to the last 7; interval=day defaults to the whole window.
    """
    if sum(bool(v) for v in (symbol, sector, theme_id, topic_id)) != 1:
        raise ToolError("Pass exactly one of symbol, sector, theme_id, topic_id")
    r = _call("series", symbol=symbol, sector=sector, theme_id=theme_id, topic_id=topic_id, interval=interval,
              since=since, until=until)
    buckets = [{k: b.get(k) for k in ("t", "count", "bullish", "bearish", "neutral", "sources", "score")}
               for b in r["data"]["buckets"] if b.get("count")]
    return {"entity": r["data"]["entity"], "interval": interval, "buckets": buckets,
            "empty_buckets_omitted": len(r["data"]["buckets"]) - len(buckets), **_fresh(r)}


@server.tool(title="Screen symbols or sectors by news attention", annotations=READ_ONLY)
def screen(
        rank: Literal["symbols", "sectors"] = "symbols",
        period: Annotated[Literal["d", "w", "window"], Field(description="d = 24 hours, w = 7 days, window = everything held")] = "d",
        sort: Literal["change", "mentions", "sources", "score"] = "change", sector: SectorArg = None,
        entity_kinds: Annotated[Optional[str], Field(description='Comma-separated entity kinds, e.g. "equity,etf"')] = None,
        min_mentions: Annotated[int, Field(ge=1, description="Leave out codes with fewer mentions")] = 3,
        limit: Annotated[int, Field(ge=1, le=500, description="How many rows to return")] = 20) -> dict:
    """Rank symbols (or the 11 GICS sectors) by news attention. Use for "which tickers are suddenly in the news".

    period: d = last 24 hours, w = last 7 days, window = everything held. sort=change ranks by the rise in
    mentions against the previous period (not available for period=window; use sort=mentions there).
    Each row: mentions, topics, sources (distinct outlets), sentiment counts, score and change.
    Only for rank=symbols: sector limits the screen to one sector; min_mentions drops rarely mentioned codes;
    entity_kinds narrows the kind of entity, comma-separated, from: equity, etf, index, rate, fx, crypto,
    commodity, org, private, country. Without it the list mixes companies with rates, commodities and central
    banks; pass "equity,etf" for tradable tickers.
    """
    if rank == "sectors":
        if sector or entity_kinds:
            raise ToolError("sector and entity_kinds only apply to rank=symbols")
        r = _call("sectors", period=period, sort=sort)
    else:
        r = _call("symbols", period=period, sort=sort, sector=sector, kind=entity_kinds, min_mentions=min_mentions,
                  limit=limit)
    return {"rank": rank, "period": period, "rows": r["data"][:limit], **_fresh(r)}


@server.tool(title="Data window, themes and accepted values", annotations=READ_ONLY)
def reference() -> dict:
    """The data's window and size, known data incidents (outages, delays), the themes with their ids, and the
    accepted sector, sentiment, importance, source-type and entity-kind values. Call once; it rarely changes."""
    r = _call("meta")
    return {**r["data"], **_fresh(r)}


def main() -> None:
    if sys.stdin.isatty():
        print("finance-pulse is an MCP server: it talks to an MCP client (Claude, Cursor, ...) over stdin/stdout.\n"
              "Setup: https://github.com/SlothyAfk/finance-pulse-python#readme", file=sys.stderr)
        sys.exit(2)                     # no MCP client starts a server with a terminal as its stdin
    logging.getLogger("httpx").setLevel(logging.WARNING)    # one INFO line per request otherwise fills client logs
    server.run()


if __name__ == "__main__":
    main()
