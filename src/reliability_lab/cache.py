from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — used in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs).

    Example: "refund policy for 2024" vs "refund policy for 2026" → True (false hit).
    """
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# Similarity helpers (improved — character trigrams + TF weighting)
# ---------------------------------------------------------------------------

def _char_trigrams(text: str) -> Counter[str]:
    """Return character 3-gram counts for a normalized string."""
    t = text.lower().strip()
    t = re.sub(r"\s+", " ", t)
    if len(t) < 3:
        return Counter({t: 1})
    return Counter(t[i : i + 3] for i in range(len(t) - 2))


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    """Cosine similarity between two term-frequency counters."""
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b.get(k, 0) for k in a)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# In-memory cache (improved)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory response cache with semantic similarity and privacy guardrails.

    Improvements over baseline:
    - Exact-match fast path (similarity == 1.0 check on lowercased key).
    - Character trigram cosine similarity (distinguishes "2024" vs "2026").
    - Privacy check: _is_uncacheable() skips sensitive queries.
    - False-hit guard: _looks_like_false_hit() rejects year/ID mismatches.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float) -> None:
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, query: str) -> tuple[str | None, float]:
        """Return (response, score) or (None, best_score_seen)."""
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        # Evict expired entries
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

        # Exact-match fast path
        q_lower = query.lower().strip()
        for entry in self._entries:
            if entry.key.lower().strip() == q_lower:
                return entry.value, 1.0

        # Semantic similarity
        best_value: str | None = None
        best_score = 0.0
        q_trig = _char_trigrams(query)
        for entry in self._entries:
            score = self.similarity(query, entry.key, _q_trig=q_trig)
            if score > best_score:
                best_score = score
                best_value = entry.value if score >= self.similarity_threshold else None
                if best_value and _looks_like_false_hit(query, entry.key):
                    self.false_hit_log.append(
                        {"query": query, "cached_key": entry.key, "score": score}
                    )
                    best_value = None  # suppress false hit

        if best_value is not None and best_score >= self.similarity_threshold:
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response unless the query is privacy-sensitive."""
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str, *, _q_trig: Counter[str] | None = None) -> float:
        """Character trigram cosine similarity with exact-match fast path.

        Using character trigrams instead of pure token overlap means that
        small differences like "2024" vs "2026" produce meaningfully lower
        scores (different trigrams: '202', '024' vs '202', '026').
        """
        a_low = a.lower().strip()
        b_low = b.lower().strip()
        if a_low == b_low:
            return 1.0
        trig_a = _q_trig if _q_trig is not None else _char_trigrams(a)
        trig_b = _char_trigrams(b)
        return _cosine(trig_a, trig_b)


# ---------------------------------------------------------------------------
# Redis shared cache (implemented)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model:
        Key   = "{prefix}{query_hash}"   (one Redis Hash per query)
        Fields= "query"  (original query text)
                "response" (cached response)
        TTL   = set via Redis EXPIRE (automatic expiry, no manual eviction)

    Lookup strategy:
    1. Exact-match: hash the query and check the key directly (O(1)).
    2. Similarity scan: SCAN all keys with prefix, load each stored "query",
       compute cosine similarity, return best match above threshold.

    Guardrails applied on both get() and set():
    - _is_uncacheable(): skip privacy-sensitive queries.
    - _looks_like_false_hit(): reject year/ID mismatches on similarity hits.

    Resilience:
    - Redis connection errors are caught and logged; get/set degrade gracefully.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ) -> None:
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        Steps:
        1. Reject privacy-sensitive queries immediately.
        2. Try exact-match (O(1) hash lookup).
        3. Similarity scan across all stored entries.
        4. Apply false-hit detection before returning.
        """
        if _is_uncacheable(query):
            return None, 0.0

        try:
            # --- Step 2: exact-match ---
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_resp = self._redis.hget(exact_key, "response")
            if exact_resp is not None:
                return exact_resp, 1.0

            # --- Step 3: similarity scan ---
            best_value: str | None = None
            best_score = 0.0
            q_trig = _char_trigrams(query)
            for redis_key in self._redis.scan_iter(f"{self.prefix}*"):
                stored_query = self._redis.hget(redis_key, "query")
                if stored_query is None:
                    continue
                score = ResponseCache.similarity(query, stored_query, _q_trig=q_trig)
                if score > best_score:
                    best_score = score
                    if score >= self.similarity_threshold:
                        # --- Step 4: false-hit guard ---
                        if _looks_like_false_hit(query, stored_query):
                            self.false_hit_log.append(
                                {
                                    "query": query,
                                    "cached_key": stored_query,
                                    "score": score,
                                }
                            )
                            best_value = None
                        else:
                            best_value = self._redis.hget(redis_key, "response")

            if best_value is not None and best_score >= self.similarity_threshold:
                return best_value, best_score
            return None, best_score

        except Exception:
            # Redis down — degrade gracefully, never crash the gateway
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        Steps:
        1. Reject privacy-sensitive queries.
        2. Build deterministic hash key.
        3. Store as Redis Hash with query + response fields.
        4. Set TTL for automatic expiry.
        """
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            # Redis down — degrade gracefully
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
        except Exception:
            pass

    def close(self) -> None:
        """Close Redis connection."""
        try:
            if self._redis is not None:
                self._redis.close()
        except Exception:
            pass

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
