"""Redis-backed circuit breaker — shares state across multiple instances.

State is stored in Redis so that distributed deployments (multiple gateway
processes) see consistent circuit state:

  rl:cb:<name>:state          STRING  "closed" | "open" | "half_open"
  rl:cb:<name>:failure_count  STRING  integer (via INCR)
  rl:cb:<name>:success_count  STRING  integer (via INCR)
  rl:cb:<name>:opened_at      STRING  unix timestamp float

All keys are namespaced under ``rl:cb:<name>:``.

Graceful degradation: if Redis is unreachable, the breaker falls back to
in-memory local state (same behaviour as the plain CircuitBreaker).
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

import redis as redis_lib

from reliability_lab.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)

T = TypeVar("T")

_KEY_STATE = "rl:cb:{name}:state"
_KEY_FAILURES = "rl:cb:{name}:failure_count"
_KEY_SUCCESSES = "rl:cb:{name}:success_count"
_KEY_OPENED_AT = "rl:cb:{name}:opened_at"
_KEY_LOG = "rl:cb:{name}:log"

# How long Redis keys live without activity (seconds); prevents orphaned keys.
_KEY_TTL = 3600


class RedisCircuitBreaker(CircuitBreaker):
    """Circuit breaker that persists state in Redis.

    Inherits the full state-machine logic from ``CircuitBreaker`` but overrides
    the counters and state storage to use Redis.  When Redis is unavailable the
    instance degrades silently to pure in-memory tracking.

    Parameters
    ----------
    redis_url:
        Connection string, e.g. ``"redis://localhost:6379/0"``.
    All other parameters are identical to ``CircuitBreaker``.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_timeout_seconds: float,
        redis_url: str = "redis://localhost:6379/0",
        success_threshold: int = 1,
    ) -> None:
        # Must set these BEFORE super().__init__() because the parent's
        # __init__ sets self.state = CLOSED which triggers our property setter,
        # which calls self._r() → self._redis_available.
        self.__dict__["_redis_url"] = redis_url
        self.__dict__["_redis"] = None
        self.__dict__["_redis_available"] = True
        self.__dict__["_local_state"] = CircuitState.CLOSED
        self.__dict__["_local_failures"] = 0
        self.__dict__["_local_successes"] = 0
        self.__dict__["_local_opened_at"] = None

        super().__init__(
            name=name,
            failure_threshold=failure_threshold,
            reset_timeout_seconds=reset_timeout_seconds,
            success_threshold=success_threshold,
        )
        # Now attempt Redis connection (may flip _redis_available to False)
        self._connect()

    # ------------------------------------------------------------------
    # Redis connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            r = redis_lib.from_url(self.__dict__["_redis_url"], socket_connect_timeout=1)
            r.ping()
            self.__dict__["_redis"] = r
            self.__dict__["_redis_available"] = True
        except Exception:
            self.__dict__["_redis"] = None
            self.__dict__["_redis_available"] = False

    def _r(self) -> redis_lib.Redis | None:  # type: ignore[type-arg]
        """Return Redis client, or None if unavailable."""
        if not self.__dict__.get("_redis_available", False):
            return None
        if self.__dict__.get("_redis") is None:
            self._connect()
        return self.__dict__.get("_redis")

    def _key(self, suffix: str) -> str:
        return f"rl:cb:{self.name}:{suffix}"

    # ------------------------------------------------------------------
    # State property — reads from Redis, falls back to in-memory
    # ------------------------------------------------------------------

    @property  # type: ignore[override]
    def state(self) -> CircuitState:  # type: ignore[override]
        r = self._r()
        if r is None:
            return self.__dict__.get("_local_state", CircuitState.CLOSED)
        try:
            raw = r.get(self._key("state"))
            if raw is None:
                return CircuitState.CLOSED
            return CircuitState(raw.decode())
        except Exception:
            self.__dict__["_redis_available"] = False
            return self.__dict__.get("_local_state", CircuitState.CLOSED)

    @state.setter
    def state(self, value: CircuitState) -> None:
        self.__dict__["_local_state"] = value
        r = self._r()
        if r is None:
            return
        try:
            r.set(self._key("state"), value.value, ex=_KEY_TTL)
        except Exception:
            self.__dict__["_redis_available"] = False

    # ------------------------------------------------------------------
    # failure_count / success_count — Redis integers
    # ------------------------------------------------------------------

    @property  # type: ignore[override]
    def failure_count(self) -> int:  # type: ignore[override]
        r = self._r()
        if r is None:
            return self.__dict__.get("_local_failures", 0)
        try:
            raw = r.get(self._key("failure_count"))
            return int(raw) if raw is not None else 0
        except Exception:
            self.__dict__["_redis_available"] = False
            return self.__dict__.get("_local_failures", 0)

    @failure_count.setter
    def failure_count(self, value: int) -> None:
        self.__dict__["_local_failures"] = value
        r = self._r()
        if r is None:
            return
        try:
            if value == 0:
                r.delete(self._key("failure_count"))
            else:
                r.set(self._key("failure_count"), value, ex=_KEY_TTL)
        except Exception:
            self.__dict__["_redis_available"] = False

    @property  # type: ignore[override]
    def success_count(self) -> int:  # type: ignore[override]
        r = self._r()
        if r is None:
            return self.__dict__.get("_local_successes", 0)
        try:
            raw = r.get(self._key("success_count"))
            return int(raw) if raw is not None else 0
        except Exception:
            self.__dict__["_redis_available"] = False
            return self.__dict__.get("_local_successes", 0)

    @success_count.setter
    def success_count(self, value: int) -> None:
        self.__dict__["_local_successes"] = value
        r = self._r()
        if r is None:
            return
        try:
            if value == 0:
                r.delete(self._key("success_count"))
            else:
                r.set(self._key("success_count"), value, ex=_KEY_TTL)
        except Exception:
            self.__dict__["_redis_available"] = False

    # ------------------------------------------------------------------
    # opened_at — Redis string (float timestamp)
    # ------------------------------------------------------------------

    @property  # type: ignore[override]
    def opened_at(self) -> float | None:  # type: ignore[override]
        r = self._r()
        if r is None:
            return self.__dict__.get("_local_opened_at", None)
        try:
            raw = r.get(self._key("opened_at"))
            return float(raw) if raw is not None else None
        except Exception:
            self.__dict__["_redis_available"] = False
            return self.__dict__.get("_local_opened_at", None)

    @opened_at.setter
    def opened_at(self, value: float | None) -> None:
        self.__dict__["_local_opened_at"] = value
        r = self._r()
        if r is None:
            return
        try:
            if value is None:
                r.delete(self._key("opened_at"))
            else:
                r.set(self._key("opened_at"), str(value), ex=_KEY_TTL)
        except Exception:
            self.__dict__["_redis_available"] = False

    # ------------------------------------------------------------------
    # _transition — also appends to Redis list for shared audit log
    # ------------------------------------------------------------------

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        super()._transition(new_state, reason)
        r = self._r()
        if r is None:
            return
        try:
            import json as _json

            entry = _json.dumps(
                {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
            )
            r.rpush(self._key("log"), entry)
            r.expire(self._key("log"), _KEY_TTL)
        except Exception:
            pass  # log write failures are non-fatal

    # ------------------------------------------------------------------
    # Convenience: clear all Redis keys for this breaker (useful in tests)
    # ------------------------------------------------------------------

    def reset_redis(self) -> None:
        """Delete all Redis keys for this circuit breaker."""
        r = self._r()
        if r is None:
            return
        keys = [
            self._key("state"),
            self._key("failure_count"),
            self._key("success_count"),
            self._key("opened_at"),
            self._key("log"),
        ]
        r.delete(*keys)
