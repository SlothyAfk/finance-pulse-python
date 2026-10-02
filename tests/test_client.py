import json

import httpx
import pytest

from finance_pulse import Backend, FinancePulse, FinancePulseError

SNAPSHOT = {"built_at": "2026-10-01T12:00:00+00:00", "anchor": "2026-10-01T12:00:00+00:00",
            "window_start": "2026-07-31T00:00:00+00:00", "lag_days": 0.0}


def make(handler, **kwargs):
    return FinancePulse("test-key", transport=httpx.MockTransport(handler), **kwargs)


def test_requires_a_key(monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    with pytest.raises(ValueError, match="RAPIDAPI_KEY"):
        FinancePulse()


def test_key_from_environment_and_headers(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "env-key")
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"data": {}, "snapshot": SNAPSHOT})

    FinancePulse(transport=httpx.MockTransport(handler)).meta()
    assert seen["x-rapidapi-key"] == "env-key"
    assert seen["x-rapidapi-host"] == "finance-pulse.p.rapidapi.com"


def test_statements_joins_lists_and_drops_none():
    def handler(request):
        assert request.url.path == "/v2/statements"
        assert request.url.params.get_list("symbol") == ["NVDA,AMD"]
        assert request.url.params["has_image"] == "true"
        assert request.url.params["limit"] == "5"
        assert "sector" not in request.url.params and "cursor" not in request.url.params
        return httpx.Response(200, json={"data": [], "next_cursor": None, "snapshot": SNAPSHOT})

    make(handler).statements(symbol=["NVDA", "AMD"], has_image=True, limit=5)


def test_iter_statements_follows_cursors_and_stops_at_max_items():
    calls = []

    def handler(request):
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        start = {None: 0, "c1": 2, "c2": 4}[cursor]
        return httpx.Response(200, json={"data": [{"id": str(start)}, {"id": str(start + 1)}],
                                         "next_cursor": {None: "c1", "c1": "c2", "c2": None}[cursor],
                                         "snapshot": SNAPSHOT})

    fp = make(handler)
    assert [s["id"] for s in fp.iter_statements(symbol="NVDA", max_items=3)] == ["0", "1", "2"]
    assert calls == [None, "c1"]
    calls.clear()
    assert len(list(fp.iter_statements(max_items=None))) == 6
    assert calls == [None, "c1", "c2"]


def test_problem_json_becomes_an_error():
    def handler(request):
        return httpx.Response(400, json={"type": "about:blank", "title": "Invalid symbol", "status": 400,
                                         "detail": "'S&P' is not a ticker", "param": "symbol"})

    with pytest.raises(FinancePulseError) as info:
        make(handler).statements(symbol="S&P")
    assert (info.value.status, info.value.title, info.value.param) == (400, "Invalid symbol", "symbol")
    assert "not a ticker" in str(info.value)


def test_rapidapi_gateway_error_message():
    def handler(request):
        return httpx.Response(429, json={"message": "You have exceeded the MONTHLY quota for Requests on your current plan"})

    with pytest.raises(FinancePulseError) as info:
        make(handler).meta()
    assert info.value.status == 429 and "MONTHLY quota" in info.value.title


def test_non_json_error():
    with pytest.raises(FinancePulseError) as info:
        make(lambda request: httpx.Response(502, text="Bad Gateway")).meta()
    assert info.value.status == 502


def test_topics_joins_ids():
    def handler(request):
        assert request.url.params["ids"] == "a,b"
        return httpx.Response(200, json={"data": [], "next_cursor": None, "snapshot": SNAPSHOT})

    make(handler).topics(ids=["a", "b"])


def test_topics_takes_one_id_as_a_string():
    def handler(request):
        assert request.url.params["ids"] == "abc-def"
        return httpx.Response(200, json={"data": [], "next_cursor": None, "snapshot": SNAPSHOT})

    make(handler).topics(ids="abc-def")


def test_symbols_kind():
    def handler(request):
        assert request.url.params["kind"] == "equity,etf"
        return httpx.Response(200, json={"data": [], "period": "d", "snapshot": SNAPSHOT})

    make(handler).symbols(period="d", kind=["equity", "etf"])


