"""滑动窗口限流，保护 LLM 开销不被群友刷爆。"""

import time
from collections import defaultdict, deque


class RateLimiter:
    """同一 key 在 per_seconds 窗口内最多放行 max_calls 次。"""

    def __init__(self, max_calls: int, per_seconds: float):
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._hits: dict[object, deque] = defaultdict(deque)

    def check(self, key: object) -> bool:
        """放行返回 True 并记录；超限返回 False（不记录）。"""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.per_seconds:
            hits.popleft()
        if len(hits) >= self.max_calls:
            return False
        hits.append(now)
        return True
