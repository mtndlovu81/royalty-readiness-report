"""Retry and backoff behaviour of the MusicBrainz client.

These paths only fire against a server we don't control, so without mocks they
are never exercised at all. `Retry-After: 0` in particular was a real bug found
by hand: upstream said "retry immediately", the client obliged, and all three
retries burned inside two seconds — during the exact busy period the backoff
exists to wait out. It looks identical to healthy operation in the logs.

Every case asserts the delay sequence *and* the request count. A retry that
fires the right number of times with the wrong delays is still broken, and
either assertion alone misses it.

No network and no real sleeping: the transport is mocked and `time.sleep` is
recorded rather than performed. The token bucket's real timing is deliberately
not tested here — it's verified live, and a sleep-based test would only make
the suite slow.
"""

import httpx
import pytest

from r3 import config
from r3 import musicbrainz as mb

# Sentinel for a scripted transport failure rather than an HTTP response.
TIMEOUT = "timeout"

OK_BODY = '{"artists": []}'


@pytest.fixture
def delays(monkeypatch):
    """Record backoff sleeps instead of performing them.

    The rate gate is flattened to zero so the only thing landing in the list is
    backoff. That is a test-local monkeypatch — the gate itself is untouched.
    """
    recorded: list[float] = []
    monkeypatch.setattr(config, "MB_RATE_LIMIT_SECONDS", 0.0)
    monkeypatch.setattr(mb, "GATE_MARGIN_SECONDS", 0.0)
    monkeypatch.setattr(mb, "_next_allowed_at", 0.0)
    monkeypatch.setattr(mb.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def transport(monkeypatch):
    """Install a scripted client; returns a callable giving the request count.

    The script is a list of `(status, headers)` pairs or `TIMEOUT`. The last
    entry repeats, so `[(503, {})]` means "503 forever".
    """
    clients: list[httpx.Client] = []

    def install(script):
        count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            entry = script[min(count["n"], len(script) - 1)]
            count["n"] += 1
            if entry == TIMEOUT:
                raise httpx.ReadTimeout("timed out", request=request)
            status, headers = entry
            return httpx.Response(
                status,
                headers=headers,
                text=OK_BODY if status == 200 else "busy",
            )

        client = httpx.Client(
            base_url=config.MB_BASE_URL,
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        monkeypatch.setattr(mb, "_client", client)
        return lambda: count["n"]

    yield install

    for client in clients:
        client.close()


# (id, script, expected delays, expected requests, expected exception)
CASES = [
    (
        "retry-after-zero-falls-back-to-exponential",
        [(503, {"Retry-After": "0"})],
        [1.0, 2.0, 4.0],
        4,
        mb.Unavailable,
    ),
    (
        "503-forever",
        [(503, {})],
        [1.0, 2.0, 4.0],
        4,
        mb.Unavailable,
    ),
    (
        "retry-after-3-server-value-wins",
        [(503, {"Retry-After": "3"}), (200, {})],
        [3.0],
        2,
        None,
    ),
    (
        "retry-after-9999-clamped-to-cap",
        [(503, {"Retry-After": "9999"}), (200, {})],
        [60.0],
        2,
        None,
    ),
    (
        "retry-after-unparseable-falls-back",
        [(503, {"Retry-After": "soon"}), (200, {})],
        [1.0],
        2,
        None,
    ),
    (
        "404-not-retried",
        [(404, {})],
        [],
        1,
        mb.NotFound,
    ),
    (
        "400-not-retried",
        [(400, {})],
        [],
        1,
        mb.BadRequest,
    ),
    (
        "200-first-try",
        [(200, {})],
        [],
        1,
        None,
    ),
    (
        # The timeout path retries without backing off; in production the rate
        # gate still holds the second attempt a second back.
        "timeout-then-success",
        [TIMEOUT, (200, {})],
        [],
        2,
        None,
    ),
]


@pytest.mark.parametrize(
    "script,expected_delays,expected_requests,expected_exc",
    [pytest.param(*case[1:], id=case[0]) for case in CASES],
)
def test_retry_behaviour(
    delays, transport, script, expected_delays, expected_requests, expected_exc
):
    requests = transport(script)

    if expected_exc is None:
        assert mb.get("artist", query="x") == {"artists": []}
    else:
        with pytest.raises(expected_exc):
            mb.get("artist", query="x")

    assert delays == expected_delays
    assert requests() == expected_requests


def test_retry_after_zero_never_sleeps_zero(delays, transport):
    """The specific regression: no delay in the sequence may be instant.

    Asserted separately from the table because this is the property that
    actually matters — a future change could keep the request count right and
    still reintroduce an instant retry.
    """
    transport([(503, {"Retry-After": "0"})])

    with pytest.raises(mb.Unavailable):
        mb.get("artist", query="x")

    assert delays, "expected the client to back off at all"
    assert all(d >= mb.BACKOFF_BASE_SECONDS for d in delays)


def test_retry_count_matches_configured_retries(delays, transport):
    """BUILD.md §4 says retry up to 3x — that is 3 retries, 4 requests."""
    requests = transport([(503, {})])

    with pytest.raises(mb.Unavailable):
        mb.get("artist", query="x")

    assert len(delays) == mb.MAX_RETRIES_503
    assert requests() == mb.MAX_RETRIES_503 + 1


def test_timeout_exhausted_raises_timeout(delays, transport):
    """A timeout that never clears is a Timeout, not an Unavailable."""
    requests = transport([TIMEOUT])

    with pytest.raises(mb.Timeout):
        mb.get("artist", query="x")

    assert requests() == mb.MAX_RETRIES_TIMEOUT + 1