def test_ids_in_the_path_are_quoted():
    paths = []

    def handler(request):
        paths.append(request.url.raw_path.decode())
        return httpx.Response(404, json={"title": "Topic not found", "status": 404})

    fp = make(handler)
    for bad in ("x?limit=1", "../meta"):
        with pytest.raises(FinancePulseError):
            fp.topic(bad)
    assert paths == ["/v2/topics/x%3Flimit%3D1", "/v2/topics/..%2Fmeta"]


def test_network_failure_is_a_finance_pulse_error():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(FinancePulseError) as info:
        make(handler).meta()
    assert info.value.status == 0 and info.value.transient and "ConnectTimeout" in str(info.value)


def test_success_with_a_non_json_body_is_an_error():
    with pytest.raises(FinancePulseError, match="Unexpected response"):
        make(lambda request: httpx.Response(200, text="<html>maintenance</html>")).meta()


def test_merged_theme_redirect_is_followed_with_the_key():
    def handler(request):
        assert request.headers["x-rapidapi-key"] == "test-key"
        if request.url.path == "/v2/themes/old":
            return httpx.Response(308, headers={"location": "/v2/themes/new"})
        return httpx.Response(200, json={"data": {"id": "new"}, "snapshot": SNAPSHOT})

    assert make(handler).theme("old")["data"]["id"] == "new"


def test_retry_after_and_transient():
    def handler(request):
        return httpx.Response(503, headers={"retry-after": "60"}, json={"title": "Snapshot upgrading", "status": 503})

    with pytest.raises(FinancePulseError) as info:
        make(handler).meta()
    assert info.value.transient and info.value.retry_after == 60


def test_poll_feed_persists_the_cursor_and_resumes(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr("finance_pulse.client.time.sleep", slept.append)
    pages = {None: (["s1", "s2"], "c1"), "c1": (["s3"], "c2"), "c2": ([], "c2")}
    calls = []

    def handler(request):
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        ids, nxt = pages[cursor]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids], "next_cursor": nxt, "snapshot": SNAPSHOT})

    path = tmp_path / "cursor.json"
    fp = make(handler)
    got = [s["id"] for s in fp.poll_feed(cursor_file=path, limit=2, max_polls=2, symbol="NVDA")]
    assert got == ["s1", "s2", "s3"]
    assert calls == [None, "c1"]
    assert slept == []                       # a full page is followed at once; the last poll does not sleep
    assert json.loads(path.read_text()) == {"cursor": "c2", "handled": []}

    calls.clear()
    assert list(fp.poll_feed(cursor_file=path, limit=2, max_polls=2, interval=7)) == []
    assert calls == ["c2", "c2"]             # resumed from the file
    assert slept == [7]


def test_another_backend_changes_only_url_and_auth(monkeypatch):
    direct = Backend(base_url="https://api.example.com", key_header="Authorization",
                     key_env="FINANCE_PULSE_KEY", signup_url="https://example.com/signup", key_prefix="Bearer ")
    monkeypatch.delenv("FINANCE_PULSE_KEY", raising=False)
    with pytest.raises(ValueError, match="FINANCE_PULSE_KEY.*example.com/signup"):
        FinancePulse(backend=direct)

    seen = {}

    def handler(request):
        seen["url"], seen["headers"] = str(request.url), dict(request.headers)
        return httpx.Response(200, json={"data": {}, "snapshot": SNAPSHOT})

    monkeypatch.setenv("FINANCE_PULSE_KEY", "k1")
    FinancePulse(backend=direct, transport=httpx.MockTransport(handler)).meta()
    assert seen["url"] == "https://api.example.com/v2/meta"
    assert seen["headers"]["authorization"] == "Bearer k1"
    assert "x-rapidapi-key" not in seen["headers"] and "x-rapidapi-host" not in seen["headers"]


def feed_handler(pages, calls):
    def handler(request):
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        ids, nxt = pages[cursor]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids], "next_cursor": nxt, "snapshot": SNAPSHOT})
    return handler


