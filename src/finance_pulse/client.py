"""Client for the Finance Pulse API, served through RapidAPI.

Every method returns the API's JSON as plain dicts, unchanged: {"data": ..., "next_cursor": ..., "snapshot": ...}.
Parameters, fields and limits are documented in the API reference: https://fintopic.news/docs/reference.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence, Union
from urllib.parse import quote

import httpx

PRICING_URL = "https://rapidapi.com/evankos/api/finance-pulse/pricing"


@dataclass(frozen=True)
class Backend:
    """Where the API is reached and how a key is sent. The API itself (paths, parameters, responses) is the same
    behind every backend."""
    base_url: str
    key_header: str                      # the header that carries the key
    key_env: str                         # environment variable read when no api_key is passed
    signup_url: str                      # where to get a key
    key_prefix: str = ""                 # e.g. "Bearer "
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def headers(self, api_key: str) -> dict:
        return {self.key_header: self.key_prefix + api_key, **self.extra_headers}


RAPIDAPI = Backend(base_url="https://finance-pulse.p.rapidapi.com", key_header="X-RapidAPI-Key",
                   key_env="RAPIDAPI_KEY", signup_url=PRICING_URL,
                   extra_headers={"X-RapidAPI-Host": "finance-pulse.p.rapidapi.com"})
DEFAULT_POLL_SECONDS = 150        # poll_feed's pause between polls; a client default, change it with interval=
PAGE_RETRIES = 5                  # iter_statements retries a failed page this often in a row, then raises
log = logging.getLogger("finance_pulse")      # one warning per retry, so a long spell of them is visible

Many = Union[str, Sequence[str], None]


class FinancePulseError(Exception):
    """Any failed request. `status` is the HTTP status, or 0 when no answer arrived (timeout, network). `title`,
    `detail` and `param` come from the API's problem+json body when there is one (a gateway's own errors, e.g.
    RapidAPI's 429 for the plan's quota, only have a message). `retry_after` is the Retry-After header in seconds."""

    def __init__(self, status: int, title: str, detail: Optional[str] = None, param: Optional[str] = None,
                 retry_after: Optional[float] = None):
        self.status, self.title, self.detail, self.param, self.retry_after = status, title, detail, param, retry_after
        text = f"{status} {title}" if status else title
        if detail:
            text += f": {detail}"
        if param:
            text += f" (parameter: {param})"
        super().__init__(text)

    @property
    def transient(self) -> bool:
        """True for no answer or a server-side (5xx) error: the same request may succeed later. A 429 is not
        counted here, though it too usually passes unless `quota_used_up`."""
        return self.status == 0 or self.status >= 500

    @property
    def quota_used_up(self) -> bool:
        """True for a 429 whose message says the plan's quota is used up: waiting a few minutes will not help."""
        return self.status == 429 and "quota" in self.title.lower()


def _retry_wait(exc: FinancePulseError, attempt: int) -> Optional[float]:
    """Seconds to wait before retry number `attempt` + 1 after `exc`, or None when retrying will not help.
    A temporary failure (no answer, 5xx) waits 10, 20, 40, ... seconds; a 429 for too many requests at once or
    the plan's rate limit waits 5, 15, 45, ... seconds; a longer Retry-After is waited instead; never more than
    5 minutes. A used-up quota, a 429 that asks for a longer wait than 5 minutes, and other errors are not retried."""
    if exc.transient:
        return min(300, max(exc.retry_after or 0, 10 * 2 ** min(attempt, 5)))
    if exc.status == 429 and not exc.quota_used_up and (exc.retry_after or 0) <= 300:
        return min(300, max(exc.retry_after or 0, 5 * 3 ** min(attempt, 4)))
    return None


def _many(value: Many) -> Optional[str]:
    """Several values go out comma-separated in one parameter (the API reads them as OR)."""
    if value is None:
        return None
    return value if isinstance(value, str) else ",".join(value)


def _segment(value: str) -> str:
    """An id as one path segment. Empty, "." and ".." would address another endpoint, so they are refused."""
    value = str(value).strip()
    if value in ("", ".", ".."):
        raise ValueError(f"{value!r} is not an id")
    return quote(value, safe="")


