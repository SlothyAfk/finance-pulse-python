"""MCP server for Finance Pulse: lets an assistant query financial news as sourced statements.

Run:  RAPIDAPI_KEY=... uvx finance-pulse        (stdio transport; `finance-pulse-mcp` is the same command)

Every tool is one API request (plans have a monthly request quota), so results are trimmed to the fields an
assistant needs and default to small pages.
"""
from __future__ import annotations

import functools
import json
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

# Argument types: the description sits on the field, where the model reads it when it fills the call.
Symbols = Annotated[Optional[str], Field(description="Ticker or entity code, e.g. NVDA. Comma-separate several (OR).")]
OneSymbol = Annotated[Optional[str], Field(description="One ticker or entity code, e.g. NVDA")]
SectorArg = Annotated[Optional[Sector], Field(description="GICS sector")]
ThemeId = Annotated[Optional[str], Field(description="Theme id from `themes` or `reference`")]
TopicId = Annotated[Optional[str], Field(description="Topic (development) id, as returned in topic_id or topics[].id")]
StorylineId = Annotated[Optional[str], Field(description="Storyline id, as returned by `storylines`, `get_symbol` or a topic's storylines")]
Time = Annotated[Optional[str], Field(description="ISO-8601 time, e.g. 2026-10-01T00:00:00Z")]
Cursor = Annotated[Optional[str], Field(description="next_cursor from the previous call, for the next page; keep every other argument the same")]
PageSize = Annotated[int, Field(ge=1, le=50, description="How many to return per call; use the cursor for more")]

INSTRUCTIONS = """Finance Pulse turns financial news into statements: one-sentence facts taken from articles, each
with sentiment (bullish/bearish/neutral), importance (low/medium/high/ultra), symbols, GICS sectors, the source,
the article's headline and its publication time. Statements about the same development are grouped into topics;
one company's topics that continue each other form a storyline; topics also belong to standing themes. The data
reaches back as far as the user's plan includes: `window_start` in every result says where that history starts.

Each tool call normally uses one request of the user's plan quota, so prefer one well-filtered call over several:
everything about one company -> get_symbol; news about a company or sector -> search_statements; what is big
right now -> trending; which names are getting unusual attention -> screen; how coverage or tone changed over
time -> sentiment_series; how one company's story unfolded -> storylines, then get_storyline; which broad
subjects a company or sector is in the news for -> themes; a company's code from its name -> find_symbol.

Cite `source` and `published_at` when you quote a statement; `source_type` says whether it comes from news,
speculative analysis or reddit. Symbols are entity codes with a kind: equities and ETFs under their ticker
(NVDA, 0700.HK), and indices, rates, currencies, crypto, commodities and organisations under short codes (SPX,
USD, BTC, FED); `entities` on a statement gives each code's name and kind. Sentiment describes the
statement, not a price forecast. Topics and storylines are grouped automatically and can be wrong or incomplete:
do not count them as events. Counts on topics, storylines and themes cover all the API holds for them, so they
can be more than the plan's history lets you page through. Every result carries `built_at`, when the data was
last updated, and `lag_days`, the age of the newest statement at that moment: if `built_at` is old or `lag_days` is well above zero, say the
data may be out of date."""

server = MCPServer("finance-pulse", title="Finance Pulse", instructions=INSTRUCTIONS, version=__version__,
                   website_url="https://fintopic.news/docs")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def _tool(title: str):
    """Register a tool. The function keeps returning a dict (that is what the tests call); the model receives
    it as compact JSON text, which is a fifth to a third shorter than the SDK's indented rendering."""
    def register(fn):
        @functools.wraps(fn)
        def as_text(*args, **kwargs) -> str:
            text = json.dumps(fn(*args, **kwargs), separators=(",", ":"), ensure_ascii=False)
            return text.encode("utf-8", "replace").decode()     # a lone surrogate must not end the session
        server.tool(title=title, annotations=READ_ONLY, structured_output=False)(as_text)
        return fn
    return register

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


