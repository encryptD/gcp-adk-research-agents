"""Unit tests for rate-limit detection and ADK plugin behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.tools.base_tool import BaseTool

from research_agent.rate_limit import (
    STATE_RATE_LIMIT_EVENT,
    STATE_RATE_LIMITED,
    RateLimitPlugin,
    is_rate_limit_error,
    is_rate_limit_payload,
    rate_limit_tool_result,
)


class _FakeRateLimitError(Exception):
    def __init__(self, message: str = "RESOURCE_EXHAUSTED: quota", code: int = 429):
        super().__init__(message)
        self.code = code


class _FakeTool(BaseTool):
    def __init__(self, name: str, behavior: list[Any]):
        super().__init__(name=name, description="test tool")
        self._behavior = list(behavior)
        self.calls = 0

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        self.calls += 1
        if not self._behavior:
            raise RuntimeError("no more behaviors")
        item = self._behavior.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_is_rate_limit_error_by_code_and_message() -> None:
    assert is_rate_limit_error(_FakeRateLimitError())
    assert is_rate_limit_error(Exception("HTTP 429 Too Many Requests"))
    assert is_rate_limit_error(Exception("Quota exceeded for project"))
    assert not is_rate_limit_error(Exception("not found"))
    assert not is_rate_limit_error(ValueError("bad args"))


def test_is_rate_limit_payload_dict() -> None:
    assert is_rate_limit_payload({"status": "rate_limited"})
    assert is_rate_limit_payload({"http_status": 429, "message": "slow down"})
    assert is_rate_limit_payload({"error": "RESOURCE_EXHAUSTED"})
    assert not is_rate_limit_payload({"status": "ok", "data": [1]})


@pytest.mark.asyncio
async def test_plugin_soft_handles_tool_429_without_retry_for_mutating_tool() -> None:
    plugin = RateLimitPlugin(max_auto_retries=2, initial_backoff_seconds=0)
    tool = _FakeTool("saveResearchSlice", [])
    ctx = SimpleNamespace(state={})

    result = await plugin.on_tool_error_callback(
        tool=tool,
        tool_args={"run_id": "abc"},
        tool_context=ctx,
        error=_FakeRateLimitError(),
    )

    assert result is not None
    assert result["status"] == "rate_limited"
    assert result["http_status"] == 429
    assert result["retries_attempted"] == 0
    assert tool.calls == 0  # mutating tools must not auto-retry
    assert ctx.state[STATE_RATE_LIMITED] is True
    assert ctx.state[STATE_RATE_LIMIT_EVENT]["tool"] == "saveResearchSlice"


@pytest.mark.asyncio
async def test_plugin_retries_safe_search_tool_once() -> None:
    plugin = RateLimitPlugin(max_auto_retries=1, initial_backoff_seconds=0)
    tool = _FakeTool(
        "google_search",
        [_FakeRateLimitError(), {"results": [{"title": "ok"}]}],
    )
    # First call path is the outer exception; plugin will call run_async for retry.
    # Seed behavior for the retry only.
    tool = _FakeTool("google_search", [{"results": [{"title": "ok"}]}])
    ctx = SimpleNamespace(state={})

    result = await plugin.on_tool_error_callback(
        tool=tool,
        tool_args={"query": "adk"},
        tool_context=ctx,
        error=_FakeRateLimitError(),
    )

    assert result == {"results": [{"title": "ok"}]}
    assert tool.calls == 1
    assert STATE_RATE_LIMITED not in ctx.state


@pytest.mark.asyncio
async def test_plugin_returns_rate_limited_after_exhausted_retries() -> None:
    plugin = RateLimitPlugin(max_auto_retries=1, initial_backoff_seconds=0)
    tool = _FakeTool("url_context", [_FakeRateLimitError("still limited")])
    ctx = SimpleNamespace(state={})

    result = await plugin.on_tool_error_callback(
        tool=tool,
        tool_args={"url": "https://example.com"},
        tool_context=ctx,
        error=_FakeRateLimitError("first"),
    )

    assert result is not None
    assert result["status"] == "rate_limited"
    assert result["retries_attempted"] == 1
    assert tool.calls == 1
    assert ctx.state[STATE_RATE_LIMITED] is True


@pytest.mark.asyncio
async def test_after_tool_callback_rewrites_embedded_429_payload() -> None:
    plugin = RateLimitPlugin(max_auto_retries=0, initial_backoff_seconds=0)
    tool = _FakeTool("google_search", [])
    ctx = SimpleNamespace(state={})

    rewritten = await plugin.after_tool_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        result={"error": "429 RESOURCE_EXHAUSTED"},
    )

    assert rewritten is not None
    assert rewritten["status"] == "rate_limited"
    assert ctx.state[STATE_RATE_LIMITED] is True


@pytest.mark.asyncio
async def test_plugin_ignores_non_rate_limit_errors() -> None:
    plugin = RateLimitPlugin(max_auto_retries=1, initial_backoff_seconds=0)
    tool = _FakeTool("google_search", [])
    ctx = SimpleNamespace(state={})

    result = await plugin.on_tool_error_callback(
        tool=tool,
        tool_args={},
        tool_context=ctx,
        error=ValueError("bad request"),
    )
    assert result is None
    assert ctx.state == {}


def test_rate_limit_tool_result_shape() -> None:
    payload = rate_limit_tool_result(tool_name="x", error=_FakeRateLimitError(), retries_attempted=2)
    assert payload["error_type"] == "RESOURCE_EXHAUSTED"
    assert payload["tool"] == "x"
    assert payload["retries_attempted"] == 2
