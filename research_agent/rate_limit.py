"""Rate-limit (HTTP 429 / RESOURCE_EXHAUSTED) handling for ADK tool and model calls.

Converts quota failures into structured tool/model results the agent can act on,
instead of letting uncaught exceptions tear down the run. Aligns with the Router
prompt's "Error Handling: Quota / 429" policy.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from typing import Optional

from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.genai import types

logger = logging.getLogger(__name__)

# Session state keys written when a rate limit is observed.
STATE_RATE_LIMITED = "rate_limited"
STATE_RATE_LIMIT_EVENT = "rate_limit_event"

# Tools that are safe to auto-retry once on 429 (read-only / idempotent-ish).
# Mutating MCP tools (save*, complete, write*) are NOT auto-retried.
_SAFE_RETRY_TOOL_NAME_RE = re.compile(
    r"(search|url_context|google_search|load_web|fetch|get_|list_|read_)",
    re.IGNORECASE,
)

_RATE_LIMIT_MARKERS = (
    "429",
    "resource_exhausted",
    "resource exhausted",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "quota exceeded",
    "quota_exceeded",
    "too many requests",
    "exceeded your current quota",
)


def is_rate_limit_error(error: Any) -> bool:
    """Return True if ``error`` looks like an HTTP 429 / quota exhaustion."""
    if error is None:
        return False

    code = getattr(error, "code", None)
    if code is None:
        code = getattr(error, "status_code", None)
    if code is None and hasattr(error, "response"):
        code = getattr(error.response, "status_code", None)
    try:
        if int(code) == 429:
            return True
    except (TypeError, ValueError):
        pass

    status = str(getattr(error, "status", "") or getattr(error, "reason", "")).upper()
    if "RESOURCE_EXHAUSTED" in status or "TOO_MANY_REQUESTS" in status:
        return True

    text = _error_text(error).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def is_rate_limit_payload(result: Any) -> bool:
    """Detect rate-limit signals inside a successful tool result payload."""
    if result is None:
        return False
    if isinstance(result, dict):
        nested_error = result.get("error")
        if nested_error is not None and is_rate_limit_error(nested_error):
            return True
        status = str(result.get("status", "")).lower()
        if status in {"429", "resource_exhausted", "rate_limited", "quota"}:
            return True
        code = result.get("code") or result.get("status_code") or result.get("http_status")
        try:
            if int(code) == 429:
                return True
        except (TypeError, ValueError):
            pass
        blob = " ".join(str(v) for v in result.values() if isinstance(v, (str, int)))
        return any(marker in blob.lower() for marker in _RATE_LIMIT_MARKERS)
    return is_rate_limit_error(result)


def _error_text(error: Any) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


def _is_safe_to_retry(tool_name: str) -> bool:
    name = tool_name or ""
    # Explicitly block common mutating research MCP tools.
    lowered = name.lower()
    if any(
        token in lowered
        for token in (
            "save",
            "write",
            "complete",
            "create",
            "update",
            "delete",
            "publish",
            "start",
        )
    ):
        return False
    return bool(_SAFE_RETRY_TOOL_NAME_RE.search(name))


def rate_limit_tool_result(
    *,
    tool_name: str,
    error: Any,
    retries_attempted: int = 0,
) -> dict[str, Any]:
    """Structured tool response that keeps the agent run alive after a 429."""
    return {
        "status": "rate_limited",
        "error_type": "RESOURCE_EXHAUSTED",
        "http_status": 429,
        "tool": tool_name,
        "retries_attempted": retries_attempted,
        "message": (
            "Tool call hit a rate limit / quota exhaustion (HTTP 429). "
            "Do not retry this tool with the same arguments. "
            "If you are Research or Reviewer, stop further search/url calls, "
            "preserve any partial draft, and hand control back so Router can "
            "SYNTHESIZE. If you are Router, transfer to synthesis_agent now."
        ),
        "error_details": _error_text(error)[:2000],
    }


def mark_rate_limited(stateful_ctx: Any, *, tool_name: str, error: Any) -> None:
    """Record rate-limit signal on session state for Router/Synthesis prompts."""
    try:
        state = getattr(stateful_ctx, "state", None)
        if state is None:
            return
        state[STATE_RATE_LIMITED] = True
        state[STATE_RATE_LIMIT_EVENT] = {
            "tool": tool_name,
            "error_type": "RESOURCE_EXHAUSTED",
            "http_status": 429,
            "details": _error_text(error)[:1000],
        }
    except Exception:  # noqa: BLE001 - never let bookkeeping break the run
        logger.exception("Failed to record rate_limit state for tool %s", tool_name)


def rate_limit_model_response(error: Any) -> LlmResponse:
    """Fallback model response when the LLM itself is rate-limited."""
    text = (
        "RESOURCE_EXHAUSTED / HTTP 429: the model call was rate-limited. "
        "Do not start another research or review loop. "
        "Transfer to synthesis_agent immediately with whatever draft_report "
        "exists, or report that research could not start due to Vertex quota."
        f"\n\nDetails: {_error_text(error)[:1000]}"
    )
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
        error_code="429",
        error_message=_error_text(error)[:1000],
    )


class RateLimitPlugin(BasePlugin):
    """App-wide handling for tool and model rate-limit failures.

    Behavior:
    - Tool 429 exceptions become structured tool results (run continues).
    - Safe read tools get a short automatic retry before failing soft.
    - Successful tool payloads that embed 429/quota errors are rewritten the same way.
    - Model 429 errors become a controlled LlmResponse instead of crashing the run.
    """

    def __init__(
        self,
        *,
        name: str = "rate_limit_plugin",
        max_auto_retries: int = 1,
        initial_backoff_seconds: float = 1.0,
        backoff_multiplier: float = 2.0,
    ) -> None:
        super().__init__(name=name)
        self.max_auto_retries = max(0, max_auto_retries)
        self.initial_backoff_seconds = max(0.0, initial_backoff_seconds)
        self.backoff_multiplier = max(1.0, backoff_multiplier)

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: Any,
        error: Exception,
    ) -> Optional[dict[str, Any]]:
        if not is_rate_limit_error(error):
            return None

        tool_name = getattr(tool, "name", "<unknown>")
        retries = 0
        last_error: Any = error

        if self.max_auto_retries > 0 and _is_safe_to_retry(tool_name):
            for attempt in range(1, self.max_auto_retries + 1):
                delay = self.initial_backoff_seconds * (
                    self.backoff_multiplier ** (attempt - 1)
                )
                logger.warning(
                    "Rate limit on tool %s; retry %s/%s after %.2fs",
                    tool_name,
                    attempt,
                    self.max_auto_retries,
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
                try:
                    result = await tool.run_async(
                        args=tool_args, tool_context=tool_context
                    )
                    if is_rate_limit_payload(result):
                        last_error = result
                        retries = attempt
                        continue
                    return result if isinstance(result, dict) else {"result": result}
                except Exception as retry_error:  # noqa: BLE001
                    last_error = retry_error
                    retries = attempt
                    if not is_rate_limit_error(retry_error):
                        # Non-rate-limit failure on retry: surface as soft error.
                        return {
                            "status": "error",
                            "tool": tool_name,
                            "message": (
                                "Retry after rate limit failed with a different error."
                            ),
                            "error_details": _error_text(retry_error)[:2000],
                            "retries_attempted": retries,
                        }

        mark_rate_limited(tool_context, tool_name=tool_name, error=last_error)
        return rate_limit_tool_result(
            tool_name=tool_name,
            error=last_error,
            retries_attempted=retries,
        )

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: Any,
        result: Any,
    ) -> Optional[dict[str, Any]]:
        if not is_rate_limit_payload(result):
            return None
        tool_name = getattr(tool, "name", "<unknown>")
        mark_rate_limited(tool_context, tool_name=tool_name, error=result)
        return rate_limit_tool_result(tool_name=tool_name, error=result)

    async def on_model_error_callback(
        self,
        *,
        callback_context: Any,
        llm_request: Any,
        error: Exception,
    ) -> Optional[LlmResponse]:
        if not is_rate_limit_error(error):
            return None
        mark_rate_limited(callback_context, tool_name="model", error=error)
        logger.warning("Model call rate-limited: %s", _error_text(error)[:500])
        return rate_limit_model_response(error)
