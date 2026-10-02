"""Client for the Finance Pulse API (version 2.0, the /v2 endpoints), served through RapidAPI.

Every method returns the API's JSON as plain dicts, unchanged: {"data": ..., "next_cursor": ..., "snapshot": ...}.
The API reference is at https://fintopic.news/docs/reference.
"""
from __future__ import annotations

import json
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
BUILD_INTERVAL_SECONDS = 150      # the API builds a new snapshot about this often; polling faster returns the same data

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
        """True when the same request may succeed later: no answer, or a server-side (5xx) error."""
        return self.status == 0 or self.status >= 500


def _many(value: Many) -> Optional[str]:
    """Several values go out comma-separated in one parameter (the API reads them as OR)."""
    if value is None:
        return None
    return value if isinstance(value, str) else ",".join(value)


def _segment(value: str) -> str:
    return quote(str(value), safe="")


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
        query = {key: (str(value).lower() if isinstance(value, bool) else str(value))
                 for key, value in params.items() if value is not None}
        try:
            response = self._http.get(path, params=query)
            for _ in range(3):          # a merged theme answers 308 to its successor, on the same host
                if not response.has_redirect_location:
                    break
                target = response.url.join(response.headers["location"])
                if (target.scheme, target.host, target.port) != (response.url.scheme, response.url.host, response.url.port):
                    raise FinancePulseError(response.status_code, "Redirect to another origin refused",
                                            f"{target.scheme}://{target.netloc.decode()}")
                response = self._http.get(target)
        except httpx.HTTPError as exc:
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

    def _pages(self, path: str, params: dict, max_items: Optional[int]) -> Iterator[dict]:
        seen = 0
        while True:
            page = self.get(path, **params)
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
                   topic_id: Optional[str] = None, sentiment: Many = None, importance_min: Optional[str] = None,
                   source_type: Many = None, since: Optional[str] = None, until: Optional[str] = None,
                   has_image: Optional[bool] = None, limit: int = 50, cursor: Optional[str] = None) -> dict:
        """Search statements across all topics, newest first by the article's post time. One page.
        symbol, sector, sentiment and source_type take one value or a list (OR)."""
        return self.get("/v2/statements", symbol=_many(symbol), sector=_many(sector), theme_id=theme_id,
                        topic_id=topic_id, sentiment=_many(sentiment), importance_min=importance_min,
                        source_type=_many(source_type), since=since, until=until, has_image=has_image, limit=limit,
                        cursor=cursor)

    def iter_statements(self, *, max_items: Optional[int] = 500, **filters: Any) -> Iterator[dict]:
        """Statements across pages (each page is one request). Takes the filters of statements().
        max_items=None reads to the end of the window."""
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
        """Statements in the order they entered the API, after `cursor`. Without a cursor: the last 24 hours.
        next_cursor is always set; store it and pass it on the next call."""
        return self.get("/v2/feed", cursor=cursor, symbol=_many(symbol), sector=_many(sector), theme_id=theme_id,
                        topic_id=topic_id, sentiment=_many(sentiment), importance_min=importance_min,
                        source_type=_many(source_type), limit=limit)

    def poll_feed(self, *, cursor: Optional[str] = None, cursor_file: Union[str, Path, None] = None,
                  interval: float = BUILD_INTERVAL_SECONDS, max_polls: Optional[int] = None,
                  **filters: Any) -> Iterator[dict]:
        """Yield new statements as they enter the API, forever (or for max_polls successful polls). Takes the
        filters of feed().

        With cursor_file the position survives restarts: a statement is recorded as handled when you ask for
        the next one, so after a crash or a `break` nothing is skipped and only the statement you were working on
        is delivered again (dedupe on `id` if that matters). Two limits:
        - The first run starts 24 hours back, and there is no position to store until its first page has been
          fully handled. A restart before that begins again 24 hours before the restart, so statements of that
          first page that were not handled and are older than that are not delivered.
        - After a long pause the poller reads the whole backlog since its saved position, page after page
          without sleeping, one request per `limit` statements. Delete the file to start 24 hours back instead.
        Without cursor_file every start begins 24 hours back. A saved position belongs to the filters it was
        reached with: use one cursor file per set of filters.

        A page that comes back full is followed immediately; otherwise the poller sleeps `interval` seconds, which
        should not be lower than the API's 150 s build interval. Temporary failures (timeouts, network errors,
        5xx) are retried with a growing pause of up to 5 minutes; other errors, such as a used-up quota (429),
        are raised.
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
                raise ValueError(f"{path} is not a poll_feed cursor file; delete it to start 24 hours back") from exc
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
                if not exc.transient:
                    raise
                failures += 1
                time.sleep(min(300, exc.retry_after or 10 * 2 ** min(failures - 1, 5)))
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
            cursor = page["next_cursor"]
            save(cursor, handled)
            if len(page["data"]) < limit and (max_polls is None or polls < max_polls):
                time.sleep(interval)

    # ---- signals -----------------------------------------------------------------------------------------------

    def series(self, *, symbol: Optional[str] = None, sector: Optional[str] = None, theme_id: Optional[str] = None,
               topic_id: Optional[str] = None, interval: str = "day", since: Optional[str] = None,
               until: Optional[str] = None, source_type: Many = None, importance_min: Optional[str] = None) -> dict:
        """Hourly or daily statement counts, sentiment and distinct sources for exactly one entity (zero-filled).
        The last bucket is the current, still incomplete hour or day."""
        return self.get("/v2/series", symbol=symbol, sector=sector, theme_id=theme_id, topic_id=topic_id,
                        interval=interval, since=since, until=until, source_type=_many(source_type),
                        importance_min=importance_min)

    def symbols(self, *, period: str = "w", sort: str = "mentions", sector: Optional[str] = None,
                kind: Many = None, min_mentions: int = 3, limit: int = 500) -> dict:
        """Symbol screener. period: d (24 h), w (7 d), window. sort: mentions, sources, score, change.
        kind narrows to entity kinds, e.g. ["equity", "etf"] for tradable tickers (all kinds are in meta()).
        Each row carries the entity's `kind` and `name`. `change` is null for period="window". limit: up to
        20000 (every symbol: period="window", min_mentions=1, limit=20000)."""
        return self.get("/v2/symbols", period=period, sort=sort, sector=sector, kind=_many(kind),
                        min_mentions=min_mentions, limit=limit)

    def sectors(self, *, period: str = "w", sort: str = "mentions") -> dict:
        """The 11 GICS sectors with the same statistics as symbols()."""
        return self.get("/v2/sectors", period=period, sort=sort)

    # ---- developments ------------------------------------------------------------------------------------------

    def trending(self, *, kind: Optional[str] = None, theme_id: Optional[str] = None,
                 symbol: Optional[str] = None) -> dict:
        """Developments the most outlets picked up. kind: live (last 2 hours) or slot (2-hour slots, 7 days)."""
        return self.get("/v2/trending", kind=kind, theme_id=theme_id, symbol=symbol)

    def topics(self, *, theme_id: Optional[str] = None, symbol: Many = None, sector: Optional[str] = None,
               ids: Many = None, updated_since: Optional[str] = None, min_statements: int = 1,
               sort: str = "recent", limit: int = 50, cursor: Optional[str] = None) -> dict:
        """Topics (developments). sort: recent, size, oldest, velocity. ids: up to 50 topic ids.
        A topic's `revision` changes whenever the topic or its last-7-day statements change."""
        return self.get("/v2/topics", theme_id=theme_id, symbol=_many(symbol), sector=sector, ids=_many(ids),
                        updated_since=updated_since, min_statements=min_statements, sort=sort, limit=limit,
                        cursor=cursor)

    def topic(self, topic_id: str) -> dict:
        """One topic with its statement counts per source type and its 20 newest statements."""
        return self.get(f"/v2/topics/{_segment(topic_id)}")

    def themes(self, *, symbol: Many = None, sector: Many = None, sort: str = "size") -> dict:
        """All themes with their statistics, or only those with statements about a symbol or sector.
        sort: size (statements), topics, recent (last statement), velocity (last 24 h). Not paged."""
        return self.get("/v2/themes", symbol=_many(symbol), sector=_many(sector), sort=sort)

    def theme(self, theme_id: str) -> dict:
        return self.get(f"/v2/themes/{_segment(theme_id)}")

    def meta(self) -> dict:
        """The window, counts, known data incidents and every accepted filter value."""
        return self.get("/v2/meta")