def _call(method: str, *args, _hint: str = "", _not_found: str = "", **kwargs) -> dict:
    """One API request; errors become messages the assistant can act on (ToolError text reaches the model,
    any other exception is reported only as "Error executing tool"). `_hint` is added to a 400 or 404: what
    to do instead. `_not_found` replaces the API's own 404 explanation, which names API paths."""
    api = client()
    try:
        return getattr(api, method)(*args, **kwargs)
    except ValueError as exc:               # an argument the client refuses before any request
        raise ToolError(str(exc)) from exc
    except FinancePulseError as exc:
        seconds = round(exc.retry_after) if exc.retry_after and exc.retry_after >= 1 else 0
        wait = f"in {seconds} second{'s' if seconds != 1 else ''}" if seconds else "shortly"
        if exc.quota_used_up:
            raise ToolError(f"The plan's request quota is used up ({exc.title}). Retrying will not help until it "
                            f"resets or the plan changes; tell the user. Plans: {BACKEND.signup_url}") from exc
        if exc.status == 429:
            raise ToolError(f"The plan's rate limit was reached or too many requests are open at once "
                            f"({exc.title}). This usually passes: try again {wait}, one call at a time.") from exc
        if exc.status in (401, 403):
            raise ToolError(f"{BACKEND.key_env} is missing, wrong or not subscribed to Finance Pulse ({exc.title}). "
                            f"Subscribe (there is a free plan): {BACKEND.signup_url}") from exc
        if exc.transient:
            cause = f"{exc.status} {exc.title}" if exc.status else str(exc)
            raise ToolError(f"Finance Pulse could not be reached or is temporarily unavailable ({cause}). "
                            f"Try again {wait}.") from exc
        if exc.status == 404 and (_hint or _not_found):
            raise ToolError(" ".join(part for part in (f"{exc.title}.", _not_found, _hint) if part)) from exc
        if exc.status == 400 and _hint:
            raise ToolError(f"{exc.title}{': ' + exc.detail if exc.detail else ''}. {_hint}") from exc
        raise ToolError(str(exc)) from exc


# ---- trimming: what an assistant needs from each record ---------------------------------------------------------

def _entity(e: Optional[dict]) -> Optional[dict]:
    return {k: e.get(k) for k in ("code", "name", "kind")} if e else None


def _statement(s: dict) -> dict:
    out = {"id": s["id"], "statement": s["statement"], "published_at": s["published_at"],
           "sentiment": s["sentiment"], "importance": s["importance"], "source_type": s.get("source_type"),
           "symbols": s.get("symbols") or [], "sectors": s.get("sectors") or [],
           "source": s.get("source_domain") or "", "url": s.get("source_url"),
           "headline": s.get("article_title"),
           "topic_id": s.get("topic_id"), "topic": s.get("topic_name")}
    if s.get("entities"):                       # what each symbol code stands for
        out["entities"] = [_entity(e) for e in s["entities"]]
    return out


def _one(symbol: Optional[str], tool: str) -> None:
    """These endpoints take a single symbol: refuse a list before it costs a request or merges two series."""
    if symbol and "," in symbol:
        raise ToolError(f"{tool} takes one symbol, not a list: call it once per symbol")


def _topic(t: dict) -> dict:
    latest = t.get("latest_statement") or {}
    out = {"id": t["id"], "name": t["name"], "theme_id": t.get("theme_id"), "updated_at": t.get("updated_at"),
           "statements": t.get("statements"), "sources": t.get("sources"),
           "statements_7d": t.get("statements_7d"), "statements_24h": t.get("statements_24h"),
           "statements_4h": t.get("statements_4h"),
           "sentiment": t.get("sentiment"), "symbols": (t.get("symbols") or [])[:8],
           "latest_statement": latest.get("statement"), "latest_source": latest.get("source_domain"),
           "latest_published_at": latest.get("published_at"), "latest_url": latest.get("source_url"),
           "latest_headline": latest.get("article_title")}
    if t.get("storylines"):                     # null = not placed yet, [] = in none: both left out
        out["storylines"] = [{k: s.get(k) for k in ("id", "symbol", "topics")} for s in t["storylines"]]
    if "trending" in t:                         # where it ranked; a long-running story has many labels
        out["trending"] = (t["trending"] or [])[:3]
        out["trending_labels"] = len(t["trending"] or [])
    return out


