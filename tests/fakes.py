"""Hand-rolled requests.Session/Response test double.

The codebase threads `requests.Session` objects as parameters through
`fetcher.fetch(url, session)`, `scrape.scrape_chapters(profile, session, ...)`,
and `engine.search_novels(profile, session, query)`, so tests can just pass a
FakeSession in directly instead of hitting a real HTTP-mocking library.
"""

import requests


class FakeResponse:
    def __init__(self, text="", status_code=200, url="", headers=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Error for url: {self.url}", response=self)


class FakeSession:
    """responses: dict[url] -> FakeResponse | Exception | callable() -> (FakeResponse|Exception).

    strict=True (default): a request for a URL with no stubbed response raises
    AssertionError instead of silently returning an empty 200 -- this doubles
    as the assertion that a chapter past a tripped circuit breaker, or past a
    no_cache=False cache hit, was never actually requested over the network.
    """

    def __init__(self, responses=None, default=None, strict=True):
        self.responses = dict(responses or {})
        self.default = default
        self.strict = strict
        self.headers = {}
        self.calls = []  # [("GET"|"POST", url, kwargs), ...]

    def set(self, url, outcome):
        self.responses[url] = outcome

    def get(self, url, timeout=None, params=None, **kw):
        self.calls.append(("GET", url, {"timeout": timeout, "params": params, **kw}))
        return self._resolve(url)

    def post(self, url, data=None, timeout=None, **kw):
        self.calls.append(("POST", url, {"timeout": timeout, "data": data, **kw}))
        return self._resolve(url)

    def _resolve(self, url):
        outcome = self.responses.get(url, self.default)
        if outcome is None:
            if self.strict:
                raise AssertionError(f"FakeSession: no stubbed response for {url!r}")
            return FakeResponse("", 200, url)
        if callable(outcome) and not isinstance(outcome, FakeResponse):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSMTP:
    """smtplib.SMTP/SMTP_SSL test double. A test's smtp_factory is typically
    `lambda config: fake` (closing over one instance so the test can inspect
    fake.sent afterward), or a factory that itself raises to simulate a
    connect/login failure."""

    def __init__(self, fail_on_sendmail=None):
        self.sent = []  # [(from_addr, to_addrs, msg_bytes), ...]
        self.quit_called = False
        self.fail_on_sendmail = fail_on_sendmail  # Exception instance, or None

    def sendmail(self, from_addr, to_addrs, msg):
        if self.fail_on_sendmail is not None:
            raise self.fail_on_sendmail
        self.sent.append((from_addr, to_addrs, msg))

    def quit(self):
        self.quit_called = True
