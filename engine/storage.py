"""Durable storage for the order book, actuals, and uploaded masters.

Two interchangeable backends behind one ``Store`` interface:

* **LocalStore** (default) — one JSON file per key under a directory. Used for
  local dev and tests; single-process, so no concurrency concerns.
* **UpstashStore** — Redis-over-HTTP (Upstash REST), used in production on a
  free, ephemeral host (Render). No extra dependency (stdlib urllib).

The interface offers plain key/value plus **hash** (per-field, for orders keyed
by SO number) and **list** (append-only, for actuals) operations — so two users
acting at once don't overwrite each other (different SO# fields never clash;
actuals are appended, never rewritten as one blob).

Config (production): set
    UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
Local override: STORE_DIR (defaults to ./data/store).
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Optional


class LocalStore:
    """File-backed store — one JSON file per key (single-process)."""

    def __init__(self, base_dir):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() else "_" for c in key)
        return self.base / f"{safe}.json"

    def _read(self, key):
        p = self._path(key)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _write(self, key, data):
        self._path(key).write_text(json.dumps(data), encoding="utf-8")

    # --- key/value --- #
    def kv_get(self, key) -> Optional[str]:
        d = self._read(key)
        return d.get("v") if isinstance(d, dict) and "v" in d else None

    def kv_set(self, key, value: str) -> None:
        self._write(key, {"v": value})

    # --- hash (field -> value) --- #
    def hgetall(self, key) -> dict:
        d = self._read(key)
        return dict(d.get("h", {})) if isinstance(d, dict) else {}

    def hset(self, key, field, value: str) -> None:
        d = self._read(key) or {}
        h = d.get("h", {})
        h[field] = value
        self._write(key, {"h": h})

    def hdel(self, key, field) -> None:
        d = self._read(key) or {}
        h = d.get("h", {})
        h.pop(field, None)
        self._write(key, {"h": h})

    # --- list (append-only) --- #
    def list_append(self, key, value: str) -> None:
        d = self._read(key) or {}
        lst = d.get("l", [])
        lst.append(value)
        self._write(key, {"l": lst})

    def list_all(self, key) -> list:
        d = self._read(key)
        return list(d.get("l", [])) if isinstance(d, dict) else []


class UpstashStore:
    """Redis-over-HTTP via the Upstash REST API."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _cmd(self, *args):
        body = json.dumps([str(a) for a in args]).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")

    def kv_get(self, key) -> Optional[str]:
        return self._cmd("GET", key)

    def kv_set(self, key, value: str) -> None:
        self._cmd("SET", key, value)

    def hgetall(self, key) -> dict:
        flat = self._cmd("HGETALL", key) or []
        return {flat[i]: flat[i + 1] for i in range(0, len(flat), 2)}

    def hset(self, key, field, value: str) -> None:
        self._cmd("HSET", key, field, value)

    def hdel(self, key, field) -> None:
        self._cmd("HDEL", key, field)

    def list_append(self, key, value: str) -> None:
        self._cmd("RPUSH", key, value)

    def list_all(self, key) -> list:
        return self._cmd("LRANGE", key, 0, -1) or []


def get_store():
    """Production Upstash store if configured, else a local file store."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return UpstashStore(url, token)
    base = os.environ.get("STORE_DIR") or (
        Path(__file__).resolve().parent.parent / "data" / "store"
    )
    return LocalStore(base)