def _storyline(s: dict) -> dict:
    out = {k: s.get(k) for k in ("id", "symbol", "name", "first_seen", "last_seen", "topics", "statements",
                                 "mentions", "mentions_7d", "mentions_24h", "sources", "bullish", "bearish",
                                 "neutral")}
    if s.get("top_members"):                    # its largest topics; all of them when `topics` is no larger
        out["top_topics"] = [{k: m.get(k) for k in ("id", "name", "created_at")} for m in s["top_members"]]
    if s.get("related"):                        # the same news under other symbols
        out["related"] = [{k: r.get(k) for k in ("id", "symbol", "name", "shared_topics")} for r in s["related"]]
    return out


def _page(rows: Optional[list], cursor: Optional[str], keep: int) -> dict:
    """The newest statements, cut to `keep`. The API's cursor continues after its own full page, so it is only
    passed on when nothing was cut: after a cut, a cursor would skip the statements in between."""
    rows = rows or []
    cut = len(rows) > keep
    return {"statements_page": [_statement(s) for s in rows[:keep]],
            "statements_cursor": None if cut else cursor,
            "more_statements": cut or bool(cursor)}


def _member(m: dict) -> dict:
    return {k: m.get(k) for k in ("id", "name", "created_at", "updated_at", "statements", "mentions")}


def _period(p: Optional[dict]) -> Optional[dict]:
    if not p:
        return None
    return {k: p.get(k) for k in ("mentions", "topics", "sources", "bullish", "bearish", "neutral", "score",
                                  "change", "mentions_single", "score_single", "last_seen")}


def _fresh(response: dict) -> dict:
    snapshot = response.get("snapshot") or {}
    return {"built_at": snapshot.get("built_at"), "window_start": snapshot.get("window_start"),
            "lag_days": snapshot.get("lag_days")}


# ---- statements -------------------------------------------------------------------------------------------------

@_tool("Search news statements")
def search_statements(
        symbol: Symbols = None, sector: SectorArg = None, theme_id: ThemeId = None, topic_id: TopicId = None,
        storyline_id: StorylineId = None,
        sentiment: Annotated[Optional[Sentiment], Field(description="Only statements with this sentiment")] = None,
        importance_min: Annotated[Optional[Importance], Field(description="Minimum importance; high = key facts only")] = None,
        source_type: Annotated[Optional[str], Field(description="One of: news, speculative, reddit. news leaves out speculative analysis and reddit")] = None,
        since: Time = None, until: Time = None, limit: PageSize = 15, cursor: Cursor = None) -> dict:
    """Search news statements, newest first. Use for "what is the news saying about X".

    symbol: ticker or entity code, e.g. NVDA; comma-separate several (OR), no spaces inside a code.
    importance_min=high keeps only key facts. source_type=news leaves out speculative analysis and reddit.
    storyline_id: every statement of that storyline's topics; add symbol for only those naming its company.
    since/until: ISO-8601 times on the article's publication time, e.g. 2026-10-01T00:00:00Z.
    Each statement has its source, the article's headline and its topic. Pass next_cursor back as cursor for the
    next page (null means no more).
    """
    r = _call("statements", symbol=symbol, sector=sector, theme_id=theme_id, topic_id=topic_id,
              storyline_id=storyline_id, sentiment=sentiment, importance_min=importance_min, source_type=source_type,
              since=since, until=until, limit=limit, cursor=cursor)
    return {"statements": [_statement(s) for s in r["data"]], "next_cursor": r.get("next_cursor"), **_fresh(r)}


# ---- symbols ----------------------------------------------------------------------------------------------------

