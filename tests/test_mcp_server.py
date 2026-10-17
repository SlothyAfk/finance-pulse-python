import asyncio

import httpx
import pytest

from mcp.server.mcpserver.exceptions import ToolError

from finance_pulse import FinancePulse, mcp_server

SNAPSHOT = {"built_at": "2026-10-01T12:00:00+00:00", "anchor": "2026-10-01T12:00:00+00:00",
            "window_start": "2026-07-31T00:00:00+00:00", "lag_days": 0.0}
STATEMENT = {"id": "s1", "statement": "Nvidia expects strong growth in 2027", "published_at": "2026-10-01T09:32:53Z",
             "indexed_at": "2026-10-01T09:49:38Z", "sentiment": "bullish", "importance": "high",
             "source_type": "news", "source_url": "https://example.com/a", "source_domain": "example.com",
             "article_title": "Nvidia sees strong 2027",
             "image_url": None, "symbols": ["NVDA"], "sectors": ["InformationTechnology"],
              "topic_id": "t1", "topic_name": "Nvidia outlook",
             "theme_id": "th1"}
TOPIC = {"id": "t1", "name": "Nvidia outlook", "theme_id": "th1", "created_at": "2026-09-30T00:00:00Z",
         "updated_at": "2026-10-01T09:32:53Z", "statements": 12, "total_statements": 12, "sources": 7,
         "statements_24h": 5, "statements_4h": 2, "statements_7d": 9, "revision": "r1", "sentiment": {"bullish": 8, "bearish": 1, "neutral": 3},
         "sentiment_score": 0.58, "symbols": ["NVDA"], "sectors": ["InformationTechnology"],
         
         "latest_statement": {"id": "s1", "statement": STATEMENT["statement"], "published_at": STATEMENT["published_at"],
                              "sentiment": "bullish", "source_domain": "example.com", "image_url": None}}


ENTITY = {"code": "MSFT", "kind": "equity", "name": "Microsoft Corporation", "exchange": "NASDAQ", "country": "USA",
          "match": None}
MEMBER = {"id": "t1", "name": "US suspends Microsoft from green card program", "statements": 25, "mentions": 24,
          "created_at": "2026-10-08T15:24:25Z", "updated_at": "2026-10-10T14:22:23Z", "placed_at": None}
STORYLINE = {"id": "sl1", "symbol": "MSFT", "entity": ENTITY, "name": MEMBER["name"], "name_topic_id": "t1",
             "first_seen": "2026-10-08T15:23:50Z", "last_seen": "2026-10-10T14:22:23Z", "topics": 9,
             "statements": 53, "mentions": 48, "sources": 41, "mentions_24h": 0, "mentions_7d": 48, "bullish": 0,
             "bearish": 37, "neutral": 11, "top_members": [MEMBER],
             "related": [{"id": "sl2", "symbol": "ADBE", "name": "Adobe suspended too", "topics": 4,
                          "shared_topics": 3}]}
PERIOD = {"mentions": 232, "topics": 106, "sources": 44, "bullish": 128, "bearish": 31, "neutral": 73, "score": 0.4181,
          "mentions_prev_period": 98, "change": 134, "last_seen": "2026-10-12T12:46:00Z", "mentions_single": 207,
          "bullish_single": 124, "bearish_single": 28, "neutral_single": 55, "score_single": 0.4638}
SYMBOL = {"id": "MSFT", "kind": "equity", "name": "Microsoft Corporation", "exchange": "NASDAQ", "country": "USA",
          "aliases": ["MICROSOFT"], "periods": {"d": PERIOD, "w": PERIOD, "window": {**PERIOD, "mentions_prev_period": None, "change": None}},
          "sectors": [{"id": "InformationTechnology", "mentions": 200}],
          "related": [{"id": "ADBE", "kind": "equity", "name": "Adobe Inc", "shared_topics": 7}]}