def test_poll_feed_resumes_mid_page_without_repeating_handled_statements(tmp_path, monkeypatch):
    monkeypatch.setattr("finance_pulse.client.time.sleep", lambda s: None)
    pages = {None: (["s1", "s2", "s3"], "c1"), "c1": (["s4"], "c2")}
    path = tmp_path / "cursor.json"
    fp = make(feed_handler(pages, []))

    got = []
    for s in fp.poll_feed(cursor_file=path, limit=3, max_polls=2):
        got.append(s["id"])
        if s["id"] == "s2":
            break                            # stopped while working on s2: s1 is handled, s2 is not
    assert got == ["s1", "s2"]
    assert json.loads(path.read_text()) == {"cursor": None, "handled": ["s1"]}

    assert [s["id"] for s in fp.poll_feed(cursor_file=path, limit=3, max_polls=2)] == ["s2", "s3", "s4"]
    assert json.loads(path.read_text()) == {"cursor": "c2", "handled": []}


def test_poll_feed_retries_temporary_failures_and_raises_the_rest(monkeypatch):
    slept = []
    monkeypatch.setattr("finance_pulse.client.time.sleep", slept.append)
    answers = iter([httpx.Response(503, headers={"retry-after": "60"}, json={"title": "Snapshot upgrading"}),
                    httpx.ConnectError("down"),
                    httpx.Response(200, json={"data": [{"id": "s1"}], "next_cursor": "c1", "snapshot": SNAPSHOT}),
                    httpx.Response(429, json={"message": "quota"})])

    def handler(request):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    feed = make(handler).poll_feed(interval=0, max_polls=2)
    assert next(feed)["id"] == "s1"
    assert slept == [60, 20]                 # Retry-After, then the growing pause (not capped by interval)
    with pytest.raises(FinancePulseError) as info:
        next(feed)
    assert info.value.status == 429


def test_poll_feed_rejects_a_corrupt_cursor_file(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text("")
    with pytest.raises(ValueError, match="not a poll_feed cursor file"):
        next(make(lambda request: httpx.Response(500)).poll_feed(cursor_file=path))


def test_poll_feed_keeps_handled_ids_when_the_refetched_page_changed(tmp_path, monkeypatch):
    monkeypatch.setattr("finance_pulse.client.time.sleep", lambda s: None)
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({"cursor": "k", "handled": ["a", "b", "c"]}))
    pages = {"k": (["a", "X", "b", "c", "d"], "k2")}
    fp = make(feed_handler(pages, []))

    feed = fp.poll_feed(cursor_file=path, limit=5, max_polls=1)
    assert next(feed)["id"] == "X"
    feed.close()                             # stopped while working on X
    assert set(json.loads(path.read_text())["handled"]) == {"a", "b", "c"}
    assert [s["id"] for s in fp.poll_feed(cursor_file=path, limit=5, max_polls=1)] == ["X", "d"]


def test_poll_feed_checks_the_cursor_file_before_the_first_request(tmp_path):
    calls = []
    fp = make(feed_handler({None: (["a"], "c1")}, calls))
    with pytest.raises(ValueError, match="directory of cursor_file"):
        next(fp.poll_feed(cursor_file=tmp_path / "nodir" / "c.json"))
    assert calls == []


@pytest.mark.parametrize("key", ["abc\n", " abc ", "abc\r\n"])
def test_key_whitespace_is_stripped(key):
    seen = {}

    def handler(request):
        seen["key"] = request.headers["x-rapidapi-key"]
        return httpx.Response(200, json={"data": {}, "snapshot": SNAPSHOT})

    FinancePulse(key, transport=httpx.MockTransport(handler)).meta()
    assert seen["key"] == "abc"


def test_key_with_impossible_characters_is_rejected():
    with pytest.raises(ValueError, match="characters a key cannot have"):
        FinancePulse("ab\u00e9c")
    with pytest.raises(ValueError, match="characters a key cannot have"):
        FinancePulse("ab\ncd")


def test_redirect_to_another_host_is_refused_and_the_key_stays_home():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(308, headers={"location": "https://evil.example/x"})

    with pytest.raises(FinancePulseError, match="another origin refused: https://evil.example"):
        make(handler).theme("old")
    assert hosts == ["finance-pulse.p.rapidapi.com"]


def test_redirect_loop_ends():
    with pytest.raises(FinancePulseError):
        make(lambda request: httpx.Response(308, headers={"location": "/v2/themes/a"})).theme("a")