@_tool("Everything about one symbol")
def get_symbol(
        code: Annotated[str, Field(min_length=1, description="Ticker or entity code, e.g. NVDA. Non-US tickers carry an exchange suffix (0700.HK). When unsure of the code, call find_symbol first.")],
        statements: Annotated[int, Field(ge=0, le=20, description="How many of its newest statements to include")] = 8,
        topics: Annotated[int, Field(ge=0, le=10, description="How many of its largest topics to include")] = 5) -> dict:
    """One company or other entity in a single call. Use first for "what is going on with X".

    Returns who it is (name, kind, exchange); its mentions, topics, distinct sources, sentiment counts and score
    for the last 24 hours (d), the last 7 days (w) and all the history the plan includes, with the change
    against the period before and the counts over statements that name only this entity (`mentions_single`, `score_single`); the
    sectors of its statements; its largest topics; its storylines with the most mentions lately; the entities
    it shares topics with (`related`, empty for a rarely mentioned symbol); and its newest statements.
    A period without any mention is left out of `periods`. `more_statements` says whether the symbol has
    more statements than shown. If you expect to need them, ask for `statements` at its maximum: only then is
    `statements_cursor` set, and search_statements(symbol=..., cursor=...) continues exactly after them. With
    fewer, the cursor is null and search_statements(symbol=...) starts again at the newest, repeating the ones
    shown here. Do not guess a code: a wrong one costs a request. Look it up with find_symbol.
    """
    r = _call("symbol", code, _not_found="No statement within the plan's history mentions this code.",
              _hint="If the code itself is in doubt, look it up with find_symbol (pass part of the name).")
    d = r["data"]
    return {"id": d.get("id"), "name": d.get("name"), "kind": d.get("kind"), "exchange": d.get("exchange"),
            "country": d.get("country"),
            "periods": {k: _period(v) for k, v in (d.get("periods") or {}).items()},
            "sectors": d.get("sectors"),
            "top_topics": [_topic(t) for t in (d.get("top_topics") or [])[:topics]],
            "top_storylines": [{k: s.get(k) for k in ("id", "name", "topics", "mentions", "mentions_7d", "last_seen")}
                               for s in d.get("top_storylines") or []],
            "related": [{k: x.get(k) for k in ("id", "name", "kind", "shared_topics")} for x in d.get("related") or []],
            **_page(d.get("statements_page"), d.get("statements_cursor"), statements), **_fresh(r)}


@_tool("Find a symbol's code by name")
def find_symbol(
        query: Annotated[str, Field(min_length=2, max_length=40, description='Part of a name or code, e.g. "micron"')],
        limit: Annotated[int, Field(ge=1, le=50, description="How many matches to return")] = 10) -> dict:
    """Look up the code of a company, index, currency, commodity or organisation by name: "micron" finds MU.
    The query is matched against codes and registered names, so use the company's own name rather than a brand
    (Alphabet, not Google). Returns the matches mentioned within the plan's history, most mentioned first, each
    with its code (id), name and kind; an empty list means nothing there matches, and a list as long as `limit`
    may have more matches behind it. Use the code as `symbol` or `code` in the other tools."""
    r = _call("symbols", period="window", sort="mentions", q=query, min_mentions=1, limit=limit)
    return {"matches": [{k: x.get(k) for k in ("id", "name", "kind", "mentions", "last_seen")} for x in r["data"][:limit]],
            **_fresh(r)}


