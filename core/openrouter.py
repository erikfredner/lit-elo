"""Minimal OpenRouter Batch API client (stdlib only).

Docs: https://openrouter.ai/docs/batch-quickstart

* ``POST /api/v1/batches`` with inline ``requests`` (no JSONL upload). The
  top-level ``endpoint``/``model`` keys must precede ``requests`` because
  OpenRouter stream-parses the body.
* ``GET /api/v1/batches/{id}`` returns status and, once finished, ``results``
  (unordered; match by ``custom_id``) plus ``usage.cost``.
* No cancel endpoint; results are deleted after 30 days.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://openrouter.ai/api/v1"
FINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}


class OpenRouterError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"OpenRouter HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def api_key() -> str:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("Missing OPENROUTER_API_KEY environment variable.")
    return key


def _request(method: str, path: str, payload: dict | None = None, retries: int = 4) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "X-Title": "Canon Wars",
    }
    for attempt in range(retries + 1):
        req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise OpenRouterError(e.code, body) from None
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(2 ** attempt * 5)
                continue
            raise
    raise AssertionError("unreachable")


def create_batch(model: str, requests: list[dict], endpoint: str = "/v1/chat/completions") -> dict:
    # Key order matters: requests last.
    payload = {"endpoint": endpoint, "model": model, "completion_window": "24h", "requests": requests}
    return _request("POST", "/batches", payload)


def get_batch(batch_id: str) -> dict:
    return _request("GET", f"/batches/{batch_id}")


def list_models() -> list[dict]:
    return _request("GET", "/models")["data"]
