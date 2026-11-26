"""Synchronous wrapper around the public Open-Meteo MCP server (Forecast FM).

call_tool() returns the result's structured_content dict, or raises WeatherError.
Server markdown and notices (e.g. the low-confidence geocoding warning) are never
returned, because they are third-party text. Each call opens a fresh connection.
"""
import asyncio

URL = "https://open-meteo.caseyjhand.com/mcp"
MAX_ERR_CHARS = 500


class WeatherError(Exception):
    """Any failure talking to the weather server. Message is short plain text."""


async def _call(name, args):
    from mcp import Client  # lazy import so the tests run without mcp
    async with Client(URL) as client:
        return await client.call_tool(name, args)


def _leaves(e):
    subs = getattr(e, "exceptions", None)
    if subs:
        for s in subs:
            yield from _leaves(s)
    else:
        yield e


def _describe(e):
    """Name the real cause. An ExceptionGroup's own str() is generic, so unwrap it."""
    parts = [f"{type(x).__name__}: {str(x)[:200]}" for x in list(_leaves(e))[:3]]
    return "; ".join(parts)[:MAX_ERR_CHARS]


def _text(res):
    return "\n".join(getattr(b, "text", "") or "" for b in (res.content or []))


def call_tool(name, args, timeout=30, _impl=None):
    """Return structured_content (a dict) or raise WeatherError. Never returns server text."""
    impl = _impl or _call
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop running: asyncio.run is safe
    else:
        raise WeatherError("called from inside a running event loop")
    try:
        res = asyncio.run(asyncio.wait_for(impl(name, args), timeout))
    except TimeoutError:
        raise WeatherError(f"timeout after {timeout}s calling {name}") from None
    except Exception as e:  # includes ExceptionGroup from anyio task groups
        raise WeatherError(_describe(e)) from e
    if res.is_error:
        raise WeatherError("server error: " + _text(res)[:MAX_ERR_CHARS])
    sc = res.structured_content
    if not isinstance(sc, dict):
        raise WeatherError(f"no structured content from {name}")
    return sc