@_tool("Screen symbols or sectors by news attention")
def screen(
        rank: Literal["symbols", "sectors"] = "symbols",
        period: Annotated[Literal["d", "w", "window"], Field(description="d = last 24 hours, w = last 7 days, window = all the history the plan includes")] = "d",
        sort: Literal["change", "mentions", "sources", "score"] = "change", sector: SectorArg = None,
        entity_kinds: Annotated[Optional[str], Field(description='Comma-separated entity kinds from: equity, etf, index, rate, fx, crypto, commodity, org, private, country. E.g. "equity,etf"')] = None,
        min_mentions: Annotated[int, Field(ge=1, description="Leave out codes with fewer mentions")] = 3,
        since: Annotated[Optional[str], Field(description="Start of a custom interval (ISO-8601) to count instead of `period`")] = None,
        until: Annotated[Optional[str], Field(description="End of the custom interval (ISO-8601); omit for up to now")] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="How many rows to return")] = 20) -> dict:
    """Rank symbols (or the GICS sectors) by news attention. Use for "which tickers are suddenly in the news".

    period: d = last 24 hours, w = last 7 days, window = all the history the plan includes; or pass since (and
    until) to count any interval. sort=change ranks by the rise in mentions against the period (or equally
    long interval) before. `change` is null when there is nothing before to compare with: for period=window,
    for an interval given only by `until`, and for an interval whose preceding interval reaches before the
    plan's history. Use sort=mentions then.
    Each row: mentions, topics, sources (distinct outlets), sentiment counts, score and change; symbol rows also
    carry name, kind and the counts over statements naming only that symbol (`mentions_single`, `score_single`).
    Only for rank=symbols: sector limits the screen to one sector; min_mentions drops rarely mentioned codes;
    entity_kinds narrows the kind of entity (`reference` lists the kinds). Without it the list mixes companies
    with rates, commodities and central banks; pass "equity,etf" for tradable tickers.
    """
    if rank == "sectors":
        if sector or entity_kinds:
            raise ToolError("sector and entity_kinds only apply to rank=symbols")
        r = _call("sectors", period=period, sort=sort, since=since, until=until)
    else:
        r = _call("symbols", period=period, sort=sort, sector=sector, kind=entity_kinds, min_mentions=min_mentions,
                  since=since, until=until, limit=limit)
    rows = [{k: v for k, v in row.items() if v is not None or k in ("change", "score")} for row in r["data"][:limit]]
    out = {"rank": rank, "period": r.get("period", period), "rows": rows, **_fresh(r)}
    if r.get("since"):                          # a custom interval, as the API rounded it
        out["since"], out["until"] = r.get("since"), r.get("until")
    return out


@_tool("News volume and sentiment over time")
def sentiment_series(symbol: OneSymbol = None, sector: SectorArg = None, theme_id: ThemeId = None,
                     topic_id: TopicId = None, interval: Literal["day", "hour"] = "day",
                     since: Time = None, until: Time = None) -> dict:
    """News volume and sentiment over time for exactly one of symbol, sector, theme_id or topic_id.

    Each bucket: t (UTC start), count, bullish, bearish, neutral, sources (distinct outlets) and
    score = (bullish - bearish) / count. Buckets with no statements are omitted here. The last bucket is usually
    the current, still incomplete day or hour: do not read a low last bucket as a drop.
    interval=hour is for short ranges: pass since/until, and if the range is too long the error says the maximum.
    """
    if sum(bool(v) for v in (symbol, sector, theme_id, topic_id)) != 1:
        raise ToolError("Pass exactly one of symbol, sector, theme_id, topic_id")
    _one(symbol, "sentiment_series")
    r = _call("series", symbol=symbol, sector=sector, theme_id=theme_id, topic_id=topic_id, interval=interval,
              since=since, until=until)
    buckets = [{k: b.get(k) for k in ("t", "count", "bullish", "bearish", "neutral", "sources", "score")}
               for b in r["data"]["buckets"] if b.get("count")]
    return {"entity": r["data"]["entity"], "interval": interval, "buckets": buckets,
            "empty_buckets_omitted": len(r["data"]["buckets"]) - len(buckets), **_fresh(r)}


# ---- developments -----------------------------------------------------------------------------------------------

@_tool("Trending developments")
def trending(
        kind: Annotated[Literal["live", "slot"], Field(description="live = what is most reported right now; slot = the top of each recent time slot")] = "live",
        symbol: OneSymbol = None, theme_id: ThemeId = None,
        limit: Annotated[int, Field(ge=1, le=30, description="How many to return, taken from the top of the API's ranking; `total` in the result says how many there are")] = 15) -> dict:
    """The developments most outlets are reporting. Use for "what is moving / what is the big news right now".

    kind=live: the current list. kind=slot: the top stories of each recent time slot, newest slot first. Each
    item's `trending` holds its three newest labels (the live list or a slot's start, the rank and the number
    of sources there); `trending_labels` is how many labels it has in all. The slot list is long (`total`):
    narrow it with symbol or theme_id rather than asking for all of it.
    For a whole day rather than right now, use find_topics with sort=velocity (statements in the last 24 hours).
    Each item has its distinct source count, sentiment counts and newest statement. symbol: one code only.
    """
    _one(symbol, "trending")
    r = _call("trending", kind=kind, symbol=symbol, theme_id=theme_id)
    return {"topics": [_topic(t) for t in r["data"][:limit]], "total": len(r["data"]), **_fresh(r)}


