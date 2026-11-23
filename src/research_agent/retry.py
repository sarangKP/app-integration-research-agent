"""Exponential backoff on rate limits (PRD §6: "exponential backoff on 429s")."""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _looks_like_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text or "quota" in text


def call_with_backoff(fn: Callable[..., T], *args, max_retries: int = 5, base_delay: float = 2.0, **kwargs) -> T:
    attempt = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            attempt += 1
            if not _looks_like_rate_limit(exc) or attempt > max_retries:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning("Rate limited (attempt %d/%d), backing off %.1fs: %s", attempt, max_retries, delay, exc)
            time.sleep(delay)
