"""Retry helpers."""

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry(func: Callable[[], T], attempts: int = 3) -> T:
    """Retry a callable a fixed number of times."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("Retry failed without executing function")
