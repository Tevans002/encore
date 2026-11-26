"""Offline tests for weather_tool.call_tool (fake results, no network).

Deliberately does not use the enc/net/clock fixtures: the clock fixture patches
time.monotonic, and asyncio's event loop reads that clock.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest

import weather_tool as w


def _result(sc=None, is_error=False, texts=("x",)):
    return NS(structured_content=sc, is_error=is_error,
              content=[NS(type="text", text=t) for t in texts])


def _fake(res=None, exc=None, delay=0):
    async def impl(name, args):
        if delay:
            await asyncio.sleep(delay)
        if exc:
            raise exc
        return res
    return impl


def test_success_returns_structured_dict():
    assert w.call_tool("t", {}, _impl=_fake(_result({"a": 1}))) == {"a": 1}


def test_extra_text_blocks_are_not_returned():
    res = _result({"a": 1}, texts=("md", "> Low-confidence match"))
    assert w.call_tool("t", {}, _impl=_fake(res)) == {"a": 1}


def test_is_error_raises_and_truncates():
    res = _result(None, True, ("Error: Unknown variable name: zzz. " + "p" * 2000,))
    with pytest.raises(w.WeatherError) as e:
        w.call_tool("t", {}, _impl=_fake(res))
    msg = str(e.value)
    assert "Unknown variable name: zzz" in msg
    assert len(msg) <= len("server error: ") + w.MAX_ERR_CHARS


def test_raised_exception_is_wrapped():
    with pytest.raises(w.WeatherError, match="ConnectionError: boom"):
        w.call_tool("t", {}, _impl=_fake(exc=ConnectionError("boom")))


def test_exception_group_names_the_leaf():
    g = ExceptionGroup("unhandled errors in a TaskGroup",
                       [OSError("All connection attempts failed")])
    with pytest.raises(w.WeatherError) as e:
        w.call_tool("t", {}, _impl=_fake(exc=g))
    assert "OSError: All connection attempts failed" in str(e.value)
    assert "TaskGroup" not in str(e.value)


def test_nested_exception_group():
    g = ExceptionGroup("outer", [ExceptionGroup("inner", [ValueError("deep")])])
    with pytest.raises(w.WeatherError, match="ValueError: deep"):
        w.call_tool("t", {}, _impl=_fake(exc=g))


def test_many_leaves_are_capped():
    g = ExceptionGroup("g", [OSError("x" * 1000) for _ in range(10)])
    with pytest.raises(w.WeatherError) as e:
        w.call_tool("t", {}, _impl=_fake(exc=g))
    assert len(str(e.value)) <= w.MAX_ERR_CHARS


def test_timeout():
    with pytest.raises(w.WeatherError, match="timeout"):
        w.call_tool("t", {}, timeout=0.05, _impl=_fake(_result({}), delay=1))


def test_inside_running_loop_raises_cleanly():
    async def outer():
        return w.call_tool("t", {}, _impl=_fake(_result({"a": 1})))
    with pytest.raises(w.WeatherError, match="running event loop"):
        asyncio.run(outer())


def test_missing_structured_content():
    with pytest.raises(w.WeatherError, match="no structured content"):
        w.call_tool("t", {}, _impl=_fake(_result(None)))


def test_repeated_calls():
    for i in range(3):
        assert w.call_tool("t", {}, _impl=_fake(_result({"i": i}))) == {"i": i}