@_tool("Find developments")
def find_topics(
        symbol: Symbols = None, sector: SectorArg = None, theme_id: ThemeId = None, storyline_id: StorylineId = None,
        sort: Literal["recent", "size", "velocity", "oldest"] = "velocity",
        min_statements: Annotated[Optional[int], Field(ge=1, description="Leave out developments with fewer statements. Default: 3, or 1 with storyline_id")] = None,
        limit: PageSize = 15, cursor: Cursor = None) -> dict:
    """List developments for a symbol, sector, theme or storyline. sort=velocity: most statements in the last 24
    hours; size: most statements; recent: latest statement first. symbol: comma-separate several
    (OR). Topics with fewer than 3 statements are left out unless you lower min_statements, so an empty list
    can mean "only small ones": retry with min_statements=1. With storyline_id every topic of the storyline is
    listed by default (sort="oldest" gives them in order)."""
    if min_statements is None:
        min_statements = 1 if storyline_id else 3
    r = _call("topics", symbol=symbol, sector=sector, theme_id=theme_id, storyline_id=storyline_id, sort=sort,
              min_statements=min_statements, limit=limit, cursor=cursor)
    return {"topics": [_topic(t) for t in r["data"]], "next_cursor": r.get("next_cursor"), **_fresh(r)}


@_tool("Get one development")
def get_topic(topic_id: Annotated[str, Field(min_length=1, description="Topic (development) id, as returned in topic_id or topics[].id")]) -> dict:
    """One development in full: its counts, how many of its statements come from news, speculative analysis and
    reddit (source_mix), the storylines it belongs to, and its newest statements, each with its outlet and
    headline. For older statements call search_statements with topic_id and the returned statements_cursor as
    cursor (null when there are no more)."""
    r = _call("topic", topic_id)
    t = r["data"]
    return {**_topic(t), "created_at": t.get("created_at"), "source_mix": t.get("source_mix"),
            "statements_page": [_statement(s) for s in t.get("statements_page") or []],
            "statements_cursor": t.get("statements_cursor"), **_fresh(r)}


# ---- storylines -------------------------------------------------------------------------------------------------

@_tool("One symbol's storylines")
def storylines(
        symbol: Annotated[str, Field(min_length=1, description="Exactly one ticker or entity code, e.g. MSFT")],
        sort: Annotated[Literal["recent", "mentions", "topics", "velocity"], Field(description="recent = newest statement; mentions; topics; velocity = mentions in the last 24 hours")] = "recent",
        min_topics: Annotated[int, Field(ge=1, description="Only storylines with at least this many topics; 1 also lists single-topic ones")] = 2,
        limit: Annotated[int, Field(ge=1, le=25, description="How many to return per call; use the cursor for more")] = 10,
        cursor: Cursor = None) -> dict:
    """One company's news grouped into continuing stories: an event, then its follow-ups, reactions and
    consequences. Use for "how did the X story unfold" or "what are the ongoing stories about X".

    By default only storylines with two or more topics are listed: an empty list means
    "none with several topics", so pass min_topics=1 to see the single-topic ones.
    Each storyline has a name (one of its topics' names), first_seen and last_seen, its number of topics,
    statements and mentions (statements naming the symbol), distinct sources, sentiment counts of the mentions,
    `top_topics` (its largest topics; that is all of them when `topics` is no larger than the list) and `related`:
    storylines of other symbols that share topics with it. Storylines belong to one symbol, so news about two
    companies appears under each. The grouping is automatic and can be wrong or incomplete, and one real story
    can be split over several storylines: a reading aid, not a count of events. Call get_storyline when you
    need every topic in order or the statements.
    """
    _one(symbol, "storylines")
    r = _call("storylines", symbol=symbol, sort=sort, min_topics=min_topics, limit=limit, cursor=cursor)
    return {"storylines": [_storyline(s) for s in r["data"]], "next_cursor": r.get("next_cursor"), **_fresh(r)}