@pytest.fixture
def api(monkeypatch):
    """Routes the server's client to canned responses; returns the list of requests made."""
    requests = []
    responses = {
        "/v2/statements": {"data": [STATEMENT], "next_cursor": "c1", "snapshot": SNAPSHOT},
        "/v2/trending": {"data": [{**TOPIC, "position": 1, "trending": [{"kind": "live", "rank": 1}]}] * 3,
                         "snapshot": SNAPSHOT},
        "/v2/topics": {"data": [TOPIC], "next_cursor": None, "snapshot": SNAPSHOT},
        "/v2/topics/t1": {"data": {**TOPIC, "source_mix": {"news": 11, "speculative": 1},
                                   "statements_page": [STATEMENT], "statements_cursor": None}, "snapshot": SNAPSHOT},
        "/v2/series": {"data": {"entity": {"type": "symbol", "id": "NVDA", "name": None}, "interval": "day",
                                "buckets": [
                                    {"t": "2026-09-30T00:00:00+00:00", "count": 0, "bullish": 0, "bearish": 0,
                                     "neutral": 0, "by_importance": {}, "sources": 0, "score": None},
                                    {"t": "2026-10-01T00:00:00+00:00", "count": 4, "bullish": 3, "bearish": 1,
                                     "neutral": 0, "by_importance": {}, "sources": 3, "score": 0.5}]},
                       "snapshot": SNAPSHOT},
        "/v2/symbols": {"data": [{"id": "NVDA", "mentions": 35, "change": 14}, {"id": "AMD", "mentions": 9, "change": 2}],
                        "period": "d", "snapshot": SNAPSHOT},
        "/v2/sectors": {"data": [{"id": "Energy", "mentions": 100, "change": 5}], "period": "d", "snapshot": SNAPSHOT},
        "/v2/meta": {"data": {"statements": 75000, "enums": {"sector": ["Energy"]}}, "snapshot": SNAPSHOT},
        "/v2/storylines": {"data": [STORYLINE], "next_cursor": "sc1", "snapshot": SNAPSHOT},
        "/v2/storylines/sl1": {"data": {**STORYLINE, "members": [MEMBER, {**MEMBER, "id": "t2"}],
                                        "statements_page": [STATEMENT] * 3, "statements_cursor": "x1"},
                               "snapshot": SNAPSHOT},
        "/v2/symbols/MSFT": {"data": {**SYMBOL, "top_topics": [TOPIC] * 7, "top_storylines": [STORYLINE],
                                      "statements_page": [STATEMENT] * 20, "statements_cursor": "y1"},
                             "snapshot": SNAPSHOT},
        "/v2/themes": {"data": [{"id": "th1", "name": "Energy", "created_at": "2026-08-01T00:00:00Z",
                                 "updated_at": "2026-10-01T09:00:00Z", "topics": 40, "statements": 900,
                                 "total_topics": 60, "total_statements": 1500, "sources": 80,
                                 "statements_24h": 30, "statements_4h": 4,
                                 "sentiment": {"bullish": 300, "bearish": 400, "neutral": 200},
                                 "sentiment_score": -0.11, "symbols": ["XOM"], "sectors": ["Energy"],
                                 "entities": [{"code": "XOM", "name": "Exxon Mobil", "kind": "equity",
                                               "exchange": "NYSE", "country": "US"}]}],
                       "snapshot": SNAPSHOT},
    }

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/statements" and request.url.params.get("symbol") == "QUOTA":
            return httpx.Response(429, json={"message": "You have exceeded the MONTHLY quota"})
        if request.url.params.get("symbol") == "DOWN":
            raise httpx.ConnectTimeout("timed out")
        if request.url.path == "/v2/symbols/NOPE":
            return httpx.Response(404, json={"title": "Symbol not found", "status": 404,
                                             "detail": "No statement in the window mentions it. See /v2/symbols?q= to look one up."})
        if request.url.path == "/v2/symbols/GOLDMAN%20SACHS" or request.url.path == "/v2/symbols/GOLDMAN SACHS":
            return httpx.Response(400, json={"title": "Invalid symbol", "status": 400,
                                             "detail": "'GOLDMAN SACHS' is not a ticker", "param": "code"})
        if request.url.path == "/v2/symbols" and request.url.params.get("q") == "zzzz":
            return httpx.Response(200, json={"data": [], "period": "window", "snapshot": SNAPSHOT})
        if request.url.path == "/v2/symbols" and request.url.params.get("since"):
            return httpx.Response(200, json={"data": [{"id": "NVDA", "kind": "equity", "name": "NVIDIA Corporation",
                                                       "mentions": 5, "change": None, "score": None,
                                                       "mentions_prev_period": None}],
                                             "period": "custom", "since": "2026-10-01T00:00:00Z",
                                             "until": request.url.params.get("until"),
                                             "snapshot": SNAPSHOT})
        if request.url.path == "/v2/symbols/QUIET":
            return httpx.Response(200, json={"data": {**SYMBOL, "id": "QUIET", "periods": {"window": PERIOD},
                                                      "related": [], "top_topics": [], "top_storylines": [],
                                                      "statements_page": [STATEMENT] * 3, "statements_cursor": None},
                                             "snapshot": SNAPSHOT})
        if request.url.path == "/v2/symbols/MIC":
            return httpx.Response(200, json={"data": {**SYMBOL, "id": "MU", "name": "Micron Technology Inc",
                                                      "top_topics": [], "top_storylines": [],
                                                      "statements_page": [STATEMENT] * 20, "statements_cursor": "y1"},
                                             "snapshot": SNAPSHOT})
        return httpx.Response(200, json=responses[request.url.path])

    monkeypatch.setattr(mcp_server, "_client", FinancePulse("test-key", transport=httpx.MockTransport(handler)))
    return requests


