"""Optional durable key-value store for serverless/ephemeral hosts.

Local/dev: no store is configured -> the app uses its normal local file +
in-memory behaviour (so tests and `uvicorn` are unchanged).

Production on a free ephemeral host (e.g. Render): set the two env vars below and
actuals + uploaded datasets persist in Upstash Redis (free tier, no credit card).
Uses only the standard library (urllib) — no extra dependency, works anywhere.

    UPSTASH_REDIS_REST_URL    e.g. https://xxxx.upstash.io
    UPSTASH_REDIS_REST_TOKEN  the REST token from the Upstash console
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


class UpstashKV:
    """Minimal Redis-over-HTTP client (GET/SET) via the Upstash REST API."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _cmd(self, *args):
        body = json.dumps(list(args)).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")

    def get(self, key: str) -> Optional[str]:
        return self._cmd("GET", key)

    def set(self, key: str, value: str) -> None:
        self._cmd("SET", key, value)


def get_kv() -> Optional[UpstashKV]:
    """Return a configured store, or None when running locally (no env vars)."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return UpstashKV(url, token)
    return None
