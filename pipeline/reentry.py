"""Return-visitor detection via short-term appearance buffer.

Stores a 3-bin HSV histogram signature per visitor. On a new ENTRY, checks
the buffer within a 5-minute window; returns the prior visitor_id when
similarity exceeds the threshold so we emit REENTRY instead of a fresh ENTRY.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

Signature = tuple[float, ...]


def vector_similarity(a: Signature, b: Signature) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


@dataclass
class ReturnVisitorBuffer:
    window_ms: int = 5 * 60 * 1000
    similarity_threshold: float = 0.90
    _records: deque = field(default_factory=deque)

    def record_exit(self, visitor_id: str, signature: Signature, ts_ms: int) -> None:
        self._records.append((ts_ms, visitor_id, signature))
        self._evict(ts_ms)

    def lookup(self, signature: Signature, ts_ms: int) -> Optional[str]:
        self._evict(ts_ms)
        best_vid = None
        best_sim = 0.0
        for _ts, vid, sig in self._records:
            sim = vector_similarity(signature, sig)
            if sim > best_sim:
                best_sim = sim
                best_vid = vid
        return best_vid if best_sim >= self.similarity_threshold else None

    def _evict(self, now_ms: int) -> None:
        cutoff = now_ms - self.window_ms
        while self._records and self._records[0][0] < cutoff:
            self._records.popleft()
