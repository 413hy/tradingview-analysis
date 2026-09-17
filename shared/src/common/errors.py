"""Bounded diagnostics that never interpolate request URLs or auth payloads."""

import httpx


def error_code(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return "HTTP_" + str(exc.response.status_code)
    if isinstance(exc, httpx.TimeoutException):
        return "NETWORK_TIMEOUT"
    if isinstance(exc, httpx.HTTPError):
        return "NETWORK_ERROR"
    return type(exc).__name__
