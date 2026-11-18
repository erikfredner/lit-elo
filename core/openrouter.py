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
# Decision models (output modality "decisions", e.g. typesafe/jev-1.14) reject
# chat completions and answer typed questions on this endpoint instead.
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
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


def _request(method: str, path: str, payload: dict | None = None, retries: int = 4, base: str = BASE_URL) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "X-Title": "Canon Wars",
    }
    for attempt in range(retries + 1):
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
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


def chat_completion(model: str, body: dict) -> dict:
    """One synchronous (non-batch) chat completion; ``body`` is a batch request body.

    Billed at the model's standard price (twice the batch price). Usage
    accounting is switched on so the response carries ``usage.cost``.
    """
    payload = {"model": model, **body, "usage": {"include": True}}
    return _request("POST", "/chat/completions", payload)


def decide(model: str, body: dict) -> dict:
    """One call to the Decisions API; ``body`` holds ``state`` and ``questions``.

    The response carries ``answers`` (keyed like ``questions``) and ``usage.cost``.
    """
    return _request("POST", "", {"model": model, **body}, base=DECISIONS_URL)


def run_sync(model: str, requests: list[dict], concurrency: int = 16) -> list[dict]:
    """Run batch-style requests as concurrent non-batch calls.

    Returns results in the Batch API's shape (``custom_id``, ``response.body``,
    ``error``) so batch and sync runs are parsed by the same code. A request
    that fails after retries gets ``error`` set and no response.
    """
    from concurrent.futures import ThreadPoolExecutor

    def call(req):
        try:
            send = decide if "questions" in req["body"] else chat_completion
            body = send(model, req["body"])
            return {"custom_id": req["custom_id"], "response": {"status_code": 200, "body": body}, "error": None}
        except Exception as exc:  # keep going; the vote is counted as missing
            return {"custom_id": req["custom_id"], "response": None, "error": str(exc)[:500]}

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(call, requests))


def results_cost(results: list[dict]) -> float:
    """Total ``usage.cost`` across sync results."""
    return sum((((r.get("response") or {}).get("body") or {}).get("usage") or {}).get("cost") or 0 for r in results)


def get_batch(batch_id: str) -> dict:
    return _request("GET", f"/batches/{batch_id}")


def list_models(output_modalities: str | None = None) -> list[dict]:
    """The model catalog. The default listing omits decision models; pass ``"decisions"`` for those."""
    query = f"?output_modalities={output_modalities}" if output_modalities else ""
    return _request("GET", f"/models{query}")["data"]
