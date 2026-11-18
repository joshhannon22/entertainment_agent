"""Offline tests for _get_json retries/backoff/deadline. No network, no real sleeping.

python tests/test_get_json.py      (from apps/tools-server; pytest also works if installed)

httpx.MockTransport stands in for the network and FakeClock for time, so a 20 s deadline
test runs in milliseconds and we can assert exactly how long we would have waited.
"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tools  # noqa: E402

URL = "https://api.example.test/thing"


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def install(responses):
    """Patch tools to use a fake clock and a transport replaying `responses` in order.

    Each item is an httpx.Response, or an exception class to raise (a timeout "takes" the
    attempt's full read timeout on the fake clock, like a real one would).
    """
    clock = FakeClock()
    calls = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0)
        if isinstance(item, type) and issubclass(item, Exception):
            if issubclass(item, httpx.TimeoutException):
                clock.now += request.extensions["timeout"]["read"]
            raise item("simulated", request=request)
        return item

    tools.time = clock   # tools does `import time`, so this swaps monotonic() and sleep()
    tools._http = httpx.Client(headers=tools.HEADERS, transport=httpx.MockTransport(handler))
    return clock, calls


def get():
    return tools._get_json("Example", URL, {"q": "x"}, "Answer without it.")


def test_success_first_try():
    clock, calls = install([httpx.Response(200, json={"ok": True})])
    assert get() == ({"ok": True}, None)
    assert len(calls) == 1 and clock.sleeps == []


def test_user_agent_sent():
    _, calls = install([httpx.Response(200, json={})])
    get()
    assert calls[0].headers["User-Agent"] == tools.USER_AGENT


def test_201_is_success():   # FreeToGame's "no results"
    install([httpx.Response(201, json={"status": 0})])
    assert get() == ({"status": 0}, None)


def test_retries_5xx_then_succeeds():
    clock, calls = install([httpx.Response(503), httpx.Response(502), httpx.Response(200, json=[1])])
    assert get() == ([1], None)
    assert len(calls) == 3 and len(clock.sleeps) == 2
    assert all(0 <= s <= tools.BACKOFF_CAP_S for s in clock.sleeps)


def test_gives_up_after_max_attempts():
    _, calls = install([httpx.Response(500)] * tools.MAX_ATTEMPTS)
    data, err = get()
    assert data is None and "HTTP 500" in err["error"] and "Don't call it again" in err["error"]
    assert len(calls) == tools.MAX_ATTEMPTS


def test_4xx_not_retried():
    _, calls = install([httpx.Response(404)])
    data, err = get()
    assert "HTTP 404" in err["error"] and "Don't call it again" not in err["error"]
    assert len(calls) == 1


def test_429_honors_retry_after():
    clock, calls = install([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={})])
    assert get() == ({}, None)
    assert clock.sleeps == [2.0] and len(calls) == 2


def test_retry_after_past_deadline_fails_fast():
    clock, calls = install([httpx.Response(429, headers={"Retry-After": "60"})])
    data, err = get()
    assert "rate limiting" in err["error"]
    assert len(calls) == 1 and clock.sleeps == []   # didn't sleep 60 s just to miss the deadline


def test_non_json_not_retried():
    _, calls = install([httpx.Response(200, text="<html>maintenance</html>")])
    data, err = get()
    assert "non-JSON" in err["error"] and len(calls) == 1


def test_connect_error_retried():
    _, calls = install([httpx.ConnectError, httpx.Response(200, json={})])
    assert get() == ({}, None) and len(calls) == 2


def test_timeouts_stay_inside_total_deadline():
    # The Jikan case: a host that never answers. Must give up within TOOL_DEADLINE_S,
    # well under agent-api's 30 s MCP timeout.
    clock, calls = install([httpx.ReadTimeout] * tools.MAX_ATTEMPTS)
    start = clock.now
    data, err = get()
    assert "timed out" in err["error"]
    assert clock.now - start <= tools.TOOL_DEADLINE_S
    assert all(c.extensions["timeout"]["connect"] <= tools.CONNECT_TIMEOUT_S for c in calls)


def test_attempt_timeout_shrinks_to_remaining_budget():
    clock, calls = install([httpx.Response(200, json={})])
    tools._get_json("Example", URL, {}, "", deadline=clock.now + 2.5)
    assert calls[0].extensions["timeout"]["read"] == 2.5


def test_expired_deadline_makes_no_call():
    clock, calls = install([])
    data, err = tools._get_json("Example", URL, {}, "Skip it.", deadline=clock.now + 0.5)
    assert calls == [] and "didn't answer in time" in err["error"]


def test_tool_maps_error_dict_through():   # a real tool, end to end, still never raises
    install([httpx.Response(503)] * tools.MAX_ATTEMPTS)
    result = tools.search_tv_shows("Arcane")
    assert set(result) == {"error"} and "TVMaze" in result["error"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
