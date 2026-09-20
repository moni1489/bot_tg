from __future__ import annotations

import random
import time
from urllib.parse import urlparse


class PoliteLimiter:
    """Serial pauses between HTTP calls. One cycle, no parallel blast."""

    def __init__(
        self,
        min_sleep: float = 2.0,
        max_sleep: float = 5.0,
        *,
        rng: random.Random | None = None,
        sleeper=time.sleep,
    ) -> None:
        if min_sleep > max_sleep:
            min_sleep, max_sleep = max_sleep, min_sleep
        self.min_sleep = float(min_sleep)
        self.max_sleep = float(max_sleep)
        self.rng = rng or random.Random()
        self._sleeper = sleeper
        self._first = True
        self.blocked_hosts: set[str] = set()
        self.blocked_urls: set[str] = set()
        self.playwright_blocked = False
        self.sleeps: list[float] = []

    def reset_cycle(self) -> None:
        self._first = True
        self.blocked_hosts.clear()
        self.blocked_urls.clear()
        self.playwright_blocked = False
        self.sleeps.clear()

    def host(self, url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def is_blocked(self, url: str) -> bool:
        """True for this URL only (403) or this host (429). RSS 403 must not skip Funko/HTML."""
        if (url or "") in self.blocked_urls:
            return True
        host = self.host(url)
        if not host:
            return False
        return host in self.blocked_hosts

    def mark_status(self, url: str, status: int) -> None:
        if status == 429:
            host = self.host(url)
            if host:
                self.blocked_hosts.add(host)
            return
        if status == 403:
            # Same URL only. Do not blacklist www.ebay.com for the rest of the cycle.
            if url:
                self.blocked_urls.add(url)

    def should_skip_retries(self, url: str) -> bool:
        """Skip repeating the exact URL that already 403/429'd, not the whole eBay cycle."""
        return self.is_blocked(url)

    def mark_playwright_status(self, status: int) -> None:
        if status in {403, 429}:
            self.playwright_blocked = True

    def should_skip_playwright(self) -> bool:
        """RSS/HTML 403 must not skip Playwright; only a prior Playwright 403/429."""
        return self.playwright_blocked

    def wait(self, kind: str | None = None, html: bool | None = None, **_kwargs) -> float:
        if self._first:
            self._first = False
            self.sleeps.append(0.0)
            return 0.0
        delay = self.rng.uniform(self.min_sleep, self.max_sleep)
        self._sleeper(delay)
        self.sleeps.append(delay)
        return delay
