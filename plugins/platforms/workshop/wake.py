"""Authenticated outbound wake delivery for autonomous workshop turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import aiohttp


class WorkshopWakeError(RuntimeError):
    """Base class for outbound wake delivery failures."""


@dataclass(frozen=True)
class WorkshopWakeRejectedError(WorkshopWakeError):
    """The workshop permanently rejected a wake with an HTTP 4xx."""

    status: int

    def __str__(self) -> str:
        return f"workshop wake was permanently rejected with HTTP {self.status}"


class WorkshopWakeRetryableError(WorkshopWakeError):
    """Wake delivery may be retried by the durable completion pipeline."""


class WorkshopWakeClient:
    """Small fail-closed HTTP client for announcing already-created turns."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        timeout_seconds: float,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
    ):
        self.url = url
        self.token = token
        self.timeout_seconds = float(timeout_seconds)
        self._session_factory = session_factory

    async def deliver(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with self._session_factory(timeout=timeout) as session:
                async with session.post(
                    self.url,
                    json=payload,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    status = int(response.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise WorkshopWakeRetryableError(
                f"workshop wake transport failed: {type(exc).__name__}"
            ) from exc

        if 200 <= status < 300:
            return
        if 400 <= status < 500:
            raise WorkshopWakeRejectedError(status)
        raise WorkshopWakeRetryableError(
            f"workshop wake returned retryable HTTP {status}"
        )