def test_tools_are_registered_with_descriptions():
    tools = asyncio.run(mcp_server.server.list_tools())
    assert {t.name for t in tools} == {"search_statements", "trending", "get_topic", "find_topics",
                                       "sentiment_series", "screen", "themes", "reference", "get_symbol",
                                       "find_symbol", "storylines", "get_storyline"}
    assert all(t.description for t in tools)


def test_search_statements_is_trimmed(api):
    out = mcp_server.search_statements(symbol="NVDA", importance_min="high", limit=5)
    assert api[0].url.params["symbol"] == "NVDA" and api[0].url.params["limit"] == "5"
    assert out["next_cursor"] == "c1" and out["built_at"] == SNAPSHOT["built_at"] and out["lag_days"] == 0.0
    assert out["statements"] == [{
        "id": "s1", "statement": STATEMENT["statement"], "published_at": STATEMENT["published_at"],
        "sentiment": "bullish", "importance": "high", "source_type": "news", "symbols": ["NVDA"],
        "sectors": ["InformationTechnology"],
        "source": "example.com", "url": "https://example.com/a", "headline": "Nvidia sees strong 2027",
        "topic_id": "t1", "topic": "Nvidia outlook"}]


def test_trending_applies_limit_and_keeps_labels(api):
    out = mcp_server.trending(limit=2)
    assert api[0].url.params["kind"] == "live"
    assert len(out["topics"]) == 2 and out["total"] == 3
    assert out["topics"][0]["trending"] == [{"kind": "live", "rank": 1}]
    assert out["topics"][0]["latest_statement"] == STATEMENT["statement"]


def test_get_topic(api):
    out = mcp_server.get_topic("t1")
    assert out["name"] == "Nvidia outlook" and out["source_mix"] == {"news": 11, "speculative": 1}
    assert out["statements_page"][0]["source"] == "example.com"


def test_find_topics_defaults_to_velocity(api):
    mcp_server.find_topics(symbol="NVDA")
    assert api[0].url.params["sort"] == "velocity" and api[0].url.params["min_statements"] == "3"


def test_series_omits_empty_buckets(api):
    out = mcp_server.sentiment_series(symbol="NVDA")
    assert [b["t"] for b in out["buckets"]] == ["2026-10-01T00:00:00+00:00"]
    assert out["empty_buckets_omitted"] == 1 and "by_importance" not in out["buckets"][0]


def test_screen_symbols_and_sectors(api):
    assert mcp_server.screen(limit=1)["rows"] == [{"id": "NVDA", "mentions": 35, "change": 14}]
    assert api[0].url.path == "/v2/symbols" and api[0].url.params["sort"] == "change"
    assert mcp_server.screen(rank="sectors")["rows"][0]["id"] == "Energy"
    assert api[1].url.path == "/v2/sectors"
    mcp_server.screen(entity_kinds="equity,etf", sector="Energy")
    assert api[2].url.params["kind"] == "equity,etf" and api[2].url.params["sector"] == "Energy"
    with pytest.raises(ToolError, match="only apply to rank=symbols"):
        mcp_server.screen(rank="sectors", sector="Energy")


def test_reference(api):
    assert mcp_server.reference()["statements"] == 75000


def test_quota_error_points_to_the_plans(api):
    with pytest.raises(ToolError, match="MONTHLY quota.*will not help.*rapidapi.com") as info:
        mcp_server.search_statements(symbol="QUOTA")
    assert "try again" not in str(info.value).lower()


def test_network_failure_is_explained(api):
    with pytest.raises(ToolError, match="could not be reached.*ConnectTimeout"):
        mcp_server.search_statements(symbol="DOWN")


def test_statement_keeps_entities_when_the_api_sends_them():
    out = mcp_server._statement({**STATEMENT, "entities": [{"code": "NVDA", "kind": "equity", "name": "Nvidia"}]})
    assert out["entities"] == [{"code": "NVDA", "name": "Nvidia", "kind": "equity"}]


def test_themes_filtered_by_symbol(api):
    out = mcp_server.themes(symbol="XOM", sort="velocity")
    assert api[0].url.path == "/v2/themes"
    assert api[0].url.params["symbol"] == "XOM" and api[0].url.params["sort"] == "velocity"
    assert out["themes"] == [{"id": "th1", "name": "Energy", "topics": 40, "statements": 900, "statements_24h": 30,
                              "statements_4h": 4, "sources": 80,
                              "sentiment": {"bullish": 300, "bearish": 400, "neutral": 200},
                              "sentiment_score": -0.11, "updated_at": "2026-10-01T09:00:00Z"}]


