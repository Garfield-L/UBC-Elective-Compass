"""Server-side, Redis-backed storage for the public interaction total."""

from __future__ import annotations

import os
from typing import Any, Final, Protocol

import requests


INTERACTION_COUNTER_KEY: Final = "ubc_elective_compass:interaction_total"
INITIAL_INTERACTION_TOTAL: Final = 100
UPSTASH_REDIS_REST_URL_ENV: Final = "UPSTASH_REDIS_REST_URL"
UPSTASH_REDIS_REST_TOKEN_ENV: Final = "UPSTASH_REDIS_REST_TOKEN"


class InteractionCounterUnavailable(RuntimeError):
    """Raised when the optional persistent counter cannot be reached safely."""


class InteractionCounterStore(Protocol):
    """Small dependency boundary used by the API and its offline tests."""

    def get_total(self) -> int:
        """Return the initialized global total without changing it."""

    def increment(self) -> int:
        """Atomically add one to the initialized global total and return it."""


class UpstashRedisInteractionCounter:
    """Use Upstash's Redis REST commands without exposing credentials to clients.

    Each operation first uses Redis ``SET ... NX`` to establish the requested
    seed only when the key is absent, then reads or atomically increments the
    integer. Concurrent initializers cannot overwrite an existing total.
    """

    def __init__(
        self,
        rest_url: str,
        rest_token: str,
        *,
        http_client: Any = requests,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not rest_url.strip() or not rest_token.strip():
            raise ValueError("Upstash REST URL and token are both required")
        self.rest_url = rest_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {rest_token}"}
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds

    def _command(self, *command: str | int) -> object:
        try:
            response = self.http_client.post(
                self.rest_url,
                json=list(command),
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise InteractionCounterUnavailable("Interaction storage is unavailable") from error

        if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
            raise InteractionCounterUnavailable("Interaction storage returned an invalid response")
        return payload["result"]

    @staticmethod
    def _as_total(value: object) -> int:
        if isinstance(value, bool):
            raise InteractionCounterUnavailable("Interaction storage returned an invalid total")
        try:
            total = int(value)
        except (TypeError, ValueError) as error:
            raise InteractionCounterUnavailable("Interaction storage returned an invalid total") from error
        if total < 0:
            raise InteractionCounterUnavailable("Interaction storage returned an invalid total")
        return total

    def _initialize_if_absent(self) -> None:
        # ``NX`` makes the requested 100 seed race-safe and never resets an
        # existing production value.
        self._command("SET", INTERACTION_COUNTER_KEY, INITIAL_INTERACTION_TOTAL, "NX")

    def get_total(self) -> int:
        self._initialize_if_absent()
        return self._as_total(self._command("GET", INTERACTION_COUNTER_KEY))

    def increment(self) -> int:
        self._initialize_if_absent()
        return self._as_total(self._command("INCR", INTERACTION_COUNTER_KEY))


def interaction_store_from_environment() -> InteractionCounterStore | None:
    """Build the optional persistent store only when both server secrets exist."""
    rest_url = os.getenv(UPSTASH_REDIS_REST_URL_ENV)
    rest_token = os.getenv(UPSTASH_REDIS_REST_TOKEN_ENV)
    if not rest_url or not rest_token or not rest_url.strip() or not rest_token.strip():
        return None
    return UpstashRedisInteractionCounter(rest_url.strip(), rest_token.strip())