@_tool("Get one storyline")
def get_storyline(
        storyline_id: Annotated[str, Field(min_length=1, description="Storyline id, as returned by `storylines`, `get_symbol` or a topic's storylines")],
        statements: Annotated[int, Field(ge=0, le=20, description="How many of its newest statements to include")] = 8,
        members: Annotated[int, Field(ge=0, le=100, description="How many of its topics to list, oldest first")] = 50) -> dict:
    """One storyline in full: its topics, oldest first (how the story unfolded), and its newest statements.
    If `topics` is larger than the `members` list, ask again with `members` at its maximum; for a storyline
    longer than that, page through all of them with find_topics(storyline_id=..., sort="oldest"). While the
    list is cut, `top_topics` names the storyline's largest topics.
    `more_statements` says whether it has more statements than shown. If you expect to need them, ask for
    `statements` at its maximum: only then is `statements_cursor` set, and
    search_statements(storyline_id=..., cursor=...) continues exactly after them (add symbol to keep only those
    naming the company). With fewer, the cursor is null and search_statements(storyline_id=...) starts again at
    the newest, repeating the ones shown here."""
    r = _call("storyline", storyline_id, _not_found="Its topics may be older than the plan's history.",
              _hint="Storyline ids come from `storylines`, `get_symbol` or a topic's storylines.")
    d = r["data"]
    out = _storyline(d)
    shown = [_member(m) for m in (d.get("members") or [])[:members]]
    if len(shown) >= (d.get("topics") or 0):
        out.pop("top_topics", None)             # every topic is in `members`
    return {**out, "members": shown,
            **_page(d.get("statements_page"), d.get("statements_cursor"), statements), **_fresh(r)}


# ---- themes and reference ---------------------------------------------------------------------------------------

@_tool("Themes")
def themes(symbol: Symbols = None, sector: SectorArg = None,
           sort: Annotated[Literal["size", "topics", "recent", "velocity"], Field(description="size = statements; topics = developments; recent = last statement; velocity = statements in the last 24 hours")] = "size") -> dict:
    """The standing subjects the news is grouped into, each with its number of developments and statements,
    24-hour volume, distinct sources and sentiment counts. With symbol or sector:
    only the themes that have statements about it; the counts are still those of the whole theme, so use
    search_statements with theme_id and symbol for the symbol's own statements. Use the returned id as theme_id
    in the other tools."""
    r = _call("themes", symbol=symbol, sector=sector, sort=sort)
    keep = ("id", "name", "topics", "statements", "statements_24h", "statements_4h", "sources", "sentiment",
            "sentiment_score", "updated_at")
    return {"themes": [{k: t.get(k) for k in keep} for t in r["data"]], **_fresh(r)}


@_tool("History reach, counts and accepted values")
def reference() -> dict:
    """What the API holds right now: how far back the plan's history goes (window_days), counts of statements,
    topics, storylines and symbols, the themes with their ids, and the accepted sector, sentiment, importance,
    source-type and entity-kind values. One call per conversation is enough."""
    r = _call("meta")
    keep = ("window_days", "newest_statement", "statements", "statements_7d", "topics", "topics_7d", "themes",
            "storylines", "storylines_multi_topic", "sources_7d", "symbols_d", "symbols_w", "symbols_window", "enums")
    return {**{k: r["data"][k] for k in keep if k in r["data"]}, **_fresh(r)}


def main() -> None:
    if sys.stdin.isatty():
        print("finance-pulse is an MCP server: it talks to an MCP client (Claude, Cursor, ...) over stdin/stdout.\n"
              "Setup: https://github.com/SlothyAfk/finance-pulse-python#readme", file=sys.stderr)
        sys.exit(2)                     # no MCP client starts a server with a terminal as its stdin
    logging.getLogger("httpx").setLevel(logging.WARNING)    # one INFO line per request otherwise fills client logs
    server.run()


if __name__ == "__main__":
    main()