def test_redirect_without_a_location_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(308)

    with pytest.raises(FinancePulseError):
        make(handler).theme("a")
    assert len(calls) == 1


def test_get_refuses_absolute_urls():
    fp = make(lambda request: httpx.Response(200, json={"data": {}}))
    for bad in ("https://evil.example/x", "//evil.example/x", "v2/meta"):
        with pytest.raises(ValueError, match="single '/'"):
            fp.get(bad)


def test_success_without_the_envelope_is_an_error():
    with pytest.raises(FinancePulseError, match="Unexpected response"):
        make(lambda request: httpx.Response(200, json={"message": "odd"})).meta()


def test_has_image_false_is_sent():
    def handler(request):
        assert request.url.params["has_image"] == "false"
        return httpx.Response(200, json={"data": [], "next_cursor": None, "snapshot": SNAPSHOT})

    make(handler).statements(has_image=False)


@pytest.mark.parametrize("header,expected", [("1.5", 1.5), ("0", 0.0), ("Wed, 21 Oct 2026 07:28:00 GMT", None),
                                              ("\u00b2", None), ("-1", 0.0), ("inf", None), ("1e400", None)])
def test_retry_after_parsing(header, expected):
    def handler(request):
        return httpx.Response(503, headers=[(b"retry-after", header.encode())], json={"title": "x"})

    with pytest.raises(FinancePulseError) as info:
        make(handler).meta()
    assert info.value.retry_after == expected


def test_poll_feed_resumed_with_a_smaller_limit_does_not_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr("finance_pulse.client.time.sleep", lambda s: None)
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({"cursor": None, "handled": ["s1", "s2", "s3", "s4", "s5"]}))   # run 1 used limit 10
    pages = {None: (["s1", "s2", "s3"], "c3"), "c3": (["s4", "s5", "s6"], "c6"), "c6": (["s7"], "c7")}
    fp = make(feed_handler(pages, []))
    assert [s["id"] for s in fp.poll_feed(cursor_file=path, limit=3, max_polls=3)] == ["s6", "s7"]
    assert json.loads(path.read_text()) == {"cursor": "c7", "handled": []}


def test_poll_feed_caps_a_huge_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr("finance_pulse.client.time.sleep", slept.append)
    answers = iter([httpx.Response(503, headers={"retry-after": "86400"}, json={"title": "x"}),
                    httpx.Response(200, json={"data": [{"id": "s1"}], "next_cursor": "c1", "snapshot": SNAPSHOT})])
    feed = make(lambda request: next(answers)).poll_feed(max_polls=1)
    assert next(feed)["id"] == "s1" and slept == [300]


@pytest.mark.parametrize("state", [{"cursor": 5}, {"cursor": "c", "handled": "abc"}, [1],
                                   {"cursor": None, "handled": [1, "a"]}])
def test_poll_feed_rejects_a_wrongly_shaped_cursor_file(tmp_path, state):
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="not a poll_feed cursor file"):
        next(make(lambda request: httpx.Response(500)).poll_feed(cursor_file=path))


def test_themes_filters():
    def handler(request):
        assert request.url.path == "/v2/themes"
        assert dict(request.url.params) == {"symbol": "NVDA,AMD", "sector": "Energy", "sort": "velocity"}
        return httpx.Response(200, json={"data": [], "snapshot": SNAPSHOT})

    make(handler).themes(symbol=["NVDA", "AMD"], sector="Energy", sort="velocity")


def test_no_removed_v1_names_are_sent():
    """API 2.0 removed the unprefixed paths and industry_group; unknown parameters are a 400."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [], "next_cursor": None, "snapshot": SNAPSHOT})

    fp = make(handler)
    fp.statements(symbol="NVDA", sector="Energy"); fp.feed(sector="Energy"); fp.series(sector="Energy")
    fp.symbols(sector="Energy"); fp.sectors(); fp.topics(sector="Energy"); fp.themes(sector="Energy")
    fp.trending(); fp.meta(); fp.topic("t"); fp.theme("t"); fp.statement("s")
    for request in seen:
        assert request.url.path.startswith("/v2/")
        assert not {"industry_group", "page", "period_days"} & set(request.url.params.keys())
        if "period" in request.url.params:
            assert request.url.path in ("/v2/symbols", "/v2/sectors")
