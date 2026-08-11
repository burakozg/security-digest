"""Retry utilities with exponential backoff."""

import logging
import random
import time
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Exceptions that are worth retrying (transient)
RETRYABLE_EXCEPTIONS = (
    Exception,  # Broad catch; we'll re-raise after max retries
)


def retry(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    non_retryable: tuple[type[Exception], ...] = (),
    **kwargs: Any,
) -> T:
    """Call func with retries and exponential backoff. Raises last exception if all
    retries fail. Exceptions matching non_retryable (e.g. auth errors) are re-raised
    immediately on the first attempt -- retrying a bad API key or wrong SMTP
    password just delays a failure the user has to go fix by hand."""
    last_exception: Exception | None = None
    delay = initial_delay

    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except non_retryable:
            raise
        except Exception as e:
            last_exception = e
            if attempt == max_retries:
                raise
            wait = min(delay, max_delay)
            if jitter:
                wait = wait * (0.5 + random.random())
            log.warning("Attempt %d/%d failed (%s), retrying in %.1fs: %s", attempt + 1, max_retries + 1, type(e).__name__, wait, e)
            time.sleep(wait)
            delay *= exponential_base

    raise last_exception  # type: ignore
