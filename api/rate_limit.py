from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request, status

_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_LOCK = Lock()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded:
        return forwarded[:80]
    return request.client.host[:80] if request.client else "unknown"


def enforce_rate_limit(request: Request, namespace: str, limit: int, window_seconds: int) -> None:
    key = f"{namespace}:{client_ip(request)}"
    now = monotonic()
    cutoff = now - window_seconds

    with _LOCK:
        if len(_BUCKETS) > 10_000:
            stale_keys = [
                bucket_key for bucket_key, values in _BUCKETS.items()
                if not values or values[-1] < cutoff
            ]
            for stale_key in stale_keys:
                _BUCKETS.pop(stale_key, None)

        bucket = _BUCKETS[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Demasiados intentos. Intente nuevamente más tarde.",
                headers={"Retry-After": str(window_seconds)},
            )
        bucket.append(now)
