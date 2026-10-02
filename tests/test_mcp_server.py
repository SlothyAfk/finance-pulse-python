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
             "image_url": None, "symbols": ["NVDA"], "sectors": ["InformationTechnology"],
              "topic_id": "t1", "topic_name": "Nvidia outlook",
             "theme_id": "th1"}
TOPIC = {"id": "t1", "name": "Nvidia outlook", "theme_id": "th1", "created_at": "2026-09-30T00:00:00Z",
         "updated_at": "2026-10-01T09:32:53Z", "statements": 12, "total_statements": 12, "sources": 7,
         "statements_24h": 5, "statements_4h": 2, "statements_7d": 9, "revision": "r1", "sentiment": {"bullish": 8, "bearish": 1, "neutral": 3},
         "sentiment_score": 0.58, "symbols": ["NVDA"], "sectors": ["InformationTechnology"],
         
         "latest_statement": {"id": "s1", "statement": STATEMENT["statement"], "published_at": STATEMENT["published_at"],
                              "sentiment": "bullish", "source_domain": "example.com", "image_url": None}}


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
        return httpx.Response(200, json=responses[request.url.path])

    monkeypatch.setattr(mcp_server, "_client", FinancePulse("test-key", transport=httpx.MockTransport(handler)))
    return requests


def test_tools_are_registered_with_descriptions():
    tools = asyncio.run(mcp_server.server.list_tools())
    assert {t.name for t in tools} == {"search_statements", "trending", "get_topic", "find_topics",
                                       "sentiment_series", "screen", "themes", "reference"}
    assert all(t.description for t in tools)


def test_search_statements_is_trimmed(api):
    out = mcp_server.search_statements(symbol="NVDA", importance_min="high", limit=5)
    assert api[0].url.params["symbol"] == "NVDA" and api[0].url.params["limit"] == "5"
    assert out["next_cursor"] == "c1" and out["built_at"] == SNAPSHOT["built_at"] and out["lag_days"] == 0.0
    assert out["statements"] == [{
        "id": "s1", "statement": STATEMENT["statement"], "published_at": STATEMENT["published_at"],
        "sentiment": "bullish", "importance": "high", "source_type": "news", "symbols": ["NVDA"],
        "sectors": ["InformationTechnology"],
        "source": "example.com", "url": "https://example.com/a", "topic_id": "t1", "topic": "Nvidia outlook"}]


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
    with pytest.raises(ToolError, match="MONTHLY quota.*rapidapi.com"):
        mcp_server.search_statements(symbol="QUOTA")


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
