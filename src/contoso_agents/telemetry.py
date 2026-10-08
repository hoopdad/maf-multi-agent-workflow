"""Body-free Azure HTTP timing, delivered exclusively through stderr."""

from __future__ import annotations

import json
import logging
import sys
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

from azure.core.pipeline.policies import SansIOHTTPPolicy

from .models import utc_now


def redact_url(url: str) -> str:
    """Discard credentials, query strings (including SAS), and fragments."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        if not hostname or parts.scheme not in {"http", "https"}:
            return "[redacted-url]"
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except (TypeError, ValueError):
        return "[redacted-url]"


class AzureHttpTimingPolicy(SansIOHTTPPolicy):
    """A SansIO policy also usable with Blob SDK public raw HTTP hooks."""

    _context_key = "contoso_http_timing"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        super().__init__()
        self._logger = logger or logging.getLogger("contoso_agents.azure_http")
        if logger is None and not self._logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False

    def on_request(self, request) -> None:
        request.context[self._context_key] = {
            "started": perf_counter(),
            "timestamp": utc_now(),
            "method": request.http_request.method,
            "url": redact_url(request.http_request.url),
        }

    def _finish(self, request, status_code: int | None, outcome: str) -> None:
        timing = request.context.pop(self._context_key, None)
        if timing is None:
            return
        self._logger.info(
            json.dumps(
                {
                    "event": "azure_http",
                    "timestamp": timing["timestamp"],
                    "method": timing["method"],
                    "url": timing["url"],
                    "status_code": status_code,
                    "outcome": outcome,
                    "duration_ms": round((perf_counter() - timing["started"]) * 1000, 3),
                },
                separators=(",", ":"),
            )
        )

    def on_response(self, request, response) -> None:
        self._finish(request, response.http_response.status_code, "response")

    def on_raw_response(self, response) -> None:
        # The Blob raw-response hook receives only the response, sharing request context.
        self._finish(response, response.http_response.status_code, "response")

    def on_exception(self, request) -> None:
        self._finish(request, None, "exception")