class FinancePulse:
    """Finance Pulse API client.

        fp = FinancePulse()                      # key from the RAPIDAPI_KEY environment variable
        for s in fp.statements(symbol="NVDA", importance_min="high", limit=5)["data"]:
            print(s["published_at"], s["sentiment"], s["statement"])
    """

    def __init__(self, api_key: Optional[str] = None, *, backend: Backend = RAPIDAPI,
                 base_url: Optional[str] = None, timeout: float = 30.0,
                 transport: Optional[httpx.BaseTransport] = None):
        api_key = (api_key or os.environ.get(backend.key_env) or "").strip()
        if not api_key:
            raise ValueError(f"No API key: pass api_key= or set {backend.key_env}. "
                             f"Get a free key at {backend.signup_url}")
        if not (api_key.isascii() and api_key.isprintable()):
            raise ValueError(f"The API key ({backend.key_env}) contains characters a key cannot have; copy it again")
        self.backend = backend
        # Redirects are followed by hand in get(): httpx would send the key header to whatever host one names.
        self._http = httpx.Client(base_url=base_url or backend.base_url, timeout=timeout, transport=transport,
                                  headers=backend.headers(api_key))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FinancePulse":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- transport ---------------------------------------------------------------------------------------------

    def get(self, path: str, **params: Any) -> dict:
        """GET any path of the API, e.g. "/v2/meta". None values are dropped, booleans are sent as true/false.
        Raises FinancePulseError for every failure, including timeouts and network errors (status 0)."""
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("path must start with a single '/': the key is only ever sent to the API's own host")
        if "?" in path or "#" in path:
            raise ValueError("pass query parameters as keyword arguments, not in the path")
        query = {key: (str(value).lower() if isinstance(value, bool) else str(value))
                 for key, value in params.items() if value is not None}
        try:
            request = self._http.build_request("GET", path, params=query)
        except httpx.InvalidURL as exc:
            raise ValueError(f"not a valid path: {exc}") from exc
        try:
            response = self._http.send(request)
            for _ in range(3):          # a retired id answers 308 to its successor, on the same host
                if not response.has_redirect_location:
                    break
                target = response.url.join(response.headers["location"])
                if (target.scheme, target.host, target.port) != (response.url.scheme, response.url.host, response.url.port):
                    raise FinancePulseError(response.status_code, "Redirect to another origin refused",
                                            f"{target.scheme}://{target.netloc.decode()}")
                response = self._http.get(target)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise FinancePulseError(0, "Could not reach the API", f"{type(exc).__name__}: {exc}".rstrip(": ")) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success:
            if not isinstance(body, dict) or "data" not in body:
                raise FinancePulseError(response.status_code, "Unexpected response",
                                        "the body is not the API's JSON envelope")
            return body
        if not isinstance(body, dict):
            body = {}
        title = body.get("title") or body.get("message") or body.get("detail") or response.reason_phrase
        detail = body.get("detail") if body.get("title") else None
        try:
            retry_after = max(0.0, float(response.headers.get("retry-after", "")))
        except ValueError:              # absent, or an HTTP date
            retry_after = None
        if retry_after is not None and not math.isfinite(retry_after):
            retry_after = None
        raise FinancePulseError(response.status_code, str(title), detail, body.get("param"), retry_after)

    def _retrying(self, call, /, *args, **kwargs) -> dict:
        """call(*args, **kwargs), retried up to PAGE_RETRIES times in a row when _retry_wait allows it."""
        for attempt in range(PAGE_RETRIES + 1):
            try:
                return call(*args, **kwargs)
            except FinancePulseError as exc:
                wait = _retry_wait(exc, attempt)
                if wait is None or attempt == PAGE_RETRIES:
                    raise
                log.warning("%s; retrying in %.0f s", exc, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _pages(self, path: str, params: dict, max_items: Optional[int]) -> Iterator[dict]:
        seen = 0
        while True:
            page = self._retrying(self.get, path, **params)
            for row in page["data"]:
                yield row
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
            if not page.get("next_cursor"):
                return
            params = {**params, "cursor": page["next_cursor"]}

    # ---- statements --------------------------------------------------------------------------------------------

    def statements(self, *, symbol: Many = None, sector: Many = None, theme_id: Optional[str] = None,
                   topic_id: Optional[str] = None, storyline_id: Optional[str] = None, sentiment: Many = None,
                   importance_min: Optional[str] = None,
                   source_type: Many = None, since: Optional[str] = None, until: Optional[str] = None,
                   has_image: Optional[bool] = None, limit: int = 50, cursor: Optional[str] = None) -> dict:
        """Search statements across all topics, newest first by the article's post time. One page.
        symbol, sector, sentiment and source_type take one value or a list (OR). storyline_id gives every
        statement of that storyline's topics; add symbol= for only those that name its symbol."""
        return self.get("/v2/statements", symbol=_many(symbol), sector=_many(sector), theme_id=theme_id,
                        topic_id=topic_id, storyline_id=storyline_id, sentiment=_many(sentiment),
                        importance_min=importance_min,
                        source_type=_many(source_type), since=since, until=until, has_image=has_image, limit=limit,
                        cursor=cursor)

    def iter_statements(self, *, max_items: Optional[int] = 500, **filters: Any) -> Iterator[dict]:
        """Statements across pages (each page is one request). Takes the filters of statements().
        max_items=None reads to the end of your plan's history. A page that fails for a temporary reason (timeout, network
        error, 5xx) or with a passing 429 is retried up to five times in a row, with a growing pause; a used-up
        quota, or a 429 that asks to wait more than 5 minutes, is raised at once. Each retry is a warning on the
        "finance_pulse" logger."""
        filters.setdefault("limit", 100)
        for key in ("symbol", "sector", "sentiment", "source_type"):
            if key in filters:
                filters[key] = _many(filters[key])
        return self._pages("/v2/statements", filters, max_items)

    def statement(self, statement_id: str) -> dict:
        return self.get(f"/v2/statements/{_segment(statement_id)}")

    def feed(self, *, cursor: Optional[str] = None, symbol: Many = None, sector: Many = None,
             theme_id: Optional[str] = None, topic_id: Optional[str] = None, sentiment: Many = None,
             importance_min: Optional[str] = None, source_type: Many = None, limit: int = 100) -> dict:
        """Statements in the order they entered the API, after `cursor`. Without a cursor the feed starts a
        limited time back (the API's default starting point), not at the start of your history.
        next_cursor is always set; store it and pass it on the next call. `more` true means more statements may
        follow now (even after a short page): call again at once; false means you are up to date."""
        return self.get("/v2/feed", cursor=cursor, symbol=_many(symbol), sector=_many(sector), theme_id=theme_id,
                        topic_id=topic_id, sentiment=_many(sentiment), importance_min=importance_min,
                        source_type=_many(source_type), limit=limit)

    def poll_feed(self, *, cursor: Optional[str] = None, cursor_file: Union[str, Path, None] = None,
                  interval: float = DEFAULT_POLL_SECONDS, max_polls: Optional[int] = None,
                  **filters: Any) -> Iterator[dict]:
        """Yield new statements as they enter the API, forever (or for max_polls successful polls). Takes the
        filters of feed().

        With cursor_file the position survives restarts: a statement is recorded as handled when you ask for
        the next one, so after a crash or a `break` nothing is skipped and only the statement you were working on
        is delivered again (dedupe on `id` if that matters). Two limits:
        - The first run starts at the feed's default starting point (a limited time back), and there is no
          position to store until its first page has been fully handled. A restart before that begins again at
          the default starting point as of the restart, so statements of that first page that were not handled
          and are older than that are not delivered.
        - After a long pause the poller reads the whole backlog since its saved position, as far back as your
          plan's history reaches (older statements are skipped), page after page without sleeping, at least one
          request per `limit` statements. Delete the file to start afresh instead.
        Without cursor_file every start begins at the default starting point. A saved position belongs to the
        filters it was reached with: use one cursor file per set of filters.

        When the API says more statements may follow (`more`), the next page is asked for at once; otherwise
        the poller sleeps `interval` seconds.
        Every poll is one request: at the default interval a poller that runs all month makes about 17,000
        requests, so set `interval` to fit your plan's monthly quota. Temporary failures (timeouts, network
        errors, 5xx) and a 429 for too many requests at once or the plan's rate limit are retried for as long as
        they last, with a pause that grows to 5 minutes; each retry is a warning on the "finance_pulse" logger.
        A used-up quota, a 429 that asks to wait more than 5 minutes, and other errors are raised at once.
        """
        path = Path(cursor_file) if cursor_file else None
        handled: set = set()
        if path is not None and not path.parent.is_dir():
            raise ValueError(f"The directory of cursor_file does not exist: {path.parent}")
        if path is not None and path.exists():
            try:
                state = json.loads(path.read_text())
                saved, ids = state["cursor"], state.get("handled", [])
                if not (saved is None or isinstance(saved, str)) or not isinstance(ids, list) \
                        or not all(isinstance(i, str) for i in ids):
                    raise TypeError
                handled = set(ids)
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path} is not a poll_feed cursor file; delete it to start afresh") from exc
            if cursor is None:
                cursor = saved
            else:
                handled = set()        # an explicit cursor overrides the file

        def save(position: Optional[str], done: set) -> None:
            if path is None:
                return
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"cursor": position, "handled": sorted(done)}))
            os.replace(tmp, path)       # never leaves a half-written file

        save(cursor, handled)           # fails now, not after the first statement, if the file cannot be written
        limit = filters["limit"] = filters.get("limit") or 100
        polls = failures = 0
        while max_polls is None or polls < max_polls:
            try:
                page = self.feed(cursor=cursor, **filters)
            except FinancePulseError as exc:
                wait = _retry_wait(exc, failures)
                if wait is None:
                    raise
                failures += 1
                log.warning("%s; retrying in %.0f s", exc, wait)
                time.sleep(wait)
                continue
            failures = 0
            polls += 1
            for statement in page["data"]:
                if statement["id"] in handled:
                    continue            # delivered before the restart
                yield statement
                handled.add(statement["id"])
                save(cursor, handled)
            # The page is behind the new cursor; ids handled earlier that it did not contain (the previous run
            # used a larger limit) are still ahead and stay recorded.
            handled -= {statement["id"] for statement in page["data"]}
            sent, cursor = cursor, page["next_cursor"]
            save(cursor, handled)
            more = page.get("more")
            if more is None:                # an answer without the flag: a full page means more may follow
                more = len(page["data"]) >= limit
            if not page["data"] and cursor == sent:
                more = False                # a position that does not move is never followed without a pause
            if not more and (max_polls is None or polls < max_polls):
                time.sleep(interval)

    # ---- signals -----------------------------------------------------------------------------------------------

    def series(self, *, symbol: Optional[str] = None, sector: Optional[str] = None, theme_id: Optional[str] = None,
               topic_id: Optional[str] = None, interval: str = "day", since: Optional[str] = None,
               until: Optional[str] = None, source_type: Many = None, importance_min: Optional[str] = None) -> dict:
        """Hourly or daily statement counts, sentiment and distinct sources for exactly one entity (zero-filled).
        Without `until` the last bucket is the current, still incomplete hour or day."""
        return self.get("/v2/series", symbol=symbol, sector=sector, theme_id=theme_id, topic_id=topic_id,
                        interval=interval, since=since, until=until, source_type=_many(source_type),
                        importance_min=importance_min)

    def symbols(self, *, period: str = "w", sort: str = "mentions", sector: Optional[str] = None,
                kind: Many = None, min_mentions: int = 3, q: Optional[str] = None, since: Optional[str] = None,
                until: Optional[str] = None, limit: int = 500) -> dict:
        """Symbol screener. period: d (24 h), w (7 d), window. sort: mentions, sources, score, change.
        kind narrows to entity kinds, e.g. ["equity", "etf"] for tradable tickers (all kinds are in meta()).
        q looks a symbol up by code or name (q="micron" finds MU) among those mentioned in the period, so
        pass period="window" with it; it ignores min_mentions.
        since / until (ISO-8601) count a custom interval instead of `period`; the change is then against the
        equally long interval before it. Each row carries the entity's `kind` and `name`, and the `_single`
        counts over statements that name only that entity. `change` is null for period="window"."""
        return self.get("/v2/symbols", period=period, sort=sort, sector=sector, kind=_many(kind),
                        min_mentions=min_mentions, q=q, since=since, until=until, limit=limit)

    def symbol(self, code: str) -> dict:
        """One symbol in one call: who it is, its counts and sentiment per period, sectors, largest topics, top
        storylines, related symbols and newest statements. `periods` holds d, w and window, leaving out a period
        without mentions. A known alias answers as its code; look a code up with symbols(q=...)."""
        return self.get(f"/v2/symbols/{_segment(code)}")

    def sectors(self, *, period: str = "w", sort: str = "mentions", since: Optional[str] = None,
                until: Optional[str] = None) -> dict:
        """The GICS sectors with the same statistics as symbols(); since / until work the same way."""
        return self.get("/v2/sectors", period=period, sort=sort, since=since, until=until)

    # ---- developments ------------------------------------------------------------------------------------------

    def trending(self, *, kind: Optional[str] = None, theme_id: Optional[str] = None,
                 symbol: Optional[str] = None) -> dict:
        """Developments the most outlets picked up. kind: live (the current list) or slot (the top of each
        recent time slot); without kind, both. Each item's `trending` labels say where it ranked."""
        return self.get("/v2/trending", kind=kind, theme_id=theme_id, symbol=symbol)

    def topics(self, *, theme_id: Optional[str] = None, symbol: Many = None, sector: Optional[str] = None,
               storyline_id: Optional[str] = None, ids: Many = None, updated_since: Optional[str] = None,
               min_statements: int = 1, sort: str = "recent", limit: int = 50,
               cursor: Optional[str] = None) -> dict:
        """Topics (developments). sort: recent, size, oldest, velocity. ids: several topic ids at once.
        storyline_id: only the topics of that storyline. A topic's `revision` changes whenever the topic or its
        recent statements change; `storylines` lists the storylines it belongs to."""
        return self.get("/v2/topics", theme_id=theme_id, symbol=_many(symbol), sector=sector,
                        storyline_id=storyline_id, ids=_many(ids), updated_since=updated_since,
                        min_statements=min_statements, sort=sort, limit=limit, cursor=cursor)

    def topic(self, topic_id: str) -> dict:
        """One topic with its statement counts per source type, its storylines and its newest statements."""
        return self.get(f"/v2/topics/{_segment(topic_id)}")

    def storylines(self, *, symbol: Optional[str] = None, ids: Many = None, min_topics: int = 1,
                   sort: str = "recent", limit: int = 50, cursor: Optional[str] = None) -> dict:
        """One symbol's storylines: its topics grouped into continuing stories (an event, then its follow-ups).
        Pass exactly one symbol, or ids= for several storylines by id. min_topics=2 leaves out the storylines
        that hold a single topic. sort: recent, mentions, topics, velocity. The grouping is automatic: a
        reading aid, not a count of events."""
        return self.get("/v2/storylines", symbol=symbol, ids=_many(ids), min_topics=min_topics,
                        sort=None if ids else sort, limit=None if ids else limit, cursor=cursor)

    def storyline(self, storyline_id: str) -> dict:
        """One storyline with its topics (oldest first) and its newest statements. For a very long storyline
        compare `topics` with the length of `members`; topics(storyline_id=...) pages through all of them."""
        return self.get(f"/v2/storylines/{_segment(storyline_id)}")

    def themes(self, *, symbol: Many = None, sector: Many = None, sort: str = "size") -> dict:
        """All themes with their statistics, or only those with statements about a symbol or sector.
        sort: size (statements), topics, recent (last statement), velocity (last 24 h). Not paged."""
        return self.get("/v2/themes", symbol=_many(symbol), sector=_many(sector), sort=sort)

    def theme(self, theme_id: str) -> dict:
        return self.get(f"/v2/themes/{_segment(theme_id)}")

    def meta(self) -> dict:
        """How far back your plan's history goes (window_start, window_days), counts and every accepted filter
        value."""
        return self.get("/v2/meta")
