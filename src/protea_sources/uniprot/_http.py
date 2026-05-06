"""Private HTTP retry helper for the UniProt source plugin.

UniProt's REST API rate-limits aggressively (429) and returns 5xx
intermittently under load. The client wraps :mod:`requests` with:

* Exponential backoff (``backoff_base * 2**(attempt-1)``) capped at
  ``backoff_max``.
* Optional ``Retry-After`` honoring (UniProt sometimes sets it on 429).
* Random uniform jitter so coordinated retries don't synchronise.
* Per-execution counters (``client.requests``, ``client.retries``)
  the operation surfaces in its emit events.

This module is *private* to the protea-sources package — the
``_`` prefix on the filename signals "do not import from outside the
plugin". Operations consume the plugin's public ``stream_fasta``
method which uses this client internally.

The implementation mirrors the behaviour of the legacy
``protea.core.utils.UniProtHttpClient`` but lives here so the plugin
is self-contained.
"""

from __future__ import annotations

import random
import time
from typing import Any

import requests
from protea_contracts import UniProtFastaStreamPayload
from requests import Response


class UniProtRetryClient:
    """HTTP client with retries tailored for the UniProt REST API."""

    def __init__(self) -> None:
        self.session: requests.Session = requests.Session()
        self.requests: int = 0
        self.retries: int = 0

    def reset(self) -> None:
        """Zero the per-execution counters before a new run.

        The underlying ``requests.Session`` is reused across executions
        so connection pooling stays effective.
        """
        self.requests = 0
        self.retries = 0

    def get_with_retries(
        self,
        url: str,
        payload: UniProtFastaStreamPayload,
        emit: Any,
    ) -> Response:
        """GET with retry/backoff on 429, 5xx, and network errors.

        Reads retry knobs (``max_retries``, ``backoff_base_seconds``,
        ``backoff_max_seconds``, ``jitter_seconds``, ``user_agent``,
        ``timeout_seconds``) from the typed payload. Emits
        ``http.retry`` events for observability when a wait happens.
        """
        headers = {"User-Agent": payload.user_agent}
        attempt = 0
        while True:
            attempt += 1
            self.requests += 1
            try:
                resp = self.session.get(
                    url, timeout=payload.timeout_seconds, headers=headers
                )
            except requests.RequestException as exc:
                if attempt > payload.max_retries:
                    raise
                self.retries += 1
                self._sleep_backoff(
                    payload, attempt, emit,
                    reason=f"request_exception:{exc.__class__.__name__}",
                )
                continue

            if 200 <= resp.status_code < 300:
                return resp

            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt > payload.max_retries:
                    resp.raise_for_status()
                self.retries += 1
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_s = min(float(retry_after), payload.backoff_max_seconds)
                    emit(
                        "http.retry",
                        None,
                        {
                            "attempt": attempt,
                            "wait_seconds": wait_s,
                            "reason": "retry_after",
                        },
                        "warning",
                    )
                    time.sleep(wait_s)
                else:
                    self._sleep_backoff(
                        payload, attempt, emit,
                        reason=f"status_{resp.status_code}",
                    )
                continue

            resp.raise_for_status()

    def _sleep_backoff(
        self,
        payload: UniProtFastaStreamPayload,
        attempt: int,
        emit: Any,
        reason: str,
    ) -> None:
        base = payload.backoff_base_seconds * (2 ** (attempt - 1))
        wait_s = min(base, payload.backoff_max_seconds) + random.uniform(
            0.0, payload.jitter_seconds
        )
        emit(
            "http.retry",
            None,
            {"attempt": attempt, "wait_seconds": wait_s, "reason": reason},
            "warning",
        )
        time.sleep(wait_s)


def extract_next_cursor(link_header: str) -> str | None:
    """Extract the ``cursor=...`` value from a UniProt ``Link: ...; rel="next"`` header.

    Returns ``None`` when the header is empty, lacks a ``rel="next"``
    component, or doesn't contain a ``cursor=`` parameter. The
    implementation is intentionally permissive to handle UniProt's
    occasionally-malformed Link headers without raising.
    """
    if (
        not link_header
        or 'rel="next"' not in link_header
        or "cursor=" not in link_header
    ):
        return None
    try:
        return link_header.split("cursor=")[-1].split(">")[0]
    except Exception:
        return None