def test_sector_values_are_in_the_schema_and_validated(api):
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert "InformationTechnology" in str(tools["search_statements"].input_schema["properties"]["sector"])
    assert all(t.annotations.read_only_hint for t in tools.values())
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.server.call_tool("find_topics", {"sector": "Technology"}))
    assert api == []                         # rejected before any request is spent


def test_missing_key_is_explained(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client", None)
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    with pytest.raises(ToolError, match="RAPIDAPI_KEY is not set for the finance-pulse server"):
        mcp_server.reference()


def test_call_through_the_server(api):
    result = asyncio.run(mcp_server.server.call_tool("screen", {"rank": "sectors"}))
    assert "Energy" in str(result)


def test_error_text_reaches_the_model(api):
    with pytest.raises(ToolError, match="quota.*rapidapi.com"):
        asyncio.run(mcp_server.server.call_tool("search_statements", {"symbol": "QUOTA"}))


def test_series_needs_exactly_one_entity_before_any_request(api):
    with pytest.raises(ToolError, match="exactly one"):
        mcp_server.sentiment_series()
    with pytest.raises(ToolError, match="exactly one"):
        mcp_server.sentiment_series(symbol="NVDA", sector="Energy")
    assert api == []


def test_versions_agree():
    import json
    import pathlib

    import finance_pulse
    spec = json.loads((pathlib.Path(__file__).parent.parent / "server.json").read_text())
    assert spec["version"] == spec["packages"][0]["version"] == finance_pulse.__version__ == mcp_server.server.version


def test_get_symbol_is_one_request_and_trimmed(api):
    out = mcp_server.get_symbol("MSFT", statements=2, topics=3)
    assert [r.url.path for r in api] == ["/v2/symbols/MSFT"]
    assert (out["id"], out["name"], out["kind"], out["exchange"]) == ("MSFT", "Microsoft Corporation", "equity", "NASDAQ")
    assert out["periods"]["w"] == {"mentions": 232, "topics": 106, "sources": 44, "bullish": 128, "bearish": 31,
                                   "neutral": 73, "score": 0.4181, "change": 134, "mentions_single": 207,
                                   "score_single": 0.4638, "last_seen": "2026-10-12T12:46:00Z"}
    assert out["periods"]["window"]["change"] is None
    assert len(out["top_topics"]) == 3 and len(out["statements_page"]) == 2
    # the page was cut from 20 to 2: the API's cursor would skip 18 statements, so it is not passed on
    assert out["statements_cursor"] is None and out["more_statements"] is True
    assert out["top_storylines"] == [{"id": "sl1", "name": MEMBER["name"], "topics": 9, "mentions": 48,
                                      "mentions_7d": 48, "last_seen": "2026-10-10T14:22:23Z"}]
    assert out["related"] == [{"id": "ADBE", "name": "Adobe Inc", "kind": "equity", "shared_topics": 7}]
    assert out["sectors"] == [{"id": "InformationTechnology", "mentions": 200}]


def test_find_symbol_looks_up_by_name(api):
    out = mcp_server.find_symbol("micron", limit=1)
    params = api[0].url.params
    assert api[0].url.path == "/v2/symbols"
    assert (params["q"], params["period"], params["min_mentions"], params["limit"]) == ("micron", "window", "1", "1")
    assert out["matches"] == [{"id": "NVDA", "name": None, "kind": None, "mentions": 35, "last_seen": None}]


def test_storylines_for_one_symbol(api):
    out = mcp_server.storylines("MSFT", sort="mentions")
    params = api[0].url.params
    assert (params["symbol"], params["sort"], params["min_topics"], params["limit"]) == ("MSFT", "mentions", "2", "10")
    line = out["storylines"][0]
    assert (line["id"], line["symbol"], line["topics"], line["mentions"], line["bearish"]) == ("sl1", "MSFT", 9, 48, 37)
    assert line["related"] == [{"id": "sl2", "symbol": "ADBE", "name": "Adobe suspended too", "shared_topics": 3}]
    assert line["top_topics"] == [{"id": "t1", "name": MEMBER["name"], "created_at": MEMBER["created_at"]}]
    assert "top_members" not in line and "entity" not in line
    assert out["next_cursor"] == "sc1"


def test_get_storyline_lists_every_topic_in_order(api):
    out = mcp_server.get_storyline("sl1", statements=1)
    assert api[0].url.path == "/v2/storylines/sl1"
    assert [m["id"] for m in out["members"]] == ["t1", "t2"]
    assert out["members"][0] == {"id": "t1", "name": MEMBER["name"], "created_at": MEMBER["created_at"],
                                 "updated_at": MEMBER["updated_at"], "statements": 25, "mentions": 24}
    assert len(out["statements_page"]) == 1 and out["statements_cursor"] is None and out["more_statements"] is True
    # 2 of its 9 topics are listed, so the largest ones are still named
    assert out["top_topics"] == [{"id": "t1", "name": MEMBER["name"], "created_at": MEMBER["created_at"]}]
    assert out["statements_page"][0]["headline"] == "Nvidia sees strong 2027"


def test_storyline_id_filters(api):
    mcp_server.search_statements(storyline_id="sl1", symbol="MSFT")
    mcp_server.find_topics(storyline_id="sl1", min_statements=1)
    assert api[0].url.params["storyline_id"] == "sl1" and api[0].url.params["symbol"] == "MSFT"
    assert api[1].url.path == "/v2/topics" and api[1].url.params["storyline_id"] == "sl1"


def test_topic_storylines_are_kept_only_when_there_are_some():
    with_lines = mcp_server._topic({**TOPIC, "storylines": [{"id": "sl1", "symbol": "NVDA", "topics": 3, "placed_at": None}]})
    assert with_lines["storylines"] == [{"id": "sl1", "symbol": "NVDA", "topics": 3}]
    assert "storylines" not in mcp_server._topic({**TOPIC, "storylines": []})
    assert "storylines" not in mcp_server._topic({**TOPIC, "storylines": None})


def test_screen_custom_interval(api):
    out = mcp_server.screen(since="2026-10-01T00:00:00Z", until="2026-10-08T00:00:00Z", sort="mentions")
    assert api[0].url.params["since"] == "2026-10-01T00:00:00Z" and api[0].url.params["until"] == "2026-10-08T00:00:00Z"
    # the interval as the API echoes it; change and score stay visible when null, other null fields are dropped
    assert (out["period"], out["since"], out["until"]) == ("custom", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z")
    assert out["rows"] == [{"id": "NVDA", "kind": "equity", "name": "NVIDIA Corporation", "mentions": 5,
                            "change": None, "score": None}]
    mcp_server.screen(rank="sectors", since="2026-10-01T00:00:00Z")
    assert api[1].url.path == "/v2/sectors" and api[1].url.params["since"] == "2026-10-01T00:00:00Z"


def test_public_texts_describe_the_data_not_the_service():
    """Texts say what the user gets, not how or how fast it is produced, so an API-side change needs no release."""
    import pathlib
    import re
    root = pathlib.Path(__file__).parent.parent
    tools = asyncio.run(mcp_server.server.list_tools())
    text = mcp_server.INSTRUCTIONS + " ".join((t.description or "") + str(t.input_schema) for t in tools)
    text += (root / "README.md").read_text().split("## Development")[0]
    text += (root / "src/finance_pulse/client.py").read_text() + (root / "src/finance_pulse/mcp_server.py").read_text()
    text += (root / "pyproject.toml").read_text() + (root / "server.json").read_text()
    text = re.sub(r"\s+", " ", text)                          # docstrings wrap: match across line breaks
    for banned in (r"every (few|\d[\d.]*) (seconds|minutes|hours)", r"\b\d+ themes\b", r"\b\d+[- ]days? window",
                   r"rolling \d+", r"window of \d+", r"rebuil[dt]", r"build interval", r"\bincidents?\b",
                   r"language model"):
        assert not re.search(banned, text, re.I), banned


def test_get_symbol_keeps_the_cursor_when_nothing_was_cut(api):
    out = mcp_server.get_symbol("MIC", statements=20)        # an alias: the answer is the code it stands for
    assert out["id"] == "MU" and len(out["statements_page"]) == 20
    assert out["statements_cursor"] == "y1" and out["more_statements"] is True


def test_get_symbol_for_a_rarely_mentioned_symbol(api):
    out = mcp_server.get_symbol("QUIET")
    assert list(out["periods"]) == ["window"] and out["related"] == [] and out["top_topics"] == []
    assert len(out["statements_page"]) == 3 and out["statements_cursor"] is None and out["more_statements"] is False


def test_get_symbol_errors_point_to_find_symbol(api):
    with pytest.raises(ToolError, match="Symbol not found. No statement within the plan.s history mentions this code. If the code "
                                        "itself is in doubt, look it up with find_symbol") as info:
        mcp_server.get_symbol("NOPE")
    assert "/v2/" not in str(info.value)                     # no API path in what the model reads
    with pytest.raises(ToolError, match="Invalid symbol: 'GOLDMAN SACHS' is not a ticker. If the code itself is in doubt"):
        mcp_server.get_symbol("GOLDMAN SACHS")
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.server.call_tool("get_symbol", {"code": ""}))
    assert len(api) == 2                                     # the empty code never became a request


def test_find_symbol_with_no_match(api):
    assert mcp_server.find_symbol("zzzz")["matches"] == []


def test_find_topics_lists_every_topic_of_a_storyline_by_default(api):
    mcp_server.find_topics(storyline_id="sl1")
    mcp_server.find_topics(symbol="NVDA")
    mcp_server.find_topics(storyline_id="sl1", min_statements=5)
    assert [r.url.params["min_statements"] for r in api] == ["1", "3", "5"]


def test_topic_rows_carry_the_newest_statements_link_and_time():
    row = mcp_server._topic({**TOPIC, "latest_statement": {**TOPIC["latest_statement"], "source_url": "https://example.com/a",
                                                           "article_title": "Nvidia sees strong 2027"}})
    assert (row["latest_url"], row["latest_headline"], row["latest_published_at"]) == (
        "https://example.com/a", "Nvidia sees strong 2027", STATEMENT["published_at"])


def test_transient_error_uses_retry_after(monkeypatch):
    def handler(request):
        return httpx.Response(503, headers={"retry-after": "60"}, json={"title": "Temporarily unavailable", "status": 503})

    monkeypatch.setattr(mcp_server, "_client", FinancePulse("test-key", transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="Try again in 60 seconds"):
        mcp_server.reference()


def with_answer(monkeypatch, answer):
    monkeypatch.setattr(mcp_server, "_client", FinancePulse("test-key", transport=httpx.MockTransport(lambda r: answer(r))))


def test_error_texts_for_gateway_answers(monkeypatch):
    with_answer(monkeypatch, lambda r: httpx.Response(500, text="oops"))
    with pytest.raises(ToolError, match="temporarily unavailable.*Try again shortly"):
        mcp_server.reference()
    with_answer(monkeypatch, lambda r: httpx.Response(503, headers={"retry-after": "0.4"}, json={"title": "x"}))
    with pytest.raises(ToolError, match="Try again shortly"):
        mcp_server.reference()
    for status, message in ((401, "Invalid API key"), (403, "You are not subscribed to this API.")):
        with_answer(monkeypatch, lambda r, s=status, m=message: httpx.Response(s, json={"message": m}))
        with pytest.raises(ToolError, match=f"RAPIDAPI_KEY is missing, wrong or not subscribed.*{message[:12]}.*free plan"):
            mcp_server.reference()
    with_answer(monkeypatch, lambda r: httpx.Response(429, headers={"retry-after": "2"}, json={"message": "Too many requests"}))
    with pytest.raises(ToolError, match="rate limit was reached.*Too many requests.*usually passes: try again in 2 seconds, one call") as info:
        mcp_server.reference()
    assert "quota" not in str(info.value)


def test_page_passes_the_cursor_on_only_when_nothing_was_cut():
    rows = [{**STATEMENT, "id": f"s{i}"} for i in range(20)]
    assert mcp_server._page(rows, "c", 0) == {"statements_page": [], "statements_cursor": None, "more_statements": True}
    full = mcp_server._page(rows, "c", 20)
    assert len(full["statements_page"]) == 20 and full["statements_cursor"] == "c" and full["more_statements"] is True
    last = mcp_server._page(rows, None, 20)                   # exactly one page, nothing after it
    assert last["statements_cursor"] is None and last["more_statements"] is False
    assert mcp_server._page(None, None, 8) == {"statements_page": [], "statements_cursor": None, "more_statements": False}
    assert mcp_server._page(rows[:5], None, 5)["more_statements"] is False


def test_continuation_delivers_every_statement_once(monkeypatch):
    """get_symbol with the full page, then search_statements with its cursor: no gap, no repeat."""
    ids = [f"s{i:02d}" for i in range(47)]

    def answer(request):
        if request.url.path == "/v2/symbols/BIG":
            return httpx.Response(200, json={"data": {**SYMBOL, "id": "BIG", "top_topics": [], "top_storylines": [],
                                                      "statements_page": [{**STATEMENT, "id": i} for i in ids[:20]],
                                                      "statements_cursor": "20"}, "snapshot": SNAPSHOT})
        start, limit = int(request.url.params.get("cursor", 0)), int(request.url.params["limit"])
        page = ids[start:start + limit]
        return httpx.Response(200, json={"data": [{**STATEMENT, "id": i} for i in page], "snapshot": SNAPSHOT,
                                         "next_cursor": str(start + limit) if start + limit < len(ids) else None})

    with_answer(monkeypatch, answer)
    first = mcp_server.get_symbol("BIG", statements=20)
    got, cursor = [s["id"] for s in first["statements_page"]], first["statements_cursor"]
    while cursor:
        page = mcp_server.search_statements(symbol="BIG", cursor=cursor, limit=15)
        got += [s["id"] for s in page["statements"]]
        cursor = page["next_cursor"]
    assert got == ids


def test_get_storyline_answers_under_the_id_it_was_absorbed_into(monkeypatch):
    def answer(request):
        if request.url.path == "/v2/storylines/old":
            return httpx.Response(308, headers={"location": "/v2/storylines/sl1"})
        return httpx.Response(200, json={"data": {**STORYLINE, "members": [MEMBER], "statements_page": [],
                                                  "statements_cursor": None}, "snapshot": SNAPSHOT})

    with_answer(monkeypatch, answer)
    assert mcp_server.get_storyline("old")["id"] == "sl1"


def test_trending_slot_and_size_limits(api):
    mcp_server.trending(kind="slot", symbol="NVDA")
    assert api[0].url.params["kind"] == "slot" and api[0].url.params["symbol"] == "NVDA"
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    largest = {name: tools[name].input_schema["properties"]["limit"]["maximum"]
               for name in ("trending", "screen", "search_statements", "find_topics", "storylines")}
    assert largest == {"trending": 30, "screen": 100, "search_statements": 50, "find_topics": 50, "storylines": 25}


def test_empty_ids_never_become_a_request(api):
    for tool, args in (("get_topic", {"topic_id": ""}), ("get_storyline", {"storyline_id": ""}),
                       ("storylines", {"symbol": ""}), ("get_symbol", {"code": ""})):
        with pytest.raises(ToolError):
            asyncio.run(mcp_server.server.call_tool(tool, args))
    with pytest.raises(ToolError, match="is not an id"):
        mcp_server.get_symbol(".")
    with pytest.raises(ToolError, match="is not an id"):
        mcp_server.get_topic("..")
    assert api == []


CAPTURES = __import__("pathlib").Path(__file__).parent / "captures"


@pytest.mark.parametrize("tool,args,capture", [
    ("search_statements", {"symbol": "NVDA"}, "statements"), ("trending", {}, "trending"),
    ("find_topics", {}, "topics"), ("get_topic", {"topic_id": "x"}, "topic"),
    ("storylines", {"symbol": "MSFT"}, "storylines"), ("get_storyline", {"storyline_id": "x"}, "storyline"),
    ("sentiment_series", {"symbol": "NVDA"}, "series"), ("screen", {}, "symbols"),
    ("screen", {"rank": "sectors"}, "sectors"), ("themes", {}, "themes"), ("reference", {}, "meta")])
def test_tools_on_real_responses(monkeypatch, tool, args, capture):
    """Responses recorded from the API (trimmed to a few rows): every tool must read them without error."""
    import json
    body = json.loads((CAPTURES / f"{capture}.json").read_text())
    monkeypatch.setattr(mcp_server, "_client",
                        FinancePulse("test-key", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    out = getattr(mcp_server, tool)(**args)
    json.dumps(out)
    assert out["lag_days"] is not None and out["window_start"]


def test_the_model_receives_compact_json(api):
    import json
    result = asyncio.run(mcp_server.server.call_tool("screen", {"rank": "sectors"}))
    text = result.content[0].text if hasattr(result, "content") else result[0].text
    assert "\n" not in text and '", "' not in text and '": ' not in text
    assert json.loads(text)["rows"][0]["id"] == "Energy"


def test_non_ascii_text_reaches_the_model_unescaped(monkeypatch):
    statement = {**STATEMENT, "statement": "Société Générale relève son objectif à 12 €", "article_title": "日本語"}
    with_answer(monkeypatch, lambda r: httpx.Response(200, json={"data": [statement], "next_cursor": None, "snapshot": SNAPSHOT}))
    result = asyncio.run(mcp_server.server.call_tool("search_statements", {"symbol": "GLE.PA"}))
    text = result.content[0].text if hasattr(result, "content") else result[0].text
    assert "Société Générale relève son objectif à 12 €" in text and "日本語" in text


def test_reference_passes_on_only_what_its_description_names(monkeypatch):
    import json
    body = json.loads((CAPTURES / "meta.json").read_text())
    assert "storyline_method" in body["data"]                # the API sends more than the tool passes on
    with_answer(monkeypatch, lambda r: httpx.Response(200, json=body))
    out = mcp_server.reference()
    assert set(out) == {"window_days", "newest_statement", "statements", "statements_7d", "topics", "topics_7d",
                        "themes", "storylines", "storylines_multi_topic", "sources_7d", "symbols_d", "symbols_w",
                        "symbols_window", "enums", "built_at", "window_start", "lag_days"}
    assert isinstance(out["lag_days"], float)


def test_one_symbol_tools_refuse_a_list_before_any_request(api):
    for call in (lambda: mcp_server.sentiment_series(symbol="NVDA,AMD"), lambda: mcp_server.trending(symbol="NVDA,AMD"),
                 lambda: mcp_server.storylines("NVDA,AMD")):
        with pytest.raises(ToolError, match="takes one symbol"):
            call()
    assert api == []


def test_trending_labels_and_storyline_members_are_capped(monkeypatch):
    labels = [{"kind": "slot", "slot_start": f"2026-10-0{d}T00:00:00Z", "rank": d, "sources": 5, "statements": 9} for d in range(1, 9)]
    row = mcp_server._topic({**TOPIC, "trending": labels})
    assert row["trending"] == labels[:3] and row["trending_labels"] == 8
    with_answer(monkeypatch, lambda r: httpx.Response(200, json={
        "data": {**STORYLINE, "topics": 120, "members": [{**MEMBER, "id": f"t{i}"} for i in range(120)],
                 "statements_page": [], "statements_cursor": None}, "snapshot": SNAPSHOT}))
    cut = mcp_server.get_storyline("sl1")
    assert len(cut["members"]) == 50 and cut["topics"] == 120 and "top_topics" in cut      # cut: largest still named
    most = mcp_server.get_storyline("sl1", members=100)
    assert [m["id"] for m in most["members"]] == [f"t{i}" for i in range(100)]
    none = mcp_server.get_storyline("sl1", members=0)
    assert none["members"] == [] and none["topics"] == 120 and none["top_topics"]


def test_get_storyline_with_every_topic_listed_drops_the_duplicate_list(monkeypatch):
    with_answer(monkeypatch, lambda r: httpx.Response(200, json={
        "data": {**STORYLINE, "topics": 2, "members": [MEMBER, {**MEMBER, "id": "t2"}], "statements_page": [],
                 "statements_cursor": None}, "snapshot": SNAPSHOT}))
    out = mcp_server.get_storyline("sl1")
    assert len(out["members"]) == 2 and "top_topics" not in out


def test_get_storyline_404_says_why_and_where_ids_come_from(monkeypatch):
    with_answer(monkeypatch, lambda r: httpx.Response(404, json={"title": "Storyline not found", "status": 404,
                                                                 "detail": "Its topics may be older than your plan's history."}))
    with pytest.raises(ToolError, match="Storyline not found. Its topics may be older than the plan's history. "
                                        "Storyline ids come from"):
        mcp_server.get_storyline("gone")


@pytest.mark.parametrize("tool,args,capture,rows", [
    ("trending", {"kind": "slot", "limit": 30}, "trending", 600), ("search_statements", {"limit": 50}, "statements", 50),
    ("find_topics", {"limit": 50}, "topics", 50), ("storylines", {"symbol": "MSFT", "limit": 25}, "storylines", 25),
    ("screen", {"limit": 100}, "symbols", 100)])
def test_largest_list_results_stay_readable(monkeypatch, tool, args, capture, rows):
    """Real rows repeated up to each tool's maximum: the text the model receives must stay well within what a
    client accepts as one tool result."""
    import itertools
    import json
    body = json.loads((CAPTURES / f"{capture}.json").read_text())
    body["data"] = list(itertools.islice(itertools.cycle(body["data"]), rows))
    with_answer(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = asyncio.run(mcp_server.server.call_tool(tool, args))
    text = result.content[0].text if hasattr(result, "content") else result[0].text
    assert len(text) < 45_000, len(text)


def test_largest_storyline_result_stays_readable(monkeypatch):
    import itertools
    import json
    body = json.loads((CAPTURES / "storyline.json").read_text())
    data = body["data"]
    data["members"] = list(itertools.islice(itertools.cycle(data["members"]), 200))
    data["statements_page"] = list(itertools.islice(itertools.cycle(data["statements_page"]), 20))
    data["topics"] = 200
    with_answer(monkeypatch, lambda r: httpx.Response(200, json=body))
    result = asyncio.run(mcp_server.server.call_tool("get_storyline", {"storyline_id": "x", "members": 100, "statements": 20}))
    text = result.content[0].text if hasattr(result, "content") else result[0].text
    assert len(text) < 45_000, len(text)
    assert len(json.loads(text)["members"]) == 100
