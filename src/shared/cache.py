"""带 TTL 的内存缓存：同一链接重复发不再重复抓取/调用 LLM。"""

import time


class TTLCache:
    def __init__(self, maxsize: int = 256, ttl: float = 1800.0):
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: dict[object, tuple[float, object]] = {}

    def get(self, key: object) -> object | None:
        item = self._data.get(key)
        if item is None:
            return None
        stored_at, value = item
        if time.monotonic() - stored_at >= self.ttl:
            del self._data[key]
            return None
        return value

    def set(self, key: object, value: object) -> None:
        self._data[key] = (time.monotonic(), value)
        while len(self._data) > self.maxsize:
            self._data.pop(next(iter(self._data)))
