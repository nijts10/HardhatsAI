"""Shared HTTP fetching for Part 5's two checks (crawlability.py,
page_visibility.py) -- one proven pattern, not two half-duplicated ones.

Fetches use our OWN identifying User-Agent, never one of the test agent
names (GPTBot, ClaudeBot, ...) -- this is a legitimate audit of what a
site's robots.txt actually DECLARES for those agents, not an attempt to
impersonate one of them to see what content it would get. robots.txt
itself is not agent-gated to fetch (the file describes policy for other
agents, it doesn't hide itself from an honest requester), so spoofing
would gain nothing except being dishonest about who's asking.

Same caller-injection pattern as visibility.py's VisibilityCaller /
claims_diff.py's AnswerClaimCaller: real network I/O lives behind
make_httpx_fetcher(), tests inject a canned Fetcher instead."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import httpx

USER_AGENT = "HardhatsAI-VisibilityAudit/1.0 (+https://hardhatsai.example/about-this-bot)"


@dataclass
class FetchResult:
    status_code: Optional[int]
    text: Optional[str]
    error: Optional[str]


Fetcher = Callable[[str], FetchResult]


def make_httpx_fetcher(timeout: float = 10.0) -> Fetcher:
    def _fetch(url: str) -> FetchResult:
        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})
            return FetchResult(status_code=response.status_code, text=response.text, error=None)
        except httpx.HTTPError as exc:
            return FetchResult(status_code=None, text=None, error=str(exc))

    return _fetch
