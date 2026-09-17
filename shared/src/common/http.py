"""Shared HTTP client factory with explicit direct connections."""
import httpx


def client(**kwargs):
    return httpx.AsyncClient(trust_env=False, **kwargs)
